"""Operational-only SQL reports, quality assertions, and read-only query plans."""

from sqlalchemy import text

from revenue_leakage.migrations import sql_resource

QUERIES = (
    "summary",
    "monthly",
    "outstanding",
    "aging",
    "reconciliation",
    "pricing",
    "discounts",
    "customer_trends",
    "refund_requests",
)


def query_sql(name):
    if name not in (*QUERIES, "quality"):
        raise ValueError(f"Unknown query: {name}")
    return sql_resource("queries", f"{name}.sql")


def query(connection, name, as_of):
    return [
        dict(row)
        for row in connection.execute(
            text(query_sql(name)),
            {"as_of": as_of},
        ).mappings()
    ]


def check_quality(connection):
    rows = query(connection, "quality", None)
    failed = {r["check_name"]: r["violations"] for r in rows if r["violations"]}
    if failed:
        raise ValueError(f"Database quality checks failed: {failed}")
    return rows


def validate_analytics(connection, as_of):
    """Independent grains reconcile in the same database snapshot."""
    quality = check_quality(connection)
    summary = query(connection, "summary", as_of)[0]
    monthly = query(connection, "monthly", as_of)
    aging = query(connection, "aging", as_of)
    for field in (
        "billed_cents",
        "collected_cents",
        "settled_cents",
        "refunded_cents",
        "net_cash_cents",
        "net_collected_cents",
    ):
        if sum(row[field] for row in monthly) != summary[field]:
            raise ValueError(f"Monthly reconciliation failed: {field}")
    if sum(row["outstanding_cents"] for row in aging) != summary["outstanding_cents"]:
        raise ValueError("Aging reconciliation failed")
    if summary["billed_cents"] - summary["collected_cents"] != summary["outstanding_cents"]:
        raise ValueError("Receivables reconciliation failed")
    direct = (
        connection.execute(
            text("""
        SELECT (SELECT COALESCE(SUM(total_cents), 0) FROM invoices
                WHERE issued_on <= :as_of) AS billed_cents,
               (SELECT COALESCE(SUM(amount_cents), 0) FROM payments
                WHERE paid_on <= :as_of) AS collected_cents,
               (SELECT COALESCE(SUM(settled_cents), 0) FROM payments
                WHERE paid_on <= :as_of) AS settled_cents,
               (SELECT COALESCE(SUM(amount_cents), 0) FROM refunds
                WHERE refunded_on <= :as_of) AS refunded_cents
    """),
            {"as_of": as_of},
        )
        .mappings()
        .one()
    )
    for field, value in direct.items():
        if value != summary[field]:
            raise ValueError(f"Base table reconciliation failed: {field}")
    trend_sql = query_sql("customer_trends").strip().removesuffix(";")
    customer_totals = (
        connection.execute(
            text(
                "SELECT COALESCE(SUM(billed_cents), 0) AS billed_cents, "
                "COALESCE(SUM(collected_cents), 0) AS collected_cents, "
                "COALESCE(SUM(settled_cents), 0) AS settled_cents, "
                "COALESCE(SUM(refunded_cents), 0) AS refunded_cents FROM (" + trend_sql + ") t"
            ),
            {"as_of": as_of},
        )
        .mappings()
        .one()
    )
    for field, value in customer_totals.items():
        if value != summary[field]:
            raise ValueError(f"Customer reconciliation failed: {field}")
    return {
        "as_of": as_of,
        "quality_checks": quality,
        "summary": summary,
        "monthly_rows": len(monthly),
        "status": "passed",
    }


def explain(connection, name, as_of):
    return connection.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query_sql(name)), {"as_of": as_of}
    ).scalar_one()
