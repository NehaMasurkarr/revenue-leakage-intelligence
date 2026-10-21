"""CSV artifacts and transactional database loading with enforced foreign keys."""

import hashlib
import json
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import pandas as pd
from sqlalchemy import Date, create_engine, event, inspect, select

from revenue_leakage.quality import validate
from revenue_leakage.schema import TABLES, label_metadata, labels, metadata


def engine_for(url):
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def write_dataset(root, frames, truth, config):
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Output directory must be empty: {root}")
    validate(frames, truth)
    if len(frames["customers"]) != config.customers or len(frames["invoices"]) != config.invoices:
        raise ValueError("Row counts disagree with generation config")
    if len(truth) != int(config.invoices * config.leakage_rate):
        raise ValueError("Label count disagrees with generation config")
    (root / "operational").mkdir(parents=True, exist_ok=True)
    (root / "ground_truth").mkdir()
    hashes = {}
    for folder, tables in (("operational", frames), ("ground_truth", {"leakage_labels": truth})):
        for name, frame in tables.items():
            path = root / folder / f"{name}.csv"
            frame.to_csv(path, index=False, lineterminator="\n")
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "config": config.manifest(),
        "versions": {p: version(p) for p in ("numpy", "scipy", "pandas", "Faker", "SQLAlchemy")},
        "row_counts": {name: len(f) for name, f in frames.items()},
        "label_count": len(truth),
        "leakage_cents": int(truth.leakage_cents.sum()),
        "sha256": hashes,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def read_operational(root, *, raw=False):
    """Read and hash-check only operational CSVs; label files need not exist."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    verified = {}
    for name in TABLES:
        relative = f"operational/{name}.csv"
        expected = manifest["sha256"].get(relative)
        payload = (root / relative).read_bytes()
        if not expected or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch: {relative}")
        verified[name] = payload
    frames = _read_frames(root, raw=raw, verified=verified)
    if not raw:
        validate(frames)
    for name, frame in frames.items():
        if len(frame) != manifest["row_counts"].get(name):
            raise ValueError(f"Row count mismatch: {name}")
    return frames


def _read_frames(root, *, raw=False, verified=None):
    frames = {}
    for name, table in TABLES.items():
        options = {"dtype": str, "keep_default_na": False} if raw else {}
        source = (
            BytesIO(verified[name])
            if verified is not None
            else (root / "operational" / f"{name}.csv")
        )
        frame = pd.read_csv(source, **options)
        for column in table.columns:
            if not raw and isinstance(column.type, Date):
                frame[column.name] = pd.to_datetime(frame[column.name]).dt.date
        frames[name] = frame
    return frames


def read_dataset(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    expected = {f"operational/{name}.csv" for name in TABLES} | {
        "ground_truth/leakage_labels.csv",
    }
    if set(manifest["sha256"]) != expected:
        raise ValueError("Manifest file list differs from schema")
    verified = {}
    for name, digest in manifest["sha256"].items():
        payload = (root / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"Checksum mismatch: {name}")
        verified[name] = payload
    frames = _read_frames(
        root, verified={name: verified[f"operational/{name}.csv"] for name in TABLES}
    )
    truth = pd.read_csv(BytesIO(verified["ground_truth/leakage_labels.csv"]))
    validate(frames, truth)
    return frames, truth


def load_operational(engine, frames, batch_size=1000):
    if engine.dialect.name == "postgresql":
        from revenue_leakage.pipeline import load_postgresql

        return load_postgresql(engine, frames, batch_size)
    validate(frames)
    # No replace/truncate: refuse nonempty databases and preserve SQL constraints.
    with engine.begin() as connection:
        if inspect(connection).has_table("leakage_labels"):
            raise ValueError("Operational database must not contain ground truth")
        metadata.create_all(connection)
        for table in metadata.sorted_tables:
            if connection.execute(select(table.c.id).limit(1)).first():
                raise ValueError(f"Target table is not empty: {table.name}")
        for table in metadata.sorted_tables:
            records = frames[table.name].to_dict("records")
            for start in range(0, len(records), 1000):
                connection.execute(table.insert(), records[start : start + 1000])


def load_truth(engine, truth):
    """Explicit evaluation-only load; caller must use a separate database."""
    with engine.begin() as connection:
        if set(inspect(connection).get_table_names()) & set(TABLES):
            raise ValueError("Ground truth requires a separate database")
        label_metadata.create_all(connection)
        if connection.execute(select(labels.c.id).limit(1)).first():
            raise ValueError("Ground truth target is not empty")
        records = truth.to_dict("records")
        for start in range(0, len(records), 1000):
            connection.execute(labels.insert(), records[start : start + 1000])
