from concurrent.futures import ThreadPoolExecutor
from datetime import date
from importlib.resources import files

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from revenue_leakage.analytics import QUERIES, explain, query, validate_analytics
from revenue_leakage.migrations import migrate
from revenue_leakage.schema import metadata
from revenue_leakage.storage import load_operational


def test_fresh_migrations_and_replay(pg_engine):
    assert len(migrate(pg_engine)) == 3
    assert migrate(pg_engine) == []
    inspector = inspect(pg_engine)
    assert set(metadata.tables) <= set(inspector.get_table_names())
    assert "invoice_financials" in inspector.get_view_names(schema="analytics")
    indexes = {i["name"] for i in inspector.get_indexes("invoices")}
    assert {"ix_invoices_issued_cover", "ix_invoices_due"} <= indexes
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET checksum = repeat('0', 64)"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        migrate(pg_engine)


def test_phase_one_adoption_preserves_records(pg_engine, sample):
    metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        for table in metadata.sorted_tables:
            rows = sample[0][table.name].to_dict("records")
            if rows:
                conn.execute(table.insert(), rows)
    assert len(migrate(pg_engine)) == 3
    result = load_operational(pg_engine, sample[0])
    assert all(t["inserted"] == 0 for t in result["tables"].values())


def test_partial_schema_rejected_transactionally(pg_engine):
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE customers(id integer PRIMARY KEY)")
    with pytest.raises(ValueError, match="Partial operational schema"):
        migrate(pg_engine)
    assert "schema_migrations" not in inspect(pg_engine).get_table_names()


def test_incompatible_adoption_rejected(pg_engine):
    metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE customers DROP CONSTRAINT customers_email_key")
    with pytest.raises(ValueError, match="Missing unique constraint"):
        migrate(pg_engine)


def test_idempotence_append_sequences_and_conflict_rollback(pg_engine, sample):
    frames = sample[0]
    first = load_operational(pg_engine, frames, batch_size=17)
    assert first["tables"]["invoices"]["inserted"] == 400
    second = load_operational(pg_engine, frames)
    assert first["fingerprint"] == second["fingerprint"]
    assert all(t["inserted"] == 0 for t in second["tables"].values())
    changed = {k: v.copy(deep=True) for k, v in frames.items()}
    changed["customers"].loc[len(changed["customers"])] = [
        81,
        "New Customer",
        "new@example.invalid",
        "small_business",
        date(2024, 1, 1),
    ]
    # Force a late conflict after new customer insertion; the complete load must roll back.
    changed["refunds"].loc[0, "request_reference"] = "conflicting-request"
    with pytest.raises(ValueError, match="Conflicting immutable record: refunds"):
        load_operational(pg_engine, changed)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM customers")).scalar_one() == 80
        assert conn.execute(text("SELECT count(*) FROM data_loads")).scalar_one() == 1
    changed["refunds"] = frames["refunds"]
    appended = load_operational(pg_engine, changed)
    assert appended["tables"]["customers"]["inserted"] == 1
    with pg_engine.begin() as conn:
        value = conn.execute(
            text("""
            INSERT INTO customers(name, email, segment, created_on)
            VALUES ('Next', 'next@example.invalid', 'small_business', '2024-01-01') RETURNING id
        """)
        ).scalar_one()
        assert value == 82


def test_concurrent_identical_loads(pg_engine, financial_case):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: load_operational(pg_engine, financial_case), range(2))
        )
    assert sorted(r["tables"]["invoices"]["inserted"] for r in results) == [0, 3]


def test_database_quality_and_constraints(pg_engine, financial_case):
    load_operational(pg_engine, financial_case)
    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.exec_driver_sql("UPDATE payments SET amount_cents = -1 WHERE id = 1")
    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.exec_driver_sql("UPDATE invoices SET contract_id = 999 WHERE id = 1")
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE invoice_line_items SET quantity = 2, amount_cents = 20000 WHERE id = 1"
        )
    with pg_engine.connect() as conn, pytest.raises(ValueError, match="invoice_line_totals"):
        validate_analytics(conn, date(2024, 3, 31))


