# Data dictionary

Every column is required (NOT NULL). All dates are ISO-8601 calendar dates. Monetary fields are integer USD cents, never floating-point dollars. Operational data and evaluation truth are stored separately.

## customers

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| name | VARCHAR(160) | Synthetic company name (customers) or fixed commercial plan name (pricing_plans). |
| email | VARCHAR(160) | Unique synthetic address at the reserved example.invalid domain. |
| segment | VARCHAR(20) | small_business, mid_market, or enterprise; determined by plan tier. |
| created_on | DATE | Customer creation date, zero to sixty days before contract start. |

## pricing_plans

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| name | VARCHAR(40) | Synthetic company name (customers) or fixed commercial plan name (pricing_plans). |
| currency | VARCHAR(3) | USD only in Phase 1. |
| seat_price_cents | BIGINT | Authorized monthly subscription price per seat in USD cents. |
| usage_price_cents | BIGINT | Authorized price per usage unit in USD cents. |

## contracts

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| customer_id | INTEGER | Foreign key to customers.id. |
| plan_id | INTEGER | Foreign key to pricing_plans.id. |
| starts_on | DATE | First active day of the contract, inclusive. |
| ends_on | DATE | Last active day of the contract, inclusive. |
| seats | INTEGER | Positive contracted seat count; tier-dependent distribution. |
| discount_bps | INTEGER | Authorized discount in basis points (10,000 = 100%). |

## invoices

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| contract_id | INTEGER | Foreign key to contracts.id; customer and plan are reached through the contract. |
| issued_on | DATE | First day of the monthly billing period. |
| due_on | DATE | Invoice issue date plus thirty days; may extend beyond dataset window. |
| currency | VARCHAR(3) | USD only in Phase 1. |
| subtotal_cents | BIGINT | Sum of operational invoice line amounts before discount. |
| discount_cents | BIGINT | Applied discount amount, rounded half up to an integer cent. |
| total_cents | BIGINT | subtotal_cents minus discount_cents; no taxes in Phase 1. |
| status | VARCHAR(20) | paid, partial, or unpaid according to ledger payment total; refunds do not reopen invoices. |

## invoice_line_items

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| invoice_id | INTEGER | Foreign key to invoices.id in operations; logical evaluation reference in ground truth. |
| kind | VARCHAR(20) | subscription or usage. Missed charges omit the usage row entirely. |
| quantity | INTEGER | Positive billed seat count or usage unit count. |
| unit_price_cents | BIGINT | Actual billed unit price; can differ from plan price for underbilling. |
| amount_cents | BIGINT | Line: quantity times unit price. Payment: ledger receipt. Refund: positive cash payout. |

## payments

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| invoice_id | INTEGER | Foreign key to invoices.id in operations; logical evaluation reference in ground truth. |
| paid_on | DATE | Receipt date, on or after issue date and capped at dataset end. |
| amount_cents | BIGINT | Line: quantity times unit price. Payment: ledger receipt. Refund: positive cash payout. |
| settled_cents | BIGINT | Processor settlement receipt, gross of fees (no fees modeled). |
| processor_reference | VARCHAR(80) | Unique deterministic payment transaction identifier. |

## refunds

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| payment_id | INTEGER | Foreign key to payments.id. |
| refunded_on | DATE | Refund execution date, on or after receipt date and capped at dataset end. |
| amount_cents | BIGINT | Line: quantity times unit price. Payment: ledger receipt. Refund: positive cash payout. |
| request_reference | VARCHAR(80) | Logical refund request identifier; repeated when duplicate refund injected. |

## leakage_labels

| Column | SQL type | Meaning |
| --- | --- | --- |
| id | INTEGER | Positive surrogate primary key, deterministic within a dataset. |
| invoice_id | INTEGER | Foreign key to invoices.id in operations; logical evaluation reference in ground truth. |
| leakage_type | VARCHAR(40) | underbilling, incorrect_discount, duplicate_refund, missed_charge, or reconciliation_discrepancy. |
| entity_table | VARCHAR(40) | Logical affected table: invoices, payments, or refunds; no cross-database FK. |
| entity_id | INTEGER | ID of affected invoice, payment, or second duplicate refund. |
| expected_cents | BIGINT | Counterfactual event amount; refund cash outflows are negative. |
| observed_cents | BIGINT | Actual event amount; refund cash outflows are negative. |
| leakage_cents | BIGINT | Positive recoverable loss: expected_cents minus observed_cents. |

## Phase 2 migration and ingestion metadata

These operational audit tables are created by migrations and contain no financial labels.

