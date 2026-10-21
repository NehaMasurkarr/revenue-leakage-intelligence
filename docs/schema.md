# Schema and generation contract

## Relationships

| Parent | Child | Foreign key | Cardinality |
| --- | --- | --- | --- |
| customers | contracts | customer_id | 1:N (generator creates 1:1) |
| pricing_plans | contracts | plan_id | 1:N |
| contracts | invoices | contract_id | 1:N |
| invoices | invoice_line_items | invoice_id | 1:N |
| invoices | payments | invoice_id | 1:N (generator creates 0:1) |
| payments | refunds | payment_id | 1:N |

Operational tables use integer primary keys, indexed foreign keys, required columns, unique customer emails and processor references, and checks for financial arithmetic and value ranges. Invoice periods and line kinds are unique within their parent. Dates use SQL DATE. Money uses BIGINT cents. The dictionary documents every column. `schema.sql` is the Phase 1 PostgreSQL baseline generated from the portable SQLAlchemy schema; versioned SQL under `src/revenue_leakage/sql/migrations/` is authoritative for Phase 2 database deployments; `ground_truth.sql` is deliberately separate.

Cross-table aggregates and chronology are validated in Python before loading: invoice subtotals equal line sums, payment totals match invoice status, refunds do not exceed paid amounts, and dates respect contract/payment relationships. These are not database triggers; applications writing outside this loader must maintain those invariants themselves. Database foreign keys and row-level constraints still apply.

Refund request references are intentionally not unique: retrying a logical refund request is the duplicate-refund failure being modeled. Processor payment references remain unique. Settlement amounts are separate from ledger payment amounts so discrepancies do not corrupt invoice arithmetic.

## Leakage mechanisms

| Type | Operational mutation | Counterfactual / leakage cents |
| --- | --- | --- |
| underbilling | Subscription unit price reduced by 20% | Contract-priced invoice net minus actual net |
| incorrect_discount | Invoice discount increased by 1,000 basis points | Authorized net minus actual net |
| duplicate_refund | Second payout with the same logical request reference | One authorized refund minus two payouts, represented as negative cash flows |
| missed_charge | Usage line omitted despite generated usage | Complete invoice net minus actual net |
| reconciliation_discrepancy | Processor settlement 3% below ledger payment (integer truncation, minimum 1 cent) | Payment amount minus settled amount |

Selection is seeded, without replacement; no invoice has overlapping labels. For an arbitrary leakage rate, `floor(invoice_count * rate)` events are allocated cyclically across types; counts differ by at most one. With fewer than five events, some types are absent. With the default settings each type has 1,000 cases. Underbilling and missed-charge losses include the effect of the authorized discount. Legitimate refunds can coexist with non-refund leakage and do not change the event-specific counterfactual.

## Evaluation isolation

Labels have their own SQLAlchemy metadata and optional evaluation database. `invoice_id` and polymorphic `entity_table`/`entity_id` are logical references validated in Python; they are not cross-database foreign keys. Only affected invoices receive label rows; all other generated invoices are negatives. Do not train or derive operational features from labels. File permissions and database grants must be configured by the deployment owner; directory separation alone is not an access-control boundary.

## Quality and reproducibility

`revenue-leakage validate` verifies file hashes, required columns, nonnull values, unique keys, integral money/IDs, foreign keys, line arithmetic, invoice totals, discounts, payment/refund bounds, status consistency, temporal consistency, and label arithmetic/entity references. Tests independently reconstruct billing expectations and match duplicate refunds and settlement discrepancies to labels, including clean invoices. Ground truth verifies the missed-charge expected amount only as a positive delta because its original usage is intentionally absent from operations.

All generation randomness comes from a local NumPy generator and a locally seeded Faker instance. SciPy receives that NumPy generator explicitly. Financial arithmetic uses integer cents and half-up basis-point discounts. Seed and package versions are written to the manifest; pinning dependencies is required for byte-identical regeneration.

## Phase 2 database objects

Migration 001 installs or verifies/adopts the Phase 1 baseline. Migration 002 adds positive primary-key checks, six analytical indexes, and the `data_loads` audit ledger. Migration 003 installs five operational views and the cutoff-aware invoice-balances function in the `analytics` schema. `schema_migrations` tracks checksums and execution timestamps. SQLAlchemy metadata remains the portable Phase 1 schema used for generation, CSV column order, and SQLite tests; use the migration runner rather than `metadata.create_all()` to deploy the complete PostgreSQL system. See [database operations](database.md) and [SQL definitions](sql_analytics.md).