@pytest.mark.parametrize(
    "as_of,billed,collected,refunded,outstanding",
    [
        (date(2023, 12, 31), 0, 0, 0, 0),
        (date(2024, 1, 31), 9000, 4000, 200, 5000),
        (date(2024, 2, 29), 15400, 12000, 500, 3400),
        (date(2024, 3, 31), 24400, 12000, 500, 12400),
    ],
)
def test_as_of_totals_without_fanout(
    pg_engine, financial_case, as_of, billed, collected, refunded, outstanding
):
    load_operational(pg_engine, financial_case)
    with pg_engine.connect() as conn:
        summary = validate_analytics(conn, as_of)["summary"]
        assert summary["billed_cents"] == billed
        assert summary["collected_cents"] == collected
        assert summary["refunded_cents"] == refunded
        assert summary["outstanding_cents"] == outstanding
        assert summary["reconciliation_gap_cents"] == (100 if collected else 0)
        assert summary["net_cash_cents"] == collected - refunded - (100 if collected else 0)
        trends = query(conn, "customer_trends", as_of)
        for field in ("billed_cents", "collected_cents", "settled_cents", "refunded_cents"):
            assert sum(r[field] for r in trends) == summary[field]


def test_reports_and_query_plans(pg_engine, financial_case):
    load_operational(pg_engine, financial_case)
    with pg_engine.connect() as conn:
        for name in QUERIES:
            rows = query(conn, name, date(2024, 5, 31))
            assert isinstance(rows, list)
            plan = explain(conn, name, date(2024, 5, 31))
            assert plan[0]["Execution Time"] >= 0
        pricing = query(conn, "pricing", date(2024, 3, 31))
        assert len(pricing) == 1 and pricing[0]["gross_price_gap_cents"] == 2000
        discounts = query(conn, "discounts", date(2024, 3, 31))
        assert sum(r["excessive_discount_cents"] for r in discounts) == 800
        trends = query(conn, "customer_trends", date(2024, 5, 31))
        assert [r["billed_cents"] for r in trends] == [9000, 6400, 9000, 0, 0]
        assert trends[3]["previous_month_billed_cents"] == 9000
        assert trends[4]["trailing_three_month_billed_cents"] == 9000
        assert trends[4]["month_over_month_change_pct"] is None
        repeated = query(conn, "refund_requests", date(2024, 1, 31))
        assert not repeated
        repeated = query(conn, "refund_requests", date(2024, 2, 29))
        assert repeated[0]["payout_count"] == 2
        assert repeated[0]["total_refunded_cents"] == 500
        # The all-time invoice view must also resist split-payment/refund fanout.
        view = (
            conn.execute(text("SELECT * FROM analytics.invoice_financials WHERE invoice_id=1"))
            .mappings()
            .one()
        )
        assert view["collected_cents"] == 9000 and view["refunded_cents"] == 500


@pytest.mark.parametrize(
    "as_of",
    [
        date(2024, 3, 31),
        date(2024, 4, 1),
        date(2024, 4, 30),
        date(2024, 5, 1),
        date(2024, 5, 30),
        date(2024, 6, 30),
    ],
)
def test_aging_boundaries(pg_engine, financial_case, as_of):
    load_operational(pg_engine, financial_case)
    expected = dict.fromkeys(["current", "1-30", "31-60", "61-90", "91+"], 0)
    for due, amount in [(date(2024, 3, 2), 3400), (date(2024, 3, 31), 9000)]:
        days = max(0, (as_of - due).days)
        bucket = (
            "current"
            if days == 0
            else "1-30"
            if days <= 30
            else "31-60"
            if days <= 60
            else "61-90"
            if days <= 90
            else "91+"
        )
        expected[bucket] += amount
    with pg_engine.connect() as conn:
        actual = {r["bucket"]: r["outstanding_cents"] for r in query(conn, "aging", as_of)}
    assert actual == expected


def test_sql_resources_are_operational_only():
    for group in ("queries", "migrations"):
        for resource in files("revenue_leakage").joinpath("sql", group).iterdir():
            if resource.name.endswith(".sql"):
                assert "leakage_labels" not in resource.read_text()
