# Revenue Leakage Detection & Recovery Intelligence

Phases 1–3 provide reproducible synthetic subscription billing data, isolated evaluation labels, versioned PostgreSQL migrations, idempotent ingestion, operational financial SQL analytics, and audited incremental ETL with raw/staging/analytics layers. No dashboards or visualizations are included; Tableau belongs to the final phase.

## Quick start

Python 3.11+ is required. Use the committed dependency lock for repeatable results.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
revenue-leakage generate
revenue-leakage validate
pytest -q
ruff check .
ruff format --check .
```

The default run uses seed 42 and covers January 2024 through December 2025, with exactly 10,000 customers, 10,000 contracts, 50,000 invoices, and 5,000 labeled leakage events. Output is local and ignored by Git:

- `data/synthetic/operational/`: customers, pricing plans, contracts, invoices, invoice line items, payments, refunds.
- `data/synthetic/ground_truth/leakage_labels.csv`: evaluation labels only.
- `data/synthetic/manifest.json`: configuration, dependency versions, counts, total leakage, SHA-256 checksums.

Generation refuses a nonempty output directory. To compare two runs, use `--output data/repeat` and compare manifest checksums. Reproducibility requires the same seed, configuration, code, and locked library versions. The generator never uses the current date, global random state, or network data.

```sh
revenue-leakage generate --seed 7 --customers 100 --invoices 500 --output data/small
```

## PostgreSQL

Use an existing PostgreSQL database, or start the optional free local container (requires Docker):

```sh
cp .env.example .env
# Edit .env: replace CHANGE_ME, then export the connection variables.
set -a
. ./.env
set +a
docker compose up -d --wait
revenue-leakage migrate
revenue-leakage load
revenue-leakage load  # identical records are unchanged
revenue-leakage sql-check --as-of 2025-12-31
revenue-leakage analytics --query summary --as-of 2025-12-31
```

`docker compose` uses `POSTGRES_PASSWORD` from `.env`; keep it consistent with the URL. Reserved password characters must be URL-encoded in `DATABASE_URL`. The CLI reads exported environment variables, and does not automatically source `.env`. Connection URLs use SQLAlchemy's [psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg).

`migrate` applies packaged, checksum-verified SQL migrations. `load` also migrates automatically and stages all seven operational tables in one transaction. New IDs are inserted, byte-equivalent field values for existing IDs are unchanged, and conflicting existing records fail with full rollback. It never updates or deletes existing financial records. Generated IDs share one dataset namespace: loading a different seed into the same database will usually conflict; use a separate database for each dataset. PostgreSQL advisory and table locks serialize loads, and database checks run before commit. Explicit IDs synchronize their sequences for subsequent inserts.

The PostgreSQL loader checks operational file hashes and row counts, without opening ground-truth CSVs or using evaluation labels. The Phase 1 `validate` command still validates the full synthetic artifact, including evaluation truth. CSVs cannot enforce foreign keys themselves; Python validation and database constraints enforce those relationships. The SQLite helper retains its Phase 1 empty-database behavior for portable tests.

Use a separate database and restricted role if you explicitly load labels with the Python `storage.load_truth` API. It is intended only for evaluation. Never grant operational analytics users access to that database or the ground-truth directory.

For PostgreSQL integration tests, create a **dedicated empty** database and export its URL:

```sh
# Use PostgreSQL tooling or your local database administrator to create this empty database.
createdb revenue_leakage_test
export TEST_DATABASE_URL='postgresql+psycopg://localhost/revenue_leakage_test'
pytest -q
```

Integration tests require an empty dedicated database and create and remove the operational tables, migration ledger, load ledger, analytics schema, views, and function. They refuse preexisting tables or analytics schemas. CI runs it on PostgreSQL 16 for Python 3.11 and 3.13. SQLite is used only for fast portable constraint tests.

## Data and financial model

See [schema documentation](docs/schema.md), [data dictionary](docs/data_dictionary.md), and [PostgreSQL DDL](docs/schema.sql). All monetary amounts are integer USD cents; discounts are basis points rounded half up using integer arithmetic. No financial amount is calculated using floating point. NumPy controls random draws; SciPy generates Poisson usage; Faker creates synthetic company names; Pandas handles tabular artifacts; SQLAlchemy defines and loads the constrained schema.

Each customer has one contract and one invoice per active month. Contract lengths vary and cohorts start throughout the 24-month window. The default five invoices per customer on average represent a short-tenure/cohort sample, not 24 months of continuous service for every customer. Customers may be created shortly before the window. Due dates may fall after the window; payments and refunds are capped at its end. Prices and currencies are fixed for each plan, with no tax, FX, proration, processor fees, renewals, or chargebacks in Phase 1.

Default leakage is 10% of invoices, evenly distributed across five mutually exclusive cases. Unpaid and partial invoices are ordinary collection outcomes and are not automatically leakage. Legitimate partial refunds occur alongside duplicate refund cases. Labels record exact recoverable cents relative to each event's counterfactual, not a promise of actual collection. Negative expected/observed values for refund labels represent cash outflows.

Operational schemas intentionally permit valid financial anomalies (a lower price, an extra refund request, a settlement shortfall). Quality checks enforce structural consistency rather than removing those cases. Missing usage quantities are latent: evaluation truth preserves the expected invoice amount, while the operational data contains no hidden missing-charge line.

## Development and publishing

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check .
ruff format --check .
```

