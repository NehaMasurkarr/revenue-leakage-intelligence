# PostgreSQL migrations and ingestion

PostgreSQL 16 is the verified target. Monetary records use BIGINT USD cents; aggregate sums and calculated ratios use PostgreSQL NUMERIC to avoid floating-point money and overflowing aggregate integers. Dates use DATE and audit timestamps use TIMESTAMPTZ. No tax, FX, credit-note, recognition, settlement-date, or invoice-cancellation model is introduced in Phase 2.

## Migration execution

Export `DATABASE_URL` as documented in the README, then run `revenue-leakage migrate`. Files are packaged inside `revenue_leakage/sql/migrations` and run in filename order in a single transaction:

| Version | Changes |
| --- | --- |
| 001_operational_schema.sql | Seven baseline operational tables, required columns, primary/foreign keys, arithmetic/range checks, uniqueness, foreign-key indexes |
| 002_financial_indexes.sql | Positive ID checks, analytical indexes, ingestion audit ledger |
| 003_analytics.sql | `analytics` schema, five views and cutoff-aware invoice-balances function |

The runner creates `public.schema_migrations`, obtains a transaction-scoped advisory lock shared with the loader, verifies SHA-256 migration checksums, and skips already applied files. Unknown versions or altered applied files fail. Migration files are immutable once deployed; make new numbered files for later changes. Never bypass checksum errors by editing the ledger. Migrations are forward-only; restore a database backup for rollback. New application versions should be deployed with their required migrations. DDL and inserts roll back together if a load fails; sequence values can have gaps because PostgreSQL sequences are not transactional.

For an existing Phase 1 database, the runner verifies the complete operational table set, column types/nullability, primary keys, foreign keys, unique constraints, and baseline CHECK expressions before adoption. Its expression normalization supports the project's generated baseline, not arbitrary semantically equivalent DDL. Partial or incompatible schemas fail safely and require explicit repair. Existing records are retained. The `analytics` schema and audit tables are reserved project object names. A schema already containing evaluation labels is rejected.

Schema-level FK and row checks always apply. Aggregate and chronology invariants are checked by the pipeline and `sql-check`, not triggers. Direct writers must follow those invariants and run reconciliation afterward. Ordinary duplicate logical refund requests and ledger/settlement differences remain allowed operational anomalies.

## Idempotent ingestion

`revenue-leakage load` reads `DATA_DIR` (default `data/synthetic`) or `--input`. It verifies seven operational CSV SHA-256 hashes and row counts against the manifest, validates required columns, IDs, references, dates, and financial arithmetic, and does not read evaluation files or evaluation fields from the manifest.

Within one transaction the pipeline migrates, locks all operational tables in SHARE ROW EXCLUSIVE mode, stages each table in a temporary table, compares complete rows on primary key, and inserts new IDs in FK order. Identical rows are unchanged. A differing value for an existing ID raises an immutable-record conflict and rolls back every insert in the load. Unique-key collisions also roll back. There is no silent overwrite, ignore-on-conflict data loss, truncate, or delete. Datasets generated with a different seed need their own database because their surrogate IDs overlap. Append-only compatible records are supported; source validation requires parents to be present in the incoming frames too.

The loader checks the entire persisted database for aggregate/temporal integrity before commit, advances serial sequences to existing maxima, records a canonical operational-frame fingerprint in `data_loads`, and runs ANALYZE on all seven tables. The fingerprint sorts rows by ID and hashes table names plus canonical CSV values; it is independent of labels. Replaying an identical load keeps the audit row and its timestamp unchanged, but still verifies the incoming rows against the database. It does not trust a prior load marker to hide drift. JSON output reports inserted and unchanged row counts by table. Preexisting rows not included in the incoming dataset remain untouched.

Locks permit ordinary reads but briefly block writers. Use the configured lock and statement timeouts; retry the whole command after a transient lock timeout. The shared advisory lock serializes project migration/load sessions, while table locks also coordinate with ordinary database writers. Do not edit migration or ledger objects concurrently outside the runner. A read-only analytics transaction uses REPEATABLE READ so all reconciliation queries see one snapshot.

## Configuration and permissions

| Environment variable | Default | Meaning |
| --- | --- | --- |
| DATABASE_URL | required | SQLAlchemy PostgreSQL URL; psycopg driver recommended |
| TEST_DATABASE_URL | absent | Dedicated empty disposable test database; tests skip without it |
| DATA_DIR | data/synthetic | Artifact root used by generate/load/validate unless overridden |
| LOAD_BATCH_SIZE | 1000 | Staging insert batch size; valid range 1–10,000 |
| ANALYTICS_AS_OF | absent | Inclusive YYYY-MM-DD reporting cutoff; CLI `--as-of` overrides |
| PG_STATEMENT_TIMEOUT_MS | 120000 | Per-statement timeout, positive milliseconds |
| PG_LOCK_TIMEOUT_MS | 10000 | Maximum lock wait, positive milliseconds |

The CLI uses exported environment variables and does not automatically parse `.env`. Give a migration/ingestion role CREATE and table write permissions only in the operational database. Analysts need SELECT on operational tables and analytics views, USAGE on `analytics`, and EXECUTE on `analytics.invoice_balances(date)`. SQL functions use invoker permissions and a fixed search path. Configure read-only roles/grants to suit the deployment; no users or credentials are created by migrations. Keep evaluation labels in their separate restricted database/directory.

Integration tests require a completely empty dedicated database and clean up only the known project tables and analytics schema they create. They test fresh/replayed migrations, Phase 1 adoption, incompatible schemas, constraints, concurrent identical loads, idempotence, append, sequence synchronization, rollback after a late conflict, independent financial totals, historical cutoffs, multiple payments/refunds, month gaps, aging boundaries, and every query's EXPLAIN plan.
