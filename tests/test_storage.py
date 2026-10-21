import os

import pandas as pd
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from revenue_leakage.generator import Config, generate
from revenue_leakage.schema import customers, invoices, metadata
from revenue_leakage.storage import (
    engine_for,
    load_operational,
    load_truth,
    read_dataset,
    write_dataset,
)


def test_csv_roundtrip_and_checksum(tmp_path, sample):
    frames, truth = sample
    manifest = write_dataset(
        tmp_path, frames, truth, Config(customers=80, invoices=400, leakage_rate=0.25)
    )
    loaded, labels = read_dataset(tmp_path)
    for name in frames:
        pd.testing.assert_frame_equal(frames[name], loaded[name], check_dtype=False)
    pd.testing.assert_frame_equal(truth, labels, check_dtype=False)
    assert manifest["label_count"] == 100
    assert not (tmp_path / "operational" / "leakage_labels.csv").exists()
    with pytest.raises(FileExistsError):
        write_dataset(tmp_path, frames, truth, Config())
    path = tmp_path / "operational" / "customers.csv"
    path.write_text(path.read_text() + "corruption")
    with pytest.raises(ValueError, match="Checksum"):
        read_dataset(tmp_path)


def test_empty_tables_roundtrip(tmp_path):
    config = Config(customers=1, invoices=1, leakage_rate=0)
    frames, truth = generate(config)
    write_dataset(tmp_path, frames, truth, config)
    loaded, labels = read_dataset(tmp_path)
    assert labels.empty
    assert loaded["refunds"].empty


def exercise_database(engine, sample):
    frames, _ = sample
    load_operational(engine, frames)
    assert "leakage_labels" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert len(connection.execute(select(invoices)).all()) == 400
    with pytest.raises(ValueError, match="not empty"):
        load_operational(engine, frames)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(customers.delete())
    record = frames["invoices"].iloc[0].to_dict()
    record.update(id=999999, contract_id=999999)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(invoices.insert(), record)
    record.update(contract_id=1, total_cents=-1)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(invoices.insert(), record)


def test_database_constraints(sample):
    engine = engine_for("sqlite://")
    try:
        exercise_database(engine, sample)
    finally:
        engine.dispose()


def test_truth_separate_database(sample):
    engine = engine_for("sqlite://")
    try:
        load_truth(engine, sample[1])
        assert inspect(engine).get_table_names() == ["leakage_labels"]
        with pytest.raises(ValueError):
            load_truth(engine, sample[1])
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_postgresql(sample):
    """Use only a dedicated disposable test database."""
    engine = engine_for(os.environ["TEST_DATABASE_URL"])
    assert engine.dialect.name == "postgresql"
    assert not set(inspect(engine).get_table_names()) & set(metadata.tables), (
        "Integration test requires an empty dedicated database"
    )
    try:
        exercise_database(engine, sample)
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_database_isolation(sample):
    engine = engine_for("sqlite://")
    try:
        load_operational(engine, sample[0])
        with pytest.raises(ValueError, match="separate database"):
            load_truth(engine, sample[1])
    finally:
        engine.dispose()
    engine = engine_for("sqlite://")
    try:
        load_truth(engine, sample[1])
        with pytest.raises(ValueError, match="must not contain ground truth"):
            load_operational(engine, sample[0])
    finally:
        engine.dispose()
