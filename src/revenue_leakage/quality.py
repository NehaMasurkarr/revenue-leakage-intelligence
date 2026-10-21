"""Structural checks intentionally accept injected business anomalies."""

from revenue_leakage.generator import KINDS, discounted
from revenue_leakage.schema import TABLES


class QualityError(ValueError):
    """Operational data or evaluation truth violates its contract."""


def require(condition, message):
    if not bool(condition):
        raise QualityError(message)


def validate(frames, truth=None):
    for name, table in TABLES.items():
        require(name in frames, f"missing table: {name}")
        frame = frames[name]
        require(list(frame.columns) == list(table.columns.keys()), f"{name}: columns")
        require(not frame.isna().any().any(), f"{name}: nulls")
        require(frame.id.is_unique, f"{name}: duplicate IDs")
        require((frame.id > 0).all(), f"{name}: nonpositive IDs")
        for column in table.columns:
            if column.name.endswith("_cents") or column.name in (
                "id",
                "quantity",
                "seats",
                "discount_bps",
                "customer_id",
                "plan_id",
                "contract_id",
                "invoice_id",
                "payment_id",
            ):
                values = frame[column.name]
                require(all(isinstance(v, int) for v in values), f"{name}: noninteger money/ID")
            for foreign in column.foreign_keys:
                require(
                    frame[column.name].isin(frames[foreign.column.table.name].id).all(),
                    f"{name}: orphan {column.name}",
                )
    customers, plans, contracts, inv, lines, pay, refunds = (
        frames[n]
        for n in (
            "customers",
            "pricing_plans",
            "contracts",
            "invoices",
            "invoice_line_items",
            "payments",
            "refunds",
        )
    )
    require(customers.email.is_unique, "duplicate customer emails")
    require((plans.currency == "USD").all() and (inv.currency == "USD").all(), "currency")
    require((plans[["seat_price_cents", "usage_price_cents"]] > 0).all().all(), "plan prices")
    require((contracts.seats > 0).all(), "contract seats")
    require(contracts.discount_bps.between(0, 10000).all(), "contract discount")
    require((contracts.starts_on <= contracts.ends_on).all(), "contract dates")
    joined = contracts.merge(customers, left_on="customer_id", right_on="id")
    require((joined.created_on <= joined.starts_on).all(), "customer chronology")
    joined = inv.merge(contracts, left_on="contract_id", right_on="id")
    require(
        ((joined.issued_on >= joined.starts_on) & (joined.issued_on <= joined.ends_on)).all(),
        "invoice outside contract",
    )
    require((inv.due_on >= inv.issued_on).all(), "due date before invoice")
    require(not inv.duplicated(["contract_id", "issued_on"]).any(), "duplicate billing period")
    require(lines.kind.isin(["subscription", "usage"]).all(), "line kind")
    require(not lines.duplicated(["invoice_id", "kind"]).any(), "duplicate line kind")
    require(((lines.quantity > 0) & (lines.unit_price_cents > 0)).all(), "line values")
    require((lines.amount_cents == lines.quantity * lines.unit_price_cents).all(), "line math")
    sums = lines.groupby("invoice_id").amount_cents.sum().reindex(inv.id, fill_value=0)
    require((sums.to_numpy() == inv.subtotal_cents.to_numpy()).all(), "invoice subtotal")
    require(inv.discount_cents.between(0, inv.subtotal_cents).all(), "discount bounds")
    require((inv.total_cents == inv.subtotal_cents - inv.discount_cents).all(), "invoice math")
    require(inv.status.isin(["paid", "partial", "unpaid"]).all(), "invoice status")
    require(pay.processor_reference.is_unique, "duplicate processor reference")
    require(((pay.amount_cents > 0) & (pay.settled_cents >= 0)).all(), "payment values")
    sums = pay.groupby("invoice_id").amount_cents.sum().reindex(inv.id, fill_value=0).to_numpy()
    require((sums <= inv.total_cents.to_numpy()).all(), "overpayment")
    require(
        ((inv.status == "paid") == (sums == inv.total_cents.to_numpy())).all(),
        "paid status inconsistent",
    )
    require(((inv.status == "unpaid") == (sums == 0)).all(), "unpaid status inconsistent")
    joined = pay.merge(inv, left_on="invoice_id", right_on="id")
    require((joined.paid_on >= joined.issued_on).all(), "payment chronology")
    require((refunds.amount_cents > 0).all(), "refund values")
    joined = refunds.merge(pay, left_on="payment_id", right_on="id", suffixes=("_r", "_p"))
    require((joined.refunded_on >= joined.paid_on).all(), "refund chronology")
    sums = refunds.groupby("payment_id").amount_cents.sum().reindex(pay.id, fill_value=0)
    require((sums.to_numpy() <= pay.amount_cents.to_numpy()).all(), "refund exceeds payment")
    if truth is not None:
        validate_truth(frames, truth)
    return {name: len(frame) for name, frame in frames.items()}


