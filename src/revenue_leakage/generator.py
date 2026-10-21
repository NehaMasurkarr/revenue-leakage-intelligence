"""Seeded, integer-only financial generation with isolated evaluation truth."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
from faker import Faker
from scipy.stats import poisson

KINDS = (
    "underbilling",
    "incorrect_discount",
    "duplicate_refund",
    "missed_charge",
    "reconciliation_discrepancy",
)


@dataclass(frozen=True)
class Config:
    seed: int = 42
    customers: int = 10_000
    invoices: int = 50_000
    start: date = date(2024, 1, 1)
    months: int = 24
    leakage_rate: float = 0.10

    def __post_init__(self):
        if self.customers < 1 or self.months < 1:
            raise ValueError("customers and months must be positive")
        if not self.customers <= self.invoices <= self.customers * self.months:
            raise ValueError("invoices must be between customers and customers * months")
        if self.start.day != 1:
            raise ValueError("start must be the first of a month")
        if not 0 <= self.leakage_rate <= 1:
            raise ValueError("leakage_rate must be between 0 and 1")

    def manifest(self):
        return {**asdict(self), "start": self.start.isoformat()}


def month_at(start, offset):
    value = start.year * 12 + start.month - 1 + offset
    return date(value // 12, value % 12 + 1, 1)


def discounted(subtotal, bps):
    """Round nonnegative basis-point discounts half up, without floats."""
    return (subtotal * bps + 5000) // 10000


def generate(config=None):
    config = config or Config()
    rng = np.random.default_rng(config.seed)
    fake = Faker("en_US")
    fake.seed_instance(config.seed)
    rows = {
        k: []
        for k in (
            "customers",
            "pricing_plans",
            "contracts",
            "invoices",
            "invoice_line_items",
            "payments",
            "refunds",
        )
    }
    truth = []
    plans = [("Starter", 1900, 12), ("Growth", 4900, 9), ("Enterprise", 9900, 6)]
    for i, (name, seat, usage) in enumerate(plans, 1):
        rows["pricing_plans"].append(
            dict(
                id=i,
                name=name,
                currency="USD",
                seat_price_cents=seat,
                usage_price_cents=usage,
            )
        )
    counts = np.full(config.customers, config.invoices // config.customers, dtype=int)
    counts[: config.invoices % config.customers] += 1
    # Vary contract tenure while preserving the exact requested invoice count.
    for _ in range(config.customers * 3):
        a, b = rng.integers(0, config.customers, size=2)
        if a != b and counts[a] > 1 and counts[b] < config.months:
            counts[a] -= 1
            counts[b] += 1
    affected = rng.choice(
        config.invoices,
        int(config.invoices * config.leakage_rate),
        replace=False,
    )
    events = {int(v) + 1: KINDS[i % len(KINDS)] for i, v in enumerate(affected)}
    cutoff = month_at(config.start, config.months) - timedelta(days=1)
    iid = 0
    for cid, tenure in enumerate(counts, 1):
        start_offset = int(rng.integers(0, config.months - int(tenure) + 1))
        starts = month_at(config.start, start_offset)
        ends = month_at(config.start, start_offset + int(tenure)) - timedelta(days=1)
        plan_id = int(rng.choice([1, 2, 3], p=[0.55, 0.35, 0.10]))
        _, seat_price, usage_price = plans[plan_id - 1]
        seats = int(rng.integers(2, [12, 60, 180][plan_id - 1]))
        bps = int(rng.choice([0, 500, 1000, 1500], p=[0.5, 0.2, 0.2, 0.1]))
        rows["customers"].append(
            dict(
                id=cid,
                name=fake.company(),
                email=f"customer{cid:06d}@example.invalid",
                segment=["small_business", "mid_market", "enterprise"][plan_id - 1],
                created_on=starts - timedelta(days=int(rng.integers(0, 61))),
            )
        )
        rows["contracts"].append(
            dict(
                id=cid,
                customer_id=cid,
                plan_id=plan_id,
                starts_on=starts,
                ends_on=ends,
                seats=seats,
                discount_bps=bps,
            )
        )
        for offset in range(int(tenure)):
            iid += 1
            issued = month_at(starts, offset)
            usage = max(1, int(poisson.rvs(mu=seats * 30, random_state=rng)))
            expected_subtotal = seats * seat_price + usage * usage_price
            expected = expected_subtotal - discounted(expected_subtotal, bps)
            event = events.get(iid)
            actual_seat = seat_price * 80 // 100 if event == "underbilling" else seat_price
            for kind, quantity, price in (
                ("subscription", seats, actual_seat),
                ("usage", usage, usage_price),
            ):
                if event == "missed_charge" and kind == "usage":
                    continue
                rows["invoice_line_items"].append(
                    dict(
                        id=len(rows["invoice_line_items"]) + 1,
                        invoice_id=iid,
                        kind=kind,
                        quantity=quantity,
                        unit_price_cents=price,
                        amount_cents=quantity * price,
                    )
                )
            subtotal = seats * actual_seat
            if event != "missed_charge":
                subtotal += usage * usage_price
            discount = discounted(subtotal, bps + (1000 if event == "incorrect_discount" else 0))
            total = subtotal - discount
            # Duplicate refunds and reconciliation cases require a settled payment.
            status = (
                "paid"
                if event in ("duplicate_refund", "reconciliation_discrepancy")
                else str(rng.choice(["paid", "partial", "unpaid"], p=[0.92, 0.04, 0.04]))
            )
            rows["invoices"].append(
                dict(
                    id=iid,
                    contract_id=cid,
                    issued_on=issued,
                    due_on=issued + timedelta(days=30),
                    currency="USD",
                    subtotal_cents=subtotal,
                    discount_cents=discount,
                    total_cents=total,
                    status=status,
                )
            )
            entity_table, entity_id = "invoices", iid
            observed = total
            if status != "unpaid":
                amount = total if status == "paid" else max(1, total // 2)
                settled = amount
                if event == "reconciliation_discrepancy":
                    settled -= max(1, amount * 3 // 100)
                pid = len(rows["payments"]) + 1
                paid = min(issued + timedelta(days=int(rng.integers(1, 45))), cutoff)
                rows["payments"].append(
                    dict(
                        id=pid,
                        invoice_id=iid,
                        paid_on=paid,
                        amount_cents=amount,
                        settled_cents=settled,
                        processor_reference=f"txn-{pid:08d}",
                    )
                )
                if event == "reconciliation_discrepancy":
                    expected, observed = amount, settled
                    entity_table, entity_id = "payments", pid
                legitimate_refund = event == "duplicate_refund" or rng.random() < 0.025
                if legitimate_refund:
                    refund_amount = max(1, amount * 10 // 100)
                    for _ in range(2 if event == "duplicate_refund" else 1):
                        rid = len(rows["refunds"]) + 1
                        rows["refunds"].append(
                            dict(
                                id=rid,
                                payment_id=pid,
                                refunded_on=min(paid + timedelta(days=7), cutoff),
                                amount_cents=refund_amount,
                                request_reference=f"req-{pid:08d}",
                            )
                        )
                    if event == "duplicate_refund":
                        expected, observed = -refund_amount, -2 * refund_amount
                        entity_table, entity_id = "refunds", rid
            if event:
                truth.append(
                    dict(
                        id=len(truth) + 1,
                        invoice_id=iid,
                        leakage_type=event,
                        entity_table=entity_table,
                        entity_id=entity_id,
                        expected_cents=expected,
                        observed_cents=observed,
                        leakage_cents=expected - observed,
                    )
                )
    from revenue_leakage.schema import TABLES, labels

    frames = {
        name: pd.DataFrame(records, columns=TABLES[name].columns.keys())
        for name, records in rows.items()
    }
    return frames, pd.DataFrame(truth, columns=labels.columns.keys())
