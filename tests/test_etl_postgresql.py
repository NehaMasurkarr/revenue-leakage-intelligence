from copy import deepcopy
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import revenue_leakage.etl as etl_module
from revenue_leakage.etl import ETLOptions, PipelineFailure, run_etl
from revenue_leakage.freshness import freshness_status
from revenue_leakage.run_log import recover_run, run_guard
from revenue_leakage.schema import TABLES
from revenue_leakage.storage import load_operational


@pytest.fixture
def options():
    return ETLOptions(as_of=date(2024, 3, 31), max_data_age_days=90, retry_delay_seconds=0)


def table_state(engine):
    with engine.connect() as conn:
        return {
            name: [
                tuple(r) for r in conn.exec_driver_sql(f'SELECT * FROM public."{name}" ORDER BY id')
            ]
            for name in TABLES
        }


def late_snapshot(frames):
    result = deepcopy(frames)
    # New receipt has an old business date; the source invoice status remains stale.
    result["payments"].loc[len(result["payments"])] = [
        4,
        2,
        date(2024, 2, 20),
        3400,
        3380,
        "late-txn",
    ]
    # A correction to an already-ingested payment must also propagate.
    result["payments"].loc[0, "settled_cents"] = 3950
    result["customers"].loc[0, "name"] = "Corrected Company"
    return result


def test_full_incremental_and_replay_are_idempotent(pg_engine, financial_case, options):
    full = run_etl(
        pg_engine, financial_case, snapshot_version=1, options=replace(options, mode="full")
    )
    assert full["metrics"]["raw_changed"] == sum(len(f) for f in financial_case.values())
    assert full["metrics"]["summary"]["billed_cents"] == 24400
    initial = table_state(pg_engine)
    replay = run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    assert replay["metrics"]["raw_changed"] == 0
    assert sum(replay["metrics"]["staged_rows"].values()) == 0
    assert all(
        v == {"inserted": 0, "updated": 0} for v in replay["metrics"]["operational"].values()
    )
    assert table_state(pg_engine) == initial
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM raw.deliveries")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM etl.quality_results")).scalar_one() == 38
        audit = (
            conn.execute(
                text("SELECT * FROM etl.pipeline_runs WHERE run_id=:run"), {"run": full["run_id"]}
            )
            .mappings()
            .one()
        )
        assert audit["status"] == "succeeded" and audit["ended_at"] >= audit["started_at"]
        assert audit["attempts"] == 1
        assert audit["metrics"]["quality_check_count"] == 19


@pytest.mark.parametrize("empty_target", [False, True])
def test_late_transactions_and_updates_equal_full_rebuild(
    pg_engine,
    financial_case,
    options,
    empty_target,
):
    run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    changed = late_snapshot(financial_case)
    inc = run_etl(pg_engine, changed, snapshot_version=2, options=options)
    assert inc["metrics"]["raw_changed"] == 3
    assert inc["metrics"]["operational"]["payments"] == {"inserted": 1, "updated": 1}
    assert inc["metrics"]["operational"]["invoices"]["updated"] == 1
    assert inc["metrics"]["status_corrections"] == 1
    assert inc["metrics"]["summary"]["collected_cents"] == 15400
    incremental = table_state(pg_engine)
    if empty_target:
        with pg_engine.begin() as conn:
            names = ",".join(f'public."{name}"' for name in TABLES)
            conn.exec_driver_sql("TRUNCATE analytics.customer_financials," + names)
    full = run_etl(pg_engine, None, options=replace(options, mode="full"))
    assert table_state(pg_engine) == incremental
    assert full["metrics"]["summary"] == inc["metrics"]["summary"]
    for name, stats in full["metrics"]["operational"].items():
        assert stats == {"inserted": len(changed[name]) if empty_target else 0, "updated": 0}
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM invoices WHERE id=2")).scalar_one() == "paid"
        assert (
            conn.execute(
                text(
                    "SELECT collected_cents FROM analytics.customer_financials WHERE customer_id=1"
                )
            ).scalar_one()
            == 15400
        )


def test_phase_two_database_adoption(pg_engine, financial_case, options):
    load_operational(pg_engine, financial_case)
    original = table_state(pg_engine)
    adopted = run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    assert table_state(pg_engine) == original
    assert all(
        v == {"inserted": 0, "updated": 0} for v in adopted["metrics"]["operational"].values()
    )


