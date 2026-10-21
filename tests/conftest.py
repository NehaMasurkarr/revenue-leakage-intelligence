import pytest

from revenue_leakage.generator import Config, generate


@pytest.fixture(scope="session")
def sample():
    return generate(Config(customers=80, invoices=400, leakage_rate=0.25))


@pytest.fixture
def pg_engine():
    """Every integration test gets an empty dedicated database; never reuse user data."""
    import os

    from sqlalchemy import inspect

    from revenue_leakage.schema import metadata
    from revenue_leakage.storage import engine_for

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = engine_for(url)
    assert engine.dialect.name == "postgresql"
    assert not inspect(engine).get_table_names(), "Use an empty dedicated test database"
    assert not {"analytics", "raw", "staging", "etl"} & set(inspect(engine).get_schema_names()), (
        "Use an empty test database"
    )
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS analytics CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS staging CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS raw CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS etl CASCADE")
            metadata.drop_all(connection)
            connection.exec_driver_sql("DROP TABLE IF EXISTS public.data_loads")
            connection.exec_driver_sql("DROP TABLE IF EXISTS public.schema_migrations")
        engine.dispose()


@pytest.fixture
def financial_case():
    """Hand-calculated case: split receipts, several refunds, one price/discount variance."""
    from datetime import date

    import pandas as pd

    from revenue_leakage.schema import TABLES

    records = {
        "customers": [
            (1, "Example Co", "finance@example.invalid", "small_business", date(2023, 12, 1))
        ],
        "pricing_plans": [(1, "Test", "USD", 10000, 10)],
        "contracts": [(1, 1, 1, date(2024, 1, 1), date(2024, 12, 31), 1, 1000)],
        "invoices": [
            (1, 1, date(2024, 1, 1), date(2024, 1, 31), "USD", 10000, 1000, 9000, "paid"),
            (2, 1, date(2024, 2, 1), date(2024, 3, 2), "USD", 8000, 1600, 6400, "partial"),
            (3, 1, date(2024, 3, 1), date(2024, 3, 31), "USD", 10000, 1000, 9000, "unpaid"),
        ],
        "invoice_line_items": [
            (1, 1, "subscription", 1, 10000, 10000),
            (2, 2, "subscription", 1, 8000, 8000),
            (3, 3, "subscription", 1, 10000, 10000),
        ],
        "payments": [
            (1, 1, date(2024, 1, 10), 4000, 3900, "txn-a"),
            (2, 1, date(2024, 2, 5), 5000, 5000, "txn-b"),
            (3, 2, date(2024, 2, 15), 3000, 3000, "txn-c"),
        ],
        "refunds": [
            (1, 1, date(2024, 1, 20), 200, "req-a"),
            (2, 1, date(2024, 2, 10), 300, "req-a"),
        ],
    }
    return {
        name: pd.DataFrame(records[name], columns=table.columns.keys())
        for name, table in TABLES.items()
    }
