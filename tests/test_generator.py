from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from revenue_leakage.generator import KINDS, Config, discounted, generate, month_at
from revenue_leakage.quality import QualityError, validate


def test_reproducibility_and_seed_changes(sample):
    config = Config(customers=80, invoices=400, leakage_rate=0.25)
    frames, truth = sample
    again, labels = generate(config)
    for name in frames:
        pd.testing.assert_frame_equal(frames[name], again[name])
    pd.testing.assert_frame_equal(truth, labels)
    changed, _ = generate(replace(config, seed=43))
    assert not changed["invoices"].equals(frames["invoices"])


def test_counts_horizon_and_labels(sample):
    frames, truth = sample
    assert len(frames["customers"]) == 80
    assert len(frames["invoices"]) == 400
    assert len(truth) == 100
    assert set(truth.leakage_type) == set(KINDS)
    assert frames["invoices"].issued_on.min() == date(2024, 1, 1)
    assert frames["invoices"].issued_on.max() == date(2025, 12, 1)
    validate(frames, truth)
    assert not any(
        "leakage" in col or "expected" in col for frame in frames.values() for col in frame.columns
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"customers": 0},
        {"months": 0},
        {"invoices": 1},
        {"invoices": 999999},
        {"leakage_rate": -0.1},
        {"leakage_rate": 1.1},
        {"start": date(2024, 1, 2)},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


@pytest.mark.parametrize("rate", [0, 1])
def test_boundary_datasets(rate):
    frames, truth = generate(Config(customers=1, invoices=24, leakage_rate=rate))
    validate(frames, truth)
    assert len(truth) == int(24 * rate)
    assert len(frames["invoices"]) == 24


def test_rounding_and_calendar():
    assert discounted(101, 5000) == 51
    assert discounted(1, 5000) == 1
    assert month_at(date(2024, 12, 1), 2) == date(2025, 2, 1)


@pytest.mark.parametrize("damage", ["orphan", "subtotal", "payment", "date", "label"])
def test_quality_detects_corruption(sample, damage):
    original, labels = sample
    frames = {k: v.copy(deep=True) for k, v in original.items()}
    truth = labels.copy(deep=True)
    if damage == "orphan":
        frames["invoices"].loc[0, "contract_id"] = 999999
    elif damage == "subtotal":
        frames["invoice_line_items"].loc[0, "amount_cents"] += 1
    elif damage == "payment":
        frames["payments"].loc[0, "amount_cents"] += 100000000
    elif damage == "date":
        frames["payments"].loc[0, "paid_on"] = date(1990, 1, 1)
    else:
        truth.loc[0, "leakage_cents"] += 1
    with pytest.raises(QualityError):
        validate(frames, truth)


def test_business_anomalies_match_truth(sample):
    """Detect billing and settlement deltas independently, including clean invoices."""
    frames, truth = sample
    contracts = frames["contracts"].set_index("id")
    plans = frames["pricing_plans"].set_index("id")
    labeled = truth.set_index("invoice_id")
    lines = {i: g.set_index("kind") for i, g in frames["invoice_line_items"].groupby("invoice_id")}
    for inv in frames["invoices"].itertuples():
        contract = contracts.loc[inv.contract_id]
        plan = plans.loc[contract.plan_id]
        items = lines[inv.id]
        kind = labeled.loc[inv.id].leakage_type if inv.id in labeled.index else None
        if kind == "missed_charge":
            assert "usage" not in items.index
            continue
        expected_subtotal = int(contract.seats) * int(plan.seat_price_cents) + int(
            items.loc["usage"].quantity
        ) * int(plan.usage_price_cents)
        expected = expected_subtotal - discounted(expected_subtotal, int(contract.discount_bps))
        delta = expected - inv.total_cents
        assert (delta > 0) == (kind in ("underbilling", "incorrect_discount"))
        if delta:
            assert delta == labeled.loc[inv.id].leakage_cents
    pay = frames["payments"]
    discrepancies = pay[pay.amount_cents != pay.settled_cents]
    assert set(discrepancies.invoice_id) == set(
        truth[truth.leakage_type == "reconciliation_discrepancy"].invoice_id
    )
    refunds = frames["refunds"]
    duplicates = refunds[refunds.duplicated("request_reference", keep=False)]
    duplicate_invoices = pay[pay.id.isin(duplicates.payment_id)].invoice_id
    assert set(duplicate_invoices) == set(
        truth[truth.leakage_type == "duplicate_refund"].invoice_id
    )
