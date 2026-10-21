"""Immutable, idempotent operational ingestion into PostgreSQL."""

import hashlib
import json

from sqlalchemy import text

from revenue_leakage.migrations import migrate_connection
from revenue_leakage.quality import validate
from revenue_leakage.schema import metadata


def load_postgresql(engine, frames, batch_size=1000):
    if not 1 <= batch_size <= 10_000:
        raise ValueError("LOAD_BATCH_SIZE must be between 1 and 10000")
    validate(frames)  # Never read or validate evaluation labels in this path.
    fingerprint = hashlib.sha256()
    for table in metadata.sorted_tables:
        fingerprint.update(table.name.encode())
        fingerprint.update(frames[table.name].sort_values("id").to_csv(index=False).encode())
    result = {"fingerprint": fingerprint.hexdigest(), "tables": {}}
    with engine.begin() as connection:
        migrate_connection(connection)
        # Serialize with both pipeline users and arbitrary concurrent table writers.
        lock_operational(connection)
        for table in metadata.sorted_tables:
            name = table.name  # Fixed schema names only; never external input.
            stage = f"stage_{name}"
            connection.exec_driver_sql(
                f'CREATE TEMP TABLE "{stage}" (LIKE public."{name}" INCLUDING DEFAULTS) '
                "ON COMMIT DROP"
            )
            records = frames[name].to_dict("records")
            columns = ", ".join(f'"{c.name}"' for c in table.columns)
            binds = ", ".join(f":{c.name}" for c in table.columns)
            insert = text(f'INSERT INTO "{stage}" ({columns}) VALUES ({binds})')
            for offset in range(0, len(records), batch_size):
                connection.execute(insert, records[offset : offset + batch_size])
            actual = ", ".join(f't."{c.name}"' for c in table.columns)
            incoming = ", ".join(f's."{c.name}"' for c in table.columns)
            conflict = connection.exec_driver_sql(
                f'SELECT s.id FROM "{stage}" s JOIN public."{name}" t USING (id) '
                f"WHERE ROW({actual}) IS DISTINCT FROM ROW({incoming}) LIMIT 1"
            ).scalar()
            if conflict is not None:
                raise ValueError(f"Conflicting immutable record: {name} id={conflict}")
            inserted = connection.exec_driver_sql(
                f'INSERT INTO public."{name}" ({columns}) SELECT {columns} FROM "{stage}" '
                "ON CONFLICT (id) DO NOTHING"
            ).rowcount
            result["tables"][name] = {"inserted": inserted, "unchanged": len(records) - inserted}
        from revenue_leakage.analytics import check_quality

        check_quality(connection)
        sync_sequences(connection)
        connection.execute(
            text("""
            INSERT INTO data_loads (fingerprint, row_counts)
            VALUES (:fingerprint, CAST(:counts AS jsonb))
            ON CONFLICT (fingerprint) DO NOTHING
        """),
            {
                "fingerprint": result["fingerprint"],
                "counts": json.dumps({name: len(frame) for name, frame in frames.items()}),
            },
        )
        analyze_operational(connection)
    return result


def lock_operational(connection):
    names = ", ".join(f'public."{t.name}"' for t in metadata.sorted_tables)
    connection.exec_driver_sql(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE")


def sync_sequences(connection):
    for table in metadata.sorted_tables:
        name = table.name
        connection.exec_driver_sql(
            f"SELECT setval(pg_get_serial_sequence('public.{name}', 'id'), "
            f'COALESCE((SELECT MAX(id) FROM public."{name}"), 1), '
            f'EXISTS (SELECT 1 FROM public."{name}"))'
        )


def analyze_operational(connection):
    for table in metadata.sorted_tables:
        connection.exec_driver_sql(f'ANALYZE public."{table.name}"')