@pytest.mark.parametrize(
    "damage",
    [
        "null",
        "duplicate",
        "orphan",
        "negative",
        "fractional",
        "line_math",
        "invoice_math",
        "date",
        "status",
        "overpayment",
        "refund_limit",
        "currency",
    ],
)
def test_bad_data_is_audited_and_does_not_publish(pg_engine, financial_case, options, damage):
    bad = deepcopy(financial_case)
    if damage == "null":
        bad["customers"].loc[0, "name"] = None
    elif damage == "duplicate":
        bad["customers"].loc[1] = bad["customers"].loc[0]
    elif damage == "orphan":
        bad["payments"].loc[0, "invoice_id"] = 999
    elif damage == "negative":
        bad["payments"].loc[0, "amount_cents"] = -1
    elif damage == "fractional":
        bad["payments"] = bad["payments"].astype({"amount_cents": object})
        bad["payments"].loc[0, "amount_cents"] = "1.5"
    elif damage == "line_math":
        bad["invoice_line_items"].loc[0, "amount_cents"] += 1
    elif damage == "invoice_math":
        bad["invoices"].loc[0, "total_cents"] += 1
    elif damage == "date":
        bad["payments"].loc[0, "paid_on"] = date(2000, 1, 1)
    elif damage == "status":
        bad["invoices"].loc[0, "status"] = "typo"
    elif damage == "overpayment":
        bad["payments"].loc[0, "amount_cents"] = 999999
    elif damage == "refund_limit":
        bad["refunds"].loc[0, "amount_cents"] = 999999
    else:
        bad["invoices"].loc[0, "currency"] = "EUR"
    with pytest.raises(PipelineFailure) as failure:
        run_etl(pg_engine, bad, snapshot_version=1, options=options)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM invoices")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM staging.invoices")).scalar_one() == 0
        assert (
            conn.execute(
                text("SELECT COALESCE(MAX(watermark),0) FROM etl.source_state")
            ).scalar_one()
            == 0
        )
        run = (
            conn.execute(
                text("SELECT * FROM etl.pipeline_runs WHERE run_id=:run"),
                {"run": failure.value.run_id},
            )
            .mappings()
            .one()
        )
        assert run["status"] == "failed" and run["attempts"] == 1
        assert run["error_type"] and run["error_message"] and run["ended_at"]
        assert (
            conn.execute(text("SELECT sum(violations) FROM etl.quality_results")).scalar_one() == 1
        )
        if damage != "duplicate":
            assert conn.execute(text("SELECT count(*) FROM raw.changes")).scalar_one() > 0


def test_correction_recovers_durable_failed_raw(pg_engine, financial_case, options):
    bad = deepcopy(financial_case)
    bad["payments"].loc[0, "amount_cents"] = -1
    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, bad, snapshot_version=1, options=options)
    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, None, options=options)
    success = run_etl(pg_engine, financial_case, snapshot_version=2, options=options)
    assert success["metrics"]["raw_changed"] == 1
    assert success["metrics"]["summary"]["collected_cents"] == 12000
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM raw.changes WHERE table_name='payments' AND entity_id=1")
            ).scalar_one()
            == 2
        )


