"""Explicit event freshness relative to report cutoff and ingestion health in wall time."""

from sqlalchemy import text


def event_freshness(conn, as_of, max_age_days):
    checks = []
    for name, field in (
        ("invoices", "issued_on"),
        ("payments", "paid_on"),
        ("refunds", "refunded_on"),
    ):
        latest = conn.execute(
            text(f'SELECT MAX("{field}") FROM staging."{name}" WHERE "{field}"<=:as_of'),
            {"as_of": as_of},
        ).scalar_one()
        age = (as_of - latest).days if latest else None
        violations = int(
            (age is not None and age > max_age_days) or (latest is None and name == "invoices")
        )
        checks.append(
            {
                "check_name": f"freshness_{name}",
                "violations": violations,
                "details": {
                    "latest_event_on": latest,
                    "age_days": age,
                    "max_age_days": max_age_days,
                    "state": "missing_optional" if latest is None else "observed",
                },
            }
        )
    return checks


def freshness_status(conn, as_of, max_age_days, max_run_age_seconds=86400):
    checks = event_freshness(conn, as_of, max_age_days)
    state = (
        conn.execute(
            text("""
        SELECT source_name,snapshot_version,watermark,last_received_at,last_success_at,last_run_id,
            EXTRACT(EPOCH FROM clock_timestamp()-last_received_at) AS source_age_seconds,
            EXTRACT(EPOCH FROM clock_timestamp()-last_success_at) AS pipeline_age_seconds
        FROM etl.source_state
    """)
        )
        .mappings()
        .first()
    )
    healthy = bool(
        state
        and state["last_success_at"]
        and state["last_received_at"]
        and state["pipeline_age_seconds"] <= max_run_age_seconds
        and state["source_age_seconds"] <= max_run_age_seconds
    )
    return {
        "status": "passed" if healthy and not any(c["violations"] for c in checks) else "failed",
        "as_of": as_of,
        "checks": checks,
        "source": dict(state) if state else None,
        "max_run_age_seconds": max_run_age_seconds,
    }
