"""Durable run/attempt audit and guarded recovery for abandoned processes."""

import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def encode(value):
    def default(item):
        if isinstance(item, (date, datetime, UUID)):
            return str(item)
        if isinstance(item, Decimal):
            return int(item) if item == item.to_integral_value() else str(item)
        raise TypeError(type(item).__name__)

    return json.dumps(value, default=default)


def run_key(run_id):
    return -1 - (UUID(str(run_id)).int % (2**62))


@contextmanager
def run_guard(engine, run_id):
    """A separate session lock proves a process is alive across transaction boundaries."""
    with engine.connect() as conn:
        key = run_key(run_id)
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        conn.commit()
        if not acquired:
            raise ValueError("Pipeline run is still active")
        try:
            yield
        finally:
            if not conn.invalidated:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                conn.commit()


def error_details(exc):
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if isinstance(exc, DBAPIError):
        # Never log SQL bind parameters, connection strings, or raw financial rows.
        message = f"Database operation failed (SQLSTATE {sqlstate or 'unavailable'})"
        diagnostic = getattr(original, "diag", None)
        constraint = getattr(diagnostic, "constraint_name", None)
        if constraint:
            message += f"; constraint={constraint}"
    elif isinstance(exc, ValueError):
        message = str(exc)[:1500]
    else:
        message = "Pipeline execution failed; inspect the exception type and application logs"
    return {"error_type": type(exc).__name__, "error_message": message, "sqlstate": sqlstate}


def retryable(exc):
    if not isinstance(exc, DBAPIError):
        return False
    state = getattr(getattr(exc, "orig", None), "sqlstate", "") or ""
    return exc.connection_invalidated or state in {"40001", "40P01", "55P03"} or state[:2] == "08"


def record_checks(conn, run_id, attempt, checks):
    if checks:
        conn.execute(
            text("""
            INSERT INTO etl.quality_results(run_id,attempt,check_name,violations,details)
            VALUES (:run,:attempt,:name,:violations,CAST(:details AS jsonb))
        """),
            [
                {
                    "run": run_id,
                    "attempt": attempt,
                    "name": r["check_name"],
                    "violations": r["violations"],
                    "details": encode(r.get("details", {})),
                }
                for r in checks
            ],
        )


def record_failure(engine, run_id, attempt, exc, metrics, final):
    details = error_details(exc)
    with engine.begin() as conn:
        metrics["rolled_back"] = True
        metrics["watermark_after"] = conn.execute(
            text("SELECT COALESCE(MAX(watermark),0) FROM etl.source_state")
        ).scalar_one()
        conn.execute(
            text("""
            UPDATE etl.run_attempts SET status='failed',ended_at=clock_timestamp(),
                metrics=CAST(:metrics AS jsonb),error_type=:error_type,
                error_message=:error_message,sqlstate=:sqlstate
            WHERE run_id=:run AND attempt=:attempt
        """),
            {"run": run_id, "attempt": attempt, "metrics": encode(metrics), **details},
        )
        record_checks(
            conn,
            run_id,
            attempt,
            [
                *metrics.get("quality_checks", []),
                {"check_name": "pipeline_failure", "violations": 1, "details": details},
            ],
        )
        conn.execute(
            text("""
            UPDATE etl.pipeline_runs SET status=:status,
                ended_at=CASE WHEN :final THEN clock_timestamp() ELSE NULL END,
                metrics=CAST(:metrics AS jsonb),error_type=:error_type,error_message=:error_message
            WHERE run_id=:run
        """),
            {
                "run": run_id,
                "status": "failed" if final else "running",
                "final": final,
                "metrics": encode(metrics),
                **details,
            },
        )


def recover_run(engine, run_id):
    run_id = str(UUID(str(run_id)))
    with run_guard(engine, run_id), engine.begin() as conn:
        state = conn.execute(
            text("SELECT status FROM etl.pipeline_runs WHERE run_id=:run FOR UPDATE"),
            {"run": run_id},
        ).scalar_one()
        if state != "running":
            raise ValueError("Only an abandoned running record can be recovered")
        conn.execute(
            text("""
            UPDATE etl.run_attempts SET status='abandoned',ended_at=clock_timestamp(),
                error_type='AbandonedRun',error_message='Process no longer holds its run lock'
            WHERE run_id=:run AND status='running'
        """),
            {"run": run_id},
        )
        conn.execute(
            text("""
            UPDATE etl.pipeline_runs SET status='abandoned',ended_at=clock_timestamp(),
                error_type='AbandonedRun',error_message='Process no longer holds its run lock'
            WHERE run_id=:run
        """),
            {"run": run_id},
        )
    return {"run_id": run_id, "status": "abandoned", "action": "rerun pending raw changes"}