def validate_truth(frames, truth):
    from revenue_leakage.schema import labels

    require(list(truth.columns) == list(labels.columns.keys()), "truth columns")
    require(not truth.isna().any().any(), "null truth")
    for column in (
        "id",
        "invoice_id",
        "entity_id",
        "expected_cents",
        "observed_cents",
        "leakage_cents",
    ):
        require(all(isinstance(v, int) for v in truth[column]), "noninteger truth")
    require(
        ((truth.id > 0) & (truth.invoice_id > 0) & (truth.entity_id > 0)).all(),
        "nonpositive truth IDs",
    )
    require(truth.id.is_unique and truth.invoice_id.is_unique, "duplicate truth")
    require(truth.leakage_type.isin(KINDS).all(), "unknown leakage type")
    require(truth.invoice_id.isin(frames["invoices"].id).all(), "orphan truth")
    require((truth.leakage_cents > 0).all(), "nonpositive leakage")
    require(
        (truth.leakage_cents == truth.expected_cents - truth.observed_cents).all(),
        "truth arithmetic",
    )
    invs = frames["invoices"].set_index("id")
    contracts = frames["contracts"].set_index("id")
    plans = frames["pricing_plans"].set_index("id")
    pay = frames["payments"].set_index("id")
    refunds = frames["refunds"].set_index("id")
    line_groups = {k: v for k, v in frames["invoice_line_items"].groupby("invoice_id")}
    for label in truth.itertuples():
        inv = invs.loc[label.invoice_id]
        contract = contracts.loc[inv.contract_id]
        plan = plans.loc[contract.plan_id]
        if label.leakage_type in ("underbilling", "incorrect_discount", "missed_charge"):
            require(
                label.entity_table == "invoices" and label.entity_id == label.invoice_id,
                "truth invoice entity",
            )
            require(label.observed_cents == inv.total_cents, "truth observed billing")
            lines = line_groups[label.invoice_id].set_index("kind")
            if label.leakage_type != "missed_charge":
                subtotal = int(contract.seats) * int(plan.seat_price_cents) + int(
                    lines.loc["usage"].quantity
                ) * int(plan.usage_price_cents)
                expected = subtotal - discounted(subtotal, int(contract.discount_bps))
                require(label.expected_cents == expected, "truth expected billing")
            else:
                require("usage" not in lines.index, "missed charge still present")
                # Missing usage is latent; its original cents exist only in evaluation truth.
                require(label.expected_cents > inv.total_cents, "missed charge delta")
        elif label.leakage_type == "reconciliation_discrepancy":
            require(
                label.entity_table == "payments" and label.entity_id in pay.index,
                "truth payment entity",
            )
            p = pay.loc[label.entity_id]
            require(
                p.invoice_id == label.invoice_id
                and label.expected_cents == p.amount_cents
                and label.observed_cents == p.settled_cents,
                "truth reconciliation",
            )
        else:
            require(
                label.entity_table == "refunds" and label.entity_id in refunds.index,
                "truth refund entity",
            )
            r = refunds.loc[label.entity_id]
            require(pay.loc[r.payment_id].invoice_id == label.invoice_id, "truth refund invoice")
            group = refunds[refunds.request_reference == r.request_reference]
            require(len(group) == 2 and group.payment_id.nunique() == 1, "duplicate refund pair")
            require(
                label.expected_cents == -int(r.amount_cents)
                and label.observed_cents == -int(group.amount_cents.sum()),
                "truth refund amount",
            )
