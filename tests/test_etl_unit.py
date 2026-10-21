from datetime import date

import pytest
from sqlalchemy.exc import OperationalError

from revenue_leakage.etl import ETLOptions
from revenue_leakage.normalization import normalize_record
from revenue_leakage.run_log import error_details, retryable
from revenue_leakage.source import prepare_snapshot


def test_financial_normalization_is_lossless():
    row = normalize_record(
        "pricing_plans",
        {
            "id": "1",
            "name": "  Growth  ",
            "currency": "usd",
            "seat_price_cents": "9007199254740993",
            "usage_price_cents": "10.0",
        },
    )
    assert row == {
        "id": 1,
        "name": "Growth",
        "currency": "USD",
        "seat_price_cents": 9007199254740993,
        "usage_price_cents": 10,
    }
    assert isinstance(row["seat_price_cents"], int)


@pytest.mark.parametrize("value", [None, "", "1.5", "NaN", "Infinity", str(2**63), True])
def test_invalid_financial_values(value):
    with pytest.raises(ValueError):
        normalize_record(
            "pricing_plans",
            {
                "id": "1",
                "name": "Test",
                "currency": "USD",
                "seat_price_cents": value,
                "usage_price_cents": "10",
            },
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_name": "not/a/source"},
        {"mode": "merge"},
        {"batch_size": 0},
        {"max_attempts": 6},
        {"max_data_age_days": -1},
        {"retry_delay_seconds": float("nan")},
        {"as_of": None},
    ],
)
def test_etl_option_validation(kwargs):
    with pytest.raises(ValueError):
        ETLOptions(**{"as_of": date(2024, 3, 31), **kwargs})


def test_snapshot_hash_ignores_order_and_never_uses_truth(financial_case):
    first = prepare_snapshot(financial_case)
    shuffled = {n: f.iloc[::-1] for n, f in financial_case.items()}
    assert first == prepare_snapshot(shuffled)
    bad = dict(financial_case)
    bad["leakage_labels"] = financial_case["refunds"]
    with pytest.raises(ValueError, match="exactly the seven"):
        prepare_snapshot(bad)


def test_retry_classification_and_safe_database_error():
    class SerializationFailure(Exception):
        sqlstate = "40001"

    error = OperationalError("SECRET SQL", {"password": "SECRET"}, SerializationFailure())
    assert retryable(error)
    assert "SECRET" not in str(error_details(error))
    assert not retryable(ValueError("Invalid date"))
    assert not retryable(OperationalError("sql", {}, Exception("permanent")))
    assert error_details(ValueError("Invalid date"))["error_message"] == "Invalid date"


def test_snapshot_parser_uses_exact_verified_bytes(tmp_path, financial_case, monkeypatch):
    import hashlib
    import json
    from pathlib import Path

    from revenue_leakage.source import read_snapshot

    (tmp_path / "operational").mkdir()
    hashes = {}
    for name, frame in financial_case.items():
        target = tmp_path / "operational" / f"{name}.csv"
        frame.to_csv(target, index=False)
        hashes[f"operational/{name}.csv"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "sha256": hashes,
                "row_counts": {n: len(f) for n, f in financial_case.items()},
            }
        )
    )
    original = Path.read_bytes

    def change_after_read(path):
        content = original(path)
        if path.name == "customers.csv":
            path.write_text("corrupted after verification")
        return content

    monkeypatch.setattr(Path, "read_bytes", change_after_read)
    frames = read_snapshot(tmp_path)
    assert frames["customers"].loc[0, "name"] == "Example Co"
