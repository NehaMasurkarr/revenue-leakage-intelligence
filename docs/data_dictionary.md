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
