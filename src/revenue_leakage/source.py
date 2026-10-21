"""Operational snapshot extraction and durable, content-based change capture."""

import hashlib
import json
import re
from datetime import date

import pandas as pd
from sqlalchemy import text

from revenue_leakage.migrations import lock
from revenue_leakage.schema import TABLES


def read_snapshot(root):
    """Reuse hash/count verification while deferring business checks to typed staging."""
    from revenue_leakage.storage import read_operational

    return read_operational(root, raw=True)


def prepare_snapshot(frames):
    if set(frames) != set(TABLES):
        raise ValueError("Snapshot must contain exactly the seven operational tables")
    records = []
    fingerprint = hashlib.sha256()
    counts = {}
    for name in sorted(TABLES):
        frame = frames[name]
        if list(frame.columns) != list(TABLES[name].columns.keys()):
            raise ValueError(f"{name}: source columns do not match")
        seen = set()
        entries = []
        for payload in frame.to_dict("records"):
            # Canonical raw representation keeps text exactly, dates in ISO, integers as strings.
            payload = {
                k: (
                    None
                    if v is None or pd.isna(v)
                    else v.isoformat()
                    if isinstance(v, date)
                    else str(v)
                )
                for k, v in payload.items()
            }
            try:
                entity_id = normalize_record_id(payload["id"])
            except ValueError as exc:
                raise ValueError(f"{name}: invalid source primary key") from exc
            if entity_id in seen:
                raise ValueError(f"{name}: duplicate source primary key")
            seen.add(entity_id)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            entries.append(
                {
                    "table_name": name,
                    "entity_id": entity_id,
                    "payload": encoded,
                    "payload_hash": digest,
                }
            )
        counts[name] = len(entries)
        for entry in sorted(entries, key=lambda e: e["entity_id"]):
            fingerprint.update(f"{name}:{entry['entity_id']}:{entry['payload_hash']}\n".encode())
            records.append(entry)
    return records, fingerprint.hexdigest(), counts


def normalize_record_id(value):
    # Reuse integral bounds/normalization without validating unrelated raw values.
    if value is None or not re.fullmatch(r"[+]?[0-9]+", str(value).strip()):
        raise ValueError("Invalid source ID")
    value = int(value)
    if not 0 < value < 2**31:
        raise ValueError("Invalid source ID")
    return value


def capture_snapshot(engine, frames, source_name, version, run_id, batch_size):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", source_name):
        raise ValueError("Invalid source name")
    if not isinstance(version, int) or isinstance(version, bool) or not 0 < version < 2**63:
        raise ValueError("Snapshot version must be a positive BIGINT")
    records, fingerprint, counts = prepare_snapshot(frames)
    with engine.begin() as conn:
        lock(conn)
        state = conn.execute(text("SELECT * FROM etl.source_state FOR UPDATE")).mappings().first()
        if state and state["source_name"] != source_name:
            raise ValueError("This database is bound to a different source namespace")
        if not state:
            conn.execute(
                text("INSERT INTO etl.source_state(source_name) VALUES (:source)"),
                {"source": source_name},
            )
        elif version < state["snapshot_version"]:
            raise ValueError("Stale snapshot version; source ordering must be monotonic")
        elif version == state["snapshot_version"]:
            if fingerprint != state["snapshot_fingerprint"]:
                raise ValueError("Conflicting payload for an existing snapshot version")
            return {"snapshot_version": version, "raw_changed": 0, "source_rows": counts}
        conn.exec_driver_sql("""
            CREATE TEMP TABLE incoming_raw (
                table_name text, entity_id integer, payload jsonb, payload_hash varchar(64),
                PRIMARY KEY(table_name, entity_id)
            ) ON COMMIT DROP
        """)
        for offset in range(0, len(records), batch_size):
            conn.execute(
                text("""
                INSERT INTO incoming_raw VALUES
                    (:table_name, :entity_id, CAST(:payload AS jsonb), :payload_hash)
            """),
                records[offset : offset + batch_size],
            )
        missing = conn.execute(
            text("""
            SELECT COUNT(*) FROM raw.current_records c LEFT JOIN incoming_raw i
              ON i.table_name = c.table_name AND i.entity_id = c.entity_id
            WHERE c.source_name = :source AND i.entity_id IS NULL
        """),
            {"source": source_name},
        ).scalar_one()
        if missing:
            raise ValueError(f"Complete snapshot omits {missing} existing source records")
        conn.execute(
            text("""
            INSERT INTO raw.deliveries(source_name,snapshot_version,fingerprint,run_id,
                                       row_counts,changed_rows)
            VALUES (:source,:version,:fingerprint,:run,CAST(:counts AS jsonb),0)
        """),
            {
                "source": source_name,
                "version": version,
                "fingerprint": fingerprint,
                "run": run_id,
                "counts": json.dumps(counts),
            },
        )
        changed = conn.execute(
            text("""
            INSERT INTO raw.changes(source_name,snapshot_version,table_name,entity_id,
                                    payload,payload_hash)
            SELECT :source,:version,i.table_name,i.entity_id,i.payload,i.payload_hash
            FROM incoming_raw i LEFT JOIN raw.current_records c
              ON c.source_name=:source AND c.table_name=i.table_name AND c.entity_id=i.entity_id
            WHERE c.payload_hash IS DISTINCT FROM i.payload_hash
        """),
            {"source": source_name, "version": version},
        ).rowcount
        conn.execute(
            text("""
            INSERT INTO raw.current_records(source_name,table_name,entity_id,change_id,payload_hash)
            SELECT source_name,table_name,entity_id,change_id,payload_hash FROM raw.changes
            WHERE source_name=:source AND snapshot_version=:version
            ON CONFLICT (source_name,table_name,entity_id) DO UPDATE
            SET change_id=EXCLUDED.change_id, payload_hash=EXCLUDED.payload_hash
        """),
            {"source": source_name, "version": version},
        )
        conn.execute(
            text("""
            UPDATE raw.deliveries SET changed_rows=:changed
            WHERE source_name=:source AND snapshot_version=:version
        """),
            {"changed": changed, "source": source_name, "version": version},
        )
        conn.execute(
            text("""
            UPDATE etl.source_state SET snapshot_version=:version,
                snapshot_fingerprint=:fingerprint, last_received_at=clock_timestamp()
            WHERE source_name=:source
        """),
            {"version": version, "fingerprint": fingerprint, "source": source_name},
        )
    return {"snapshot_version": version, "raw_changed": changed, "source_rows": counts}
