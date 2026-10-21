# Revenue Leakage Detection & Recovery Intelligence

Phase 1 creates reproducible synthetic subscription billing data and isolated evaluation labels. It includes PostgreSQL schemas, CSV artifacts, integrity checks, tests, and CI. No dashboards or visualizations are included; Tableau belongs to the final phase.

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
revenue-leakage load
```

`docker compose` uses `POSTGRES_PASSWORD` from `.env`; keep it consistent with the URL. Reserved password characters must be URL-encoded in `DATABASE_URL`. The CLI reads exported environment variables, and does not automatically source `.env`. Connection URLs use SQLAlchemy's [psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg).

`load` creates constrained SQLAlchemy tables and inserts operational data in a single transaction. It refuses nonempty target tables, never truncates data, and never loads labels. CSVs are checked against their manifest before loading. CSV files cannot enforce foreign keys themselves; validation and database constraints enforce those relationships.

Use a separate database and restricted role if you explicitly load labels with the Python `storage.load_truth` API. It is intended only for evaluation. Never grant operational analytics users access to that database or the ground-truth directory.

For PostgreSQL integration tests, create a **dedicated empty** database and export its URL:

```sh
# Use PostgreSQL tooling or your local database administrator to create this empty database.
createdb revenue_leakage_test
export TEST_DATABASE_URL='postgresql+psycopg://localhost/revenue_leakage_test'
pytest -q
```

The integration test creates and removes project tables in that database. It refuses existing project tables. CI runs it on PostgreSQL 16 for Python 3.11 and 3.13. SQLite is used only for fast portable constraint tests.

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

If GitHub authentication needs repair, run:

```sh
gh auth login -h github.com
gh repo create revenue-leakage-intelligence --public --source=. --remote=origin --push
```

No paid services are required.
