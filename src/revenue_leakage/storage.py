"""CSV artifacts and transactional database loading with enforced foreign keys."""

import hashlib
import json
from importlib.metadata import version
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


def read_dataset(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    expected = {f"operational/{name}.csv" for name in TABLES} | {
        "ground_truth/leakage_labels.csv",
    }
    if set(manifest["sha256"]) != expected:
        raise ValueError("Manifest file list differs from schema")
    for name, digest in manifest["sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Checksum mismatch: {name}")
    frames = {}
    for name, table in TABLES.items():
        frame = pd.read_csv(root / "operational" / f"{name}.csv")
        for column in table.columns:
            if isinstance(column.type, Date):
                frame[column.name] = pd.to_datetime(frame[column.name]).dt.date
        frames[name] = frame
    truth = pd.read_csv(root / "ground_truth" / "leakage_labels.csv")
    validate(frames, truth)
    return frames, truth


def load_operational(engine, frames):
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
