"""Incremental typed staging, financial reconciliation, and atomic publication."""

import pandas as pd
from sqlalchemy import text

from revenue_leakage.analytics import query_sql, validate_analytics
from revenue_leakage.normalization import normalize_record
from revenue_leakage.quality import validate
from revenue_leakage.schema import TABLES


def stage_changes(conn, source, lower, upper, mode, batch_size):
    if mode == "full":
        names = ",".join(f'staging."{name}"' for name in TABLES)
        conn.exec_driver_sql(f"TRUNCATE {names}")
        selected = (
            conn.execute(
                text("""
            SELECT c.table_name,c.entity_id,c.payload FROM raw.current_records r
            JOIN raw.changes c ON c.change_id=r.change_id WHERE r.source_name=:source
        """),
                {"source": source},
            )
            .mappings()
            .all()
        )
    else:
        selected = (
            conn.execute(
                text("""
            SELECT DISTINCT ON (table_name,entity_id) table_name,entity_id,payload
            FROM raw.changes WHERE source_name=:source AND change_id>:lower AND change_id<=:upper
            ORDER BY table_name,entity_id,change_id DESC
        """),
                {"source": source, "lower": lower, "upper": upper},
            )
            .mappings()
            .all()
        )
    grouped = {name: [] for name in TABLES}
    for row in selected:
        grouped[row["table_name"]].append(normalize_record(row["table_name"], row["payload"]))
    for name, table in TABLES.items():
        columns = ",".join(f'"{c.name}"' for c in table.columns)
        binds = ",".join(f":{c.name}" for c in table.columns)
        updates = ",".join(
            f'"{c.name}"=EXCLUDED."{c.name}"' for c in table.columns if c.name != "id"
        )
        statement = text(
            f'INSERT INTO staging."{name}" ({columns}) VALUES ({binds}) '
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        records = grouped[name]
        for offset in range(0, len(records), batch_size):
            conn.execute(statement, records[offset : offset + batch_size])
    # Ledger amounts, not stored source status, determine all-time invoice state.
    derived = conn.exec_driver_sql("""
        WITH receipts AS (SELECT invoice_id,SUM(amount_cents) AS amount
                          FROM staging.payments GROUP BY invoice_id),
        expected AS (
            SELECT i.id,CASE WHEN COALESCE(p.amount,0)=i.total_cents THEN 'paid'
                            WHEN COALESCE(p.amount,0)=0 THEN 'unpaid' ELSE 'partial' END AS status
            FROM staging.invoices i LEFT JOIN receipts p ON p.invoice_id=i.id
        )
        UPDATE staging.invoices i SET status=e.status FROM expected e
        WHERE i.id=e.id AND i.status IS DISTINCT FROM e.status
    """).rowcount
    frames = {}
    for name, table in TABLES.items():
        columns = ",".join(f'"{c.name}"' for c in table.columns)
        frames[name] = pd.read_sql_query(text(f'SELECT {columns} FROM staging."{name}"'), conn)
    validate(frames)
    checks = [
        dict(r)
        for r in conn.execute(
            text(
                query_sql("quality").replace(
                    "public.",
                    "staging.",
                )
            )
        ).mappings()
    ]
    if any(r["violations"] for r in checks):
        raise ValueError("Staging SQL quality checks failed")
    checks.append({"check_name": "normalized_schema_and_business_rules", "violations": 0})
    return {
        "staged_rows": {n: len(v) for n, v in grouped.items()},
        "status_corrections": derived,
        "stage_counts": {n: len(f) for n, f in frames.items()},
        "quality_checks": checks,
    }


def publish(conn):
    """Upsert corrected records while retaining unchanged operational rows and keys."""
    stats = {}
    for name, table in TABLES.items():
        columns = ",".join(f'"{c.name}"' for c in table.columns)
        left = ",".join(f'p."{c.name}"' for c in table.columns)
        right = ",".join(f's."{c.name}"' for c in table.columns)
        counts = (
            conn.exec_driver_sql(
                f"SELECT COUNT(*) FILTER(WHERE p.id IS NULL) AS inserted, "
                f"COUNT(*) FILTER(WHERE p.id IS NOT NULL AND ROW({left}) "
                f"IS DISTINCT FROM ROW({right})) AS updated "
                f'FROM staging."{name}" s LEFT JOIN public."{name}" p USING(id)'
            )
            .mappings()
            .one()
        )
        updates = ",".join(
            f'"{c.name}"=EXCLUDED."{c.name}"' for c in table.columns if c.name != "id"
        )
        conn.exec_driver_sql(
            f'INSERT INTO public."{name}" ({columns}) SELECT {right} FROM staging."{name}" s '
            f'LEFT JOIN public."{name}" p USING(id) '
            f"WHERE p.id IS NULL OR ROW({left}) IS DISTINCT FROM ROW({right}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        # Extra public records would make full and incremental outcomes diverge.
        extra = conn.exec_driver_sql(
            f'SELECT COUNT(*) FROM public."{name}" p LEFT JOIN staging."{name}" s USING(id) '
            "WHERE s.id IS NULL"
        ).scalar_one()
        if extra:
            raise ValueError(f"{name}: operational rows absent from authoritative source")
        stats[name] = dict(counts)
    return stats


def refresh_customers(conn, as_of, run_id):
    conn.exec_driver_sql("DELETE FROM analytics.customer_financials")
    conn.execute(
        text("""
        INSERT INTO analytics.customer_financials (
            customer_id,as_of,invoice_count,billed_cents,collected_cents,settled_cents,
            refunded_cents,outstanding_cents,net_cash_cents,run_id
        )
        SELECT c.id,:as_of,COUNT(b.invoice_id),COALESCE(SUM(b.billed_cents),0),
               COALESCE(SUM(b.collected_cents),0),COALESCE(SUM(b.settled_cents),0),
               COALESCE(SUM(b.refunded_cents),0),COALESCE(SUM(b.outstanding_cents),0),
               COALESCE(SUM(b.net_cash_cents),0),:run
        FROM public.customers c LEFT JOIN analytics.invoice_balances(CAST(:as_of AS date)) b
          ON b.customer_id=c.id WHERE c.created_on<=:as_of GROUP BY c.id
    """),
        {"as_of": as_of, "run": run_id},
    )
    report = validate_analytics(conn, as_of)
    customer_totals = (
        conn.exec_driver_sql("""
        SELECT COALESCE(SUM(billed_cents),0) AS billed_cents,
               COALESCE(SUM(collected_cents),0) AS collected_cents,
               COALESCE(SUM(refunded_cents),0) AS refunded_cents,
               COALESCE(SUM(outstanding_cents),0) AS outstanding_cents,
               COALESCE(SUM(net_cash_cents),0) AS net_cash_cents
        FROM analytics.customer_financials
    """)
        .mappings()
        .one()
    )
    if any(value != report["summary"][field] for field, value in customer_totals.items()):
        raise ValueError("Published customer summary does not reconcile")
    return report
