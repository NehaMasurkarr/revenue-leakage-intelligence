"""Portable SQLAlchemy schema; production target is PostgreSQL."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()


def table(name, *columns, checks=(), unique=()):
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        *columns,
        *(CheckConstraint(c) for c in checks),
        *(UniqueConstraint(*u) for u in unique),
    )


def fk(name, target):
    return Column(name, Integer, ForeignKey(f"{target}.id"), nullable=False, index=True)


def col(name, kind):
    return Column(name, kind, nullable=False)


customers = table(
    "customers",
    col("name", String(160)),
    col("email", String(160)),
    col("segment", String(20)),
    col("created_on", Date),
    unique=(("email",),),
)
pricing_plans = table(
    "pricing_plans",
    col("name", String(40)),
    col("currency", String(3)),
    col("seat_price_cents", BigInteger),
    col("usage_price_cents", BigInteger),
    checks=("seat_price_cents > 0", "usage_price_cents > 0", "currency = 'USD'"),
)
contracts = table(
    "contracts",
    fk("customer_id", "customers"),
    fk("plan_id", "pricing_plans"),
    col("starts_on", Date),
    col("ends_on", Date),
    col("seats", Integer),
    col("discount_bps", Integer),
    checks=("ends_on >= starts_on", "seats > 0", "discount_bps BETWEEN 0 AND 10000"),
)
invoices = table(
    "invoices",
    fk("contract_id", "contracts"),
    col("issued_on", Date),
    col("due_on", Date),
    col("currency", String(3)),
    col("subtotal_cents", BigInteger),
    col("discount_cents", BigInteger),
    col("total_cents", BigInteger),
    col("status", String(20)),
    checks=(
        "due_on >= issued_on",
        "currency = 'USD'",
        "subtotal_cents >= 0",
        "discount_cents BETWEEN 0 AND subtotal_cents",
        "total_cents = subtotal_cents - discount_cents",
        "status IN ('paid', 'partial', 'unpaid')",
    ),
    unique=(("contract_id", "issued_on"),),
)
invoice_line_items = table(
    "invoice_line_items",
    fk("invoice_id", "invoices"),
    col("kind", String(20)),
    col("quantity", Integer),
    col("unit_price_cents", BigInteger),
    col("amount_cents", BigInteger),
    checks=(
        "kind IN ('subscription', 'usage')",
        "quantity > 0",
        "unit_price_cents > 0",
        "amount_cents = quantity * unit_price_cents",
    ),
    unique=(("invoice_id", "kind"),),
)
payments = table(
    "payments",
    fk("invoice_id", "invoices"),
    col("paid_on", Date),
    col("amount_cents", BigInteger),
    col("settled_cents", BigInteger),
    col("processor_reference", String(80)),
    checks=("amount_cents > 0", "settled_cents >= 0"),
    unique=(("processor_reference",),),
)
refunds = table(
    "refunds",
    fk("payment_id", "payments"),
    col("refunded_on", Date),
    col("amount_cents", BigInteger),
    col("request_reference", String(80)),
    checks=("amount_cents > 0",),
)
# Separate metadata/database: labels cannot accidentally join production tables through an FK.
label_metadata = MetaData()
labels = Table(
    "leakage_labels",
    label_metadata,
    Column("id", Integer, primary_key=True),
    col("invoice_id", Integer),
    col("leakage_type", String(40)),
    col("entity_table", String(40)),
    col("entity_id", Integer),
    col("expected_cents", BigInteger),
    col("observed_cents", BigInteger),
    col("leakage_cents", BigInteger),
    CheckConstraint("leakage_cents > 0"),
    CheckConstraint("leakage_cents = expected_cents - observed_cents"),
    UniqueConstraint("invoice_id"),
)
TABLES = {t.name: t for t in metadata.sorted_tables}