| Table.column | PostgreSQL type | Meaning |
| --- | --- | --- |
| schema_migrations.version | TEXT primary key | Immutable numbered SQL filename |
| schema_migrations.checksum | VARCHAR(64), NOT NULL | SHA-256 of deployed migration text |
| schema_migrations.applied_at | TIMESTAMPTZ, NOT NULL | Database execution timestamp |
| data_loads.fingerprint | VARCHAR(64) primary key | SHA-256 of sorted operational frame content and table names; lowercase hex check |
| data_loads.row_counts | JSONB, NOT NULL | Object mapping operational table names to source row counts |
| data_loads.loaded_at | TIMESTAMPTZ, NOT NULL | First successful load time for this fingerprint; unchanged on replay |

## Phase 2 derived analytical fields

Views live in `analytics`; they are all-time virtual relations. Their source SQL and exact expressions are in migration 003. `invoice_balances(date)` is a cutoff-aware SQL function. Use [SQL documentation](sql_analytics.md) for timing, sign conventions, rounding, and report execution instructions. Monetary sums are PostgreSQL NUMERIC or BIGINT and remain integral cents; ratios/percentages are NUMERIC and may be NULL on a zero denominator.

| Relation | Grain | Fields and meaning |
| --- | --- | --- |
| invoice_financials | invoice_id | invoice_id/customer_id/contract_id are operational IDs; issued_on/due_on retain invoice dates; billed_cents is invoice total; collected_cents and settled_cents sum independent payments; refunded_cents sums actual payouts; outstanding_cents = billed − collected; net_cash_cents = settled − refunds |
| invoice_balances(as_of DATE) | invoice_id through cutoff | Same fields as invoice_financials plus days_overdue INTEGER; financial events are each filtered by their own date; supplied cutoff is inclusive |
| payment_reconciliation | payment_id | payment_id/invoice_id/paid_on/processor_reference identify receipt; ledger_cents = amount_cents; settled_cents is processor receipt; discrepancy_cents = ledger − settled; refunded_cents preaggregates payouts; net_cash_cents = settled − refunds |
| pricing_discrepancies | line_id | line_id/invoice_id/customer_id/issued_on/kind identify existing line; quantity is billed units; billed_unit_price_cents and contracted_unit_price_cents compare actual vs plan; gross_price_gap_cents = (contracted − billed unit price) × quantity |
| discount_patterns | invoice_id | invoice_id/customer_id/issued_on identify invoice; discount_bps is authorized rate; subtotal_cents/discount_cents retain actual billed values; authorized_discount_cents uses contractual bps on actual subtotal; excess_discount_cents = actual − authorized; applied_discount_bps = 10,000 × actual discount/subtotal |
| repeated_refund_requests | payment_id/request_reference | payout_count counts actual records; total_refunded_cents sums them; first_refunded_on/last_refunded_on delimit payout dates; only repeated groups appear |

Query outputs extend these relations:

| Field | Type / scope | Meaning |
| --- | --- | --- |
| month | DATE, monthly/customer/discount reports | First day of event calendar month |
| invoice_count | BIGINT, summary/aging/discount cohorts | Count at the stated report grain, never a count of joined child rows |
| net_collected_cents | NUMERIC, summary/monthly | Ledger receipts less actual refunds |
| reconciliation_gap_cents | NUMERIC, summary | Total signed ledger-minus-settlement difference |
| bucket | TEXT, aging | current, 1-30, 31-60, 61-90, or 91+ days overdue |
| previous_month_billed_cents | NUMERIC nullable, customer trends | LAG of adjacent calendar-month billed amount |
| cumulative_billed_cents | NUMERIC, customer trends | Customer sum since report calendar start through current row |
| trailing_three_month_billed_cents | NUMERIC, customer trends | Current month plus up to two previous calendar months |
| month_over_month_change_cents | NUMERIC nullable, customer trends | Current billed minus previous month's billed |
| month_over_month_change_pct | NUMERIC nullable, customer trends | 100 × change/prior month, rounded to two decimals; NULL when prior is absent or zero |
| applied_discount_cents | NUMERIC, discount report | Sum of actual discounts in the monthly authorized-rate cohort |
| signed_excess_discount_cents | NUMERIC, discount report | Sum of actual minus authorized discounts |
| excessive_discount_count | BIGINT, discount report | Count of invoices with positive excess discount |
| excessive_discount_cents | NUMERIC, discount report | Sum of positive excess discount only |
| weighted_applied_discount_bps | NUMERIC nullable, discount report | 10,000 × summed discounts/summed subtotals, rounded to two decimals |
| check_name / violations | TEXT / BIGINT, quality query | Structural invariant name and number of failing records |

Report rows are computed from operational data only. No new financial source columns or label-derived detection fields are introduced. Gross pricing and discount differences overlap economically and are not additive recovery estimates.