def test_transient_failure_rolls_back_and_retries(pg_engine, financial_case, options, monkeypatch):
    original = etl_module.publish
    calls = []

    class SerializationFailure(Exception):
        sqlstate = "40001"

    def transient(conn):
        result = original(conn)
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("injected", {}, SerializationFailure())
        return result

    monkeypatch.setattr(etl_module, "publish", transient)
    result = run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    assert result["attempts"] == 2
    assert result["metrics"]["raw_changed"] == sum(len(f) for f in financial_case.values())
    with pg_engine.connect() as conn:
        assert conn.execute(
            text("SELECT status FROM etl.run_attempts ORDER BY attempt")
        ).scalars().all() == ["failed", "succeeded"]
        assert conn.execute(text("SELECT count(*) FROM raw.deliveries")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM invoices")).scalar_one() == 3


def test_retry_exhaustion_preserves_checkpoint_and_can_replay(
    pg_engine, financial_case, options, monkeypatch
):
    original = etl_module.publish

    class LockFailure(Exception):
        sqlstate = "55P03"

    def always_fail(conn):
        original(conn)
        raise OperationalError("injected", {}, LockFailure())

    monkeypatch.setattr(etl_module, "publish", always_fail)
    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT attempts FROM etl.pipeline_runs")).scalar_one() == 3
        assert conn.execute(text("SELECT watermark FROM etl.source_state")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM invoices")).scalar_one() == 0
    monkeypatch.setattr(etl_module, "publish", original)
    recovered = run_etl(pg_engine, options=options)
    assert recovered["status"] == "succeeded"


@pytest.mark.parametrize("scenario", ["stale", "conflict", "omitted", "namespace"])
def test_source_ordering_and_completeness_guards(pg_engine, financial_case, options, scenario):
    run_etl(pg_engine, financial_case, snapshot_version=2, options=options)
    frames = deepcopy(financial_case)
    version = 3
    new_options = options
    if scenario == "stale":
        version = 1
    elif scenario == "conflict":
        version = 2
        frames["customers"].loc[0, "name"] = "Conflicting"
    elif scenario == "omitted":
        frames["refunds"] = frames["refunds"].iloc[:1]
    else:
        new_options = replace(options, source_name="different")
    before = table_state(pg_engine)
    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, frames, snapshot_version=version, options=new_options)
    assert table_state(pg_engine) == before


def test_event_and_wall_clock_freshness(pg_engine, financial_case, options):
    run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    with pg_engine.connect() as conn:
        assert freshness_status(conn, options.as_of, 90)["status"] == "passed"
        assert freshness_status(conn, date(2025, 1, 1), 90)["status"] == "failed"
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("UPDATE etl.source_state SET last_success_at=now()-interval '2 days'")
    with pg_engine.connect() as conn:
        assert freshness_status(conn, options.as_of, 90)["status"] == "failed"
    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, None, options=replace(options, as_of=date(2025, 1, 1)))


def test_abandoned_recovery_respects_live_run_guard(pg_engine, financial_case, options):
    run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    run_id = str(uuid4())
    with pg_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO etl.pipeline_runs(run_id,source_name,mode,as_of,status)
            VALUES (:run,'financials','incremental','2024-03-31','running')
        """),
            {"run": run_id},
        )
        conn.execute(
            text("INSERT INTO etl.run_attempts(run_id,attempt,status) VALUES (:run,1,'running')"),
            {"run": run_id},
        )
    with run_guard(pg_engine, run_id), pytest.raises(ValueError, match="still active"):
        recover_run(pg_engine, run_id)
    assert recover_run(pg_engine, run_id)["status"] == "abandoned"
    with pytest.raises(ValueError, match="Only an abandoned"):
        recover_run(pg_engine, run_id)


def test_extract_failure_is_audited(pg_engine, options):
    def missing_source():
        raise FileNotFoundError("missing source")

    with pytest.raises(PipelineFailure):
        run_etl(pg_engine, missing_source, snapshot_version=1, options=options)
    with pg_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT error_type FROM etl.pipeline_runs")).scalar_one()
            == "FileNotFoundError"
        )


def test_lost_commit_acknowledgement_keeps_success(pg_engine, financial_case, options, monkeypatch):
    original = etl_module._process

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OperationalError("Lost acknowledgement", {}, Exception(), connection_invalidated=True)

    monkeypatch.setattr(etl_module, "_process", lost_ack)
    result = run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    assert result["status"] == "succeeded" and result["commit_recovered"]
    with pg_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT status FROM etl.pipeline_runs")).scalar_one() == "succeeded"
        )
        assert conn.execute(text("SELECT count(*) FROM etl.run_attempts")).scalar_one() == 1


def test_same_snapshot_can_run_concurrently(pg_engine, financial_case, options):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_etl, pg_engine, financial_case, snapshot_version=1, options=options)
            for _ in range(2)
        ]
        results = [f.result() for f in futures]
    assert all(r["status"] == "succeeded" for r in results)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM raw.deliveries")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM invoices")).scalar_one() == 3


def test_empty_optional_transaction_histories_are_not_stale(pg_engine, financial_case, options):
    financial_case["payments"] = financial_case["payments"].iloc[:0]
    financial_case["refunds"] = financial_case["refunds"].iloc[:0]
    result = run_etl(pg_engine, financial_case, snapshot_version=1, options=options)
    assert result["metrics"]["summary"]["collected_cents"] == 0
    assert result["metrics"]["summary"]["outstanding_cents"] == 24400
    assert result["metrics"]["status_corrections"] == 2
    optional = result["metrics"]["freshness"][1:]
    assert all(
        r["violations"] == 0 and r["details"]["state"] == "missing_optional" for r in optional
    )