`requirements.lock` pins the verified environment. Update it deliberately when dependencies change, then regenerate and recheck manifests. Git ignores datasets, databases, secrets, `.env` files, build outputs, and virtual environments. Only `.env.example` contains placeholder configuration.

## Phase 2 SQL analytics

All reports read operational records only. Supply an explicit inclusive cutoff with `--as-of`, or export `ANALYTICS_AS_OF`. There is no implicit current-date cutoff. CLI JSON uses integer cents for integral results, and decimal strings for fractional percentages or basis points.

```sh
revenue-leakage analytics --query monthly --as-of 2025-12-31
revenue-leakage analytics --query outstanding --as-of 2025-12-31
revenue-leakage analytics --query aging --as-of 2025-12-31
revenue-leakage analytics --query reconciliation --as-of 2025-12-31
revenue-leakage analytics --query pricing --as-of 2025-12-31
revenue-leakage analytics --query discounts --as-of 2025-12-31
revenue-leakage analytics --query customer_trends --as-of 2025-12-31
revenue-leakage analytics --query refund_requests --as-of 2025-12-31
revenue-leakage explain --query monthly --as-of 2025-12-31
```

`sql-check` checks cross-table integrity and reconciles independent base-table sums, monthly activity, customer activity, and aging to the financial summary in a repeatable-read snapshot. Reports and EXPLAIN run in read-only transactions. `EXPLAIN ANALYZE` executes SELECTs and returns their measured plans; it does not mutate financial records.

Billed revenue means invoice totals after discounts, before refunds; collected revenue means gross ledger receipts. Net cash is processor receipts minus refunds. Outstanding is billed minus ledger receipts; refunds do not reopen invoices. These are operational billing/cash measures, not accounting revenue recognition. See [financial definitions and business interpretation](docs/sql_analytics.md), [migration and ingestion operations](docs/database.md), [query tuning](docs/performance.md), and [Phase 2 verification](docs/phase2_validation.md).

Environment options are documented in `.env.example`: `DATABASE_URL`, `TEST_DATABASE_URL`, `DATA_DIR`, `LOAD_BATCH_SIZE`, `ANALYTICS_AS_OF`, `PG_STATEMENT_TIMEOUT_MS`, and `PG_LOCK_TIMEOUT_MS`. Secrets belong in an exported environment or a locally sourced ignored `.env`; CLI output never includes connection URLs.

The public repository is [NehaMasurkarr/revenue-leakage-intelligence](https://github.com/NehaMasurkarr/revenue-leakage-intelligence). CI tests PostgreSQL 16 on Python 3.11 and 3.13, then generates the full dataset, migrates, loads it twice, reconciles SQL reports, and checks query plans.

No paid services are required.

## Phase 3 ETL and quality automation

The existing immutable `load` command is preserved. Use the new versioned snapshot workflow for updates and late-arriving transactions:

```sh
revenue-leakage etl-run --mode full --snapshot-version 1 --as-of 2025-12-31
revenue-leakage etl-run --snapshot-version 1 --as-of 2025-12-31
revenue-leakage etl-run --replay --as-of 2025-12-31
revenue-leakage etl-freshness --as-of 2025-12-31
revenue-leakage etl-runs --limit 20
```

Each complete source snapshot needs a producer-assigned increasing version. Identical version/content reruns are safe; corrections require a higher version. Content hashes capture changed records, and committed raw sequence watermarks drive processing, so old business dates do not hide late receipts. Extraction still scans complete files. Deletions, omitted prior IDs, stale versions and conflicting retransmissions fail. Only operational CSVs are read.

Raw capture is durable independently; staging, public corrections, customer summaries, checkpoints and success audit commit together. The workflow reuses existing financial SQL and quality checks, records 19 quality results per successful run, retries transient database failures, and supports abandoned-run recovery. Failures return a run UUID for investigation. See [architecture, lineage, recovery, freshness and configuration](docs/etl.md), [ETL dictionary](docs/data_dictionary.md), and [Phase 3 validation](docs/phase3_validation.md).
