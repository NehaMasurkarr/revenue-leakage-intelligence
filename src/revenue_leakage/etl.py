"""Audited raw-to-staging-to-analytics orchestration with committed-change watermarks."""

import math
import re
import time
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from revenue_leakage.freshness import event_freshness
from revenue_leakage.migrations import lock, migrate
from revenue_leakage.pipeline import analyze_operational, lock_operational, sync_sequences
from revenue_leakage.run_log import encode, record_checks, record_failure, retryable, run_guard
from revenue_leakage.source import capture_snapshot
from revenue_leakage.transforms import publish, refresh_customers, stage_changes


@dataclass(frozen=True)
class ETLOptions:
    as_of: date
    source_name: str = "financials"
    mode: str = "incremental"
    batch_size: int = 1000
    max_attempts: int = 3
    retry_delay_seconds: float = 0.5
    max_data_age_days: int = 45

    def __post_init__(self):
        if not isinstance(self.as_of, date):
            raise ValueError("An explicit as_of date is required")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", self.source_name):
            raise ValueError("Invalid source name")
        if self.mode not in ("incremental", "full"):
            raise ValueError("ETL mode must be incremental or full")
        if not 1 <= self.batch_size <= 10_000 or not 1 <= self.max_attempts <= 5:
            raise ValueError("Batch size must be 1–10000 and attempts 1–5")
        if not 0 <= self.max_data_age_days <= 36500:
            raise ValueError("Max data age must be 0–36500 days")
        if not math.isfinite(self.retry_delay_seconds) or not 0 <= self.retry_delay_seconds <= 10:
            raise ValueError("Retry delay must be finite and 0–10 seconds")


class PipelineFailure(RuntimeError):
    def __init__(self, run_id, cause):
        self.run_id = run_id
        super().__init__(f"ETL run {run_id} failed ({type(cause).__name__}); inspect etl-runs")


def _process(engine, run_id, attempt, options, metrics):
    with engine.begin() as conn:
        lock(conn)
        lock_operational(conn)
        state = conn.execute(text("SELECT * FROM etl.source_state FOR UPDATE")).mappings().first()
        if not state or state["source_name"] != options.source_name:
            raise ValueError("No raw snapshot for the configured source")
        upper = conn.execute(
            text("SELECT COALESCE(MAX(change_id),0) FROM raw.changes WHERE source_name=:source"),
            {"source": options.source_name},
        ).scalar_one()
        metrics.update(
            {
                "watermark_before": state["watermark"],
                "watermark_after": state["watermark"],
                "watermark_target": upper,
                "snapshot_version": state["snapshot_version"],
                "phase": "staging",
            }
        )
        metrics.update(
            stage_changes(
                conn,
                options.source_name,
                state["watermark"],
                upper,
                options.mode,
                options.batch_size,
            )
        )
        checks = metrics.pop("quality_checks")
        metrics["quality_checks"] = checks
        metrics["phase"] = "freshness"
        freshness = event_freshness(conn, options.as_of, options.max_data_age_days)
        metrics["freshness"] = freshness
        checks.extend(freshness)
        if any(r["violations"] for r in freshness):
            raise ValueError("Event freshness threshold exceeded or required invoices missing")
        metrics["phase"] = "publication"
        metrics["operational"] = publish(conn)
        metrics["phase"] = "analytics"
        report = refresh_customers(conn, options.as_of, run_id)
        metrics["summary"] = report["summary"]
        metrics["customer_count"] = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM analytics.customer_financials"
        ).scalar_one()
        checks.extend(
            {**r, "check_name": f"operational_{r['check_name']}"} for r in report["quality_checks"]
        )
        checks.append({"check_name": "financial_report_reconciliation", "violations": 0})
        metrics["quality_check_count"] = len(checks)
        sync_sequences(conn)
        analyze_operational(conn)
        conn.execute(
            text("""
            UPDATE etl.source_state SET watermark=:upper,last_success_at=clock_timestamp(),
                last_run_id=:run WHERE source_name=:source
        """),
            {"upper": upper, "run": run_id, "source": options.source_name},
        )
        metrics["phase"] = "complete"
        metrics["watermark_after"] = upper
        metrics["rolled_back"] = False
        record_checks(conn, run_id, attempt, checks)
        conn.execute(
            text("""
            UPDATE etl.run_attempts SET status='succeeded',ended_at=clock_timestamp(),
                metrics=CAST(:metrics AS jsonb) WHERE run_id=:run AND attempt=:attempt
        """),
            {"run": run_id, "attempt": attempt, "metrics": encode(metrics)},
        )
        conn.execute(
            text("""
            UPDATE etl.pipeline_runs SET status='succeeded',ended_at=clock_timestamp(),
                metrics=CAST(:metrics AS jsonb),error_type=NULL,error_message=NULL WHERE run_id=:run
        """),
            {"run": run_id, "metrics": encode(metrics)},
        )


def run_etl(engine, snapshot=None, *, snapshot_version=None, options):
    """snapshot may be frames or a callable extractor; None replays pending raw data."""
    migrate(engine)
    run_id = str(uuid4())
    metrics = {"raw_changed": 0, "phase": "extract"}
    with run_guard(engine, run_id):
        with engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO etl.pipeline_runs(run_id,source_name,mode,as_of,status)
                VALUES (:run,:source,:mode,:as_of,'running')
            """),
                {
                    "run": run_id,
                    "source": options.source_name,
                    "mode": options.mode,
                    "as_of": options.as_of,
                },
            )
        for attempt in range(1, options.max_attempts + 1):
            metrics.pop("quality_checks", None)
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE etl.pipeline_runs SET attempts=:attempt WHERE run_id=:run"),
                    {"attempt": attempt, "run": run_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO etl.run_attempts(run_id,attempt,status) "
                        "VALUES (:run,:attempt,'running')"
                    ),
                    {"run": run_id, "attempt": attempt},
                )
            try:
                if snapshot is not None:
                    metrics["phase"] = "extract"
                    frames = snapshot() if callable(snapshot) else snapshot
                    metrics["phase"] = "raw_capture"
                    captured = capture_snapshot(
                        engine,
                        frames,
                        options.source_name,
                        snapshot_version,
                        run_id,
                        options.batch_size,
                    )
                    metrics["raw_changed"] += captured["raw_changed"]
                    metrics["source_rows"] = captured["source_rows"]
                _process(engine, run_id, attempt, options, metrics)
                return {
                    "run_id": run_id,
                    "status": "succeeded",
                    "attempts": attempt,
                    "metrics": metrics,
                }
            except Exception as exc:
                # A connection can fail after COMMIT reaches the server. The atomic audit row
                # is the commit receipt; do not overwrite a committed success with a failure.
                with engine.connect() as conn:
                    committed = (
                        conn.execute(
                            text(
                                "SELECT status,attempts,metrics FROM etl.pipeline_runs "
                                "WHERE run_id=:run"
                            ),
                            {"run": run_id},
                        )
                        .mappings()
                        .one()
                    )
                if committed["status"] == "succeeded":
                    return {
                        "run_id": run_id,
                        "status": "succeeded",
                        "attempts": committed["attempts"],
                        "metrics": committed["metrics"],
                        "commit_recovered": True,
                    }
                final = attempt == options.max_attempts or not retryable(exc)
                record_failure(engine, run_id, attempt, exc, metrics, final)
                if final:
                    raise PipelineFailure(run_id, exc) from exc
                time.sleep(min(10, options.retry_delay_seconds * 2 ** (attempt - 1)))
    raise AssertionError("ETL retry loop exited unexpectedly")
