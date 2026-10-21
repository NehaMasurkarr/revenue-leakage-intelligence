# Phase 3: production-style ETL

The Python workflow reuses Phase 2 migration/checksum locking, operational schema, CSV hash/count verification, financial SQL, quality checks, and database maintenance helpers. Migration 004 adds raw, typed staging, audit, and customer-summary storage. Phases 1–2 commands retain their behavior; the `load` command still rejects corrections to existing financial rows. Use `etl-run` for tracked corrections and late transactions.

## Architecture and lineage

| Layer / object | Grain and origin | Consumers |
| --- | --- | --- |
| Operational CSVs and manifest | Complete snapshots of the seven generated financial tables; operational hashes/counts only | Snapshot extractor |
| raw.deliveries | One source/snapshot version, canonical fingerprint, counts, originating run | Replay/audit and ordering guards |
| raw.changes | One changed source record/version, original text payload, hash and committed capture sequence | Incremental normalization and historical evidence |
| raw.current_records | Latest change pointer per source/table/entity ID | Full rebuild and snapshot completeness verification |
| staging.customers, pricing_plans, contracts, invoices, invoice_line_items, payments, refunds | Normalized typed state, same primary/foreign/unique/arithmetic constraints as baseline operations | Reconciliation, quality checks, publication |
| public operational tables | Validated, published state; existing Phase 2 schema | All existing operational SQL views and reports |
| analytics.customer_financials | One customer at the last successful run's explicit cutoff, with run lineage | Customer-level billing/cash/receivables queries |
| etl.pipeline_runs, run_attempts, quality_results, source_state | Run history, attempts, measurements, ordered source version and processing watermark | Operations, failure investigation, health checks |

Ground-truth files are not opened by this workflow. No label table, prediction, dashboard, or model feeds any layer. Snapshot extraction reads exactly the seven operational files and ignores evaluation metadata. The Phase 1 full-artifact `validate` command still checks evaluation truth separately.

Raw capture commits separately from publication. Once a payload is landed, it remains available if normalization or publication fails. Staging changes, public inserts/updates, customer summaries, the processing watermark, quality results for successful processing, and the final successful run/attempt records commit together. Readers see either the prior published state or the complete new state.

## Reliable change tracking

The source has no update timestamps or CDC stream. This implementation scans complete snapshots and hashes each record, then processes only changed/new records after the last successful raw sequence watermark. It is incremental downstream processing, not incremental file extraction. It does not use invoice/payment/refund dates as ingestion watermarks, so an old-dated new payment or correction is still processed.

A producer must assign each complete snapshot a positive, monotonically increasing `--snapshot-version` (BIGINT). Repeating the same version with identical content is safe. Lower versions, different content under an existing version, duplicate primary keys, or omission of any previously captured ID fail without publication. Versions may skip numbers. New versions with no content changes are valid delivery heartbeats. An identical-version retransmission does not reset source freshness. Hashes sort by table and ID; row order does not affect the snapshot fingerprint. Raw values are canonicalized as strings (dates ISO) while preserving whitespace and source text; normalized business values are separate.

One authoritative source namespace is allowed per operational database, because generated surrogate IDs are not globally unique across datasets. Changing seeds/namespaces requires a separate database. A source version is an ordering assertion by the producer: do not increase it for an older snapshot. The pipeline can enforce ordering/completeness but cannot infer which upstream content is authoritative.

Source capture, migration, Phase 2 loading, and processing share the existing transaction advisory lock. This serializes raw sequence allocation/commit so a higher committed sequence cannot hide an earlier uncommitted change. Sequence gaps after rollback are harmless. Processing reads an upper bound under the same lock, selects the latest version of each changed entity in `(watermark, upper]`, and advances the checkpoint only after successful publication. If several deliveries arrive before processing, the run consumes the latest committed state, not necessarily only its own delivery; audit metrics distinguish source capture counts and processing watermark/version.

Updates are upserts by stable primary key in dependency order. A late receipt also recomputes the invoice's stored all-time ledger status, even if no invoice row changed upstream. Corrections to amounts, names, or dates propagate if the resulting complete state passes quality checks. Missing source rows are not interpreted as deletes. Deletes/tombstones and primary-key changes are deliberately unsupported; omission fails. Unique-key swaps or amendments violating immediate FK/unique constraints fail safely and require a source-compatible correction strategy. No records are silently overwritten by the legacy immutable loader, or silently discarded by this ETL.

## Financial transformations and quality

Money arrives in named cent columns. Normalization uses Decimal to verify exact integrality and signed SQL type bounds, then converts to integer cents. It never interprets cent values as dollars, rounds fractional cents, or performs floating-point financial arithmetic. ISO dates are parsed to calendar DATEs; text is trimmed, currencies uppercased, and enum text lowercased. Missing/empty required text, invalid dates, fractional/nonfinite/out-of-range numbers, and invalid enums fail. The original raw payload remains available when its identity was unambiguous enough to land.

Invoice status is derived from total ledger receipts: paid when receipts equal total, unpaid when receipts are zero, partial otherwise. Overpayments are rejected by the reused quality validator. Refunds do not reopen receivables; processor settlement differences remain a separate operational metric and are not forced to zero. Valid known pricing differences, discounts, repeated refund requests, and settlement shortfalls remain available for SQL analysis.

Every successful run records 19 quality results:

| Checks | Count | Enforcement |
| --- | ---: | --- |
| Staging aggregate/chronology checks | 7 | Reused Phase 2 quality SQL against staging |
| Normalized schema and business rules | 1 | Reused Python validator plus typed PK/FK/NOT NULL/unique/CHECK constraints; covers nulls, duplicate IDs/keys, references, monetary arithmetic/ranges, status and dates |
| Event recency | 3 | Latest eligible invoice, payment and refund event relative to explicit reporting cutoff |
| Published aggregate/chronology checks | 7 | Reused Phase 2 operational quality SQL |
| Report and customer reconciliation | 1 | Summary vs base tables/monthly/customer totals/aging and persisted customer summary |

Failures roll back typed/public/analytics changes and checkpoint advancement. The independent failure audit preserves completed check measurements when available and adds `pipeline_failure`; invalid identities, duplicate source IDs, or extraction failures may fail before raw landing. Failure counts do not claim that every later check ran. Metrics tagged `rolled_back=true` describe attempted work, not committed inserted/updated records; `watermark_after` is read from durable state after rollback.

Customer summaries reuse `analytics.invoice_balances(as_of)`: invoice count, billed, ledger receipts, settled receipts, refunds, outstanding, and net cash. They include customers created by the cutoff, including zero-activity customers. Summaries are rebuilt atomically each successful run and retain the publishing run ID and cutoff; operational views remain live and all-time where documented in Phase 2. See [financial definitions](sql_analytics.md) for grain, signs, and double-counting controls.

## Execution

Export configuration from the ignored local `.env` as described in the README, then:

```sh
revenue-leakage migrate
revenue-leakage etl-run --mode full --snapshot-version 1 --as-of 2025-12-31
# An identical rerun changes no raw/staging/operational rows:
revenue-leakage etl-run --snapshot-version 1 --as-of 2025-12-31
# The producer writes a complete updated snapshot and its updated operational manifest:
revenue-leakage etl-run --input data/updated --snapshot-version 2 --as-of 2025-12-31
# Consume already-landed changes without reading source files:
revenue-leakage etl-run --replay --as-of 2025-12-31
# Rebuild typed staging from all latest raw records, then reconcile/publication:
revenue-leakage etl-run --replay --mode full --as-of 2025-12-31
revenue-leakage etl-freshness --as-of 2025-12-31
revenue-leakage etl-runs --limit 20
revenue-leakage sql-check --as-of 2025-12-31
```

Use an existing complete snapshot from Phase 1, or a producer-generated compatible snapshot/manifest. The API also accepts DataFrames. `snapshot` can be a callable extractor, so missing/corrupt input files are logged within the run. API: `run_etl(engine, snapshot, snapshot_version=2, options=ETLOptions(as_of=...))`. All seven tables must be present; per-table source row counts and hashes must match. Version 1 can adopt an existing Phase 2 database only if the complete source covers its operational records. Extra public rows absent from the source fail; ETL never deletes them.

A rerun refreshes derived summary lineage and writes a new audit run even when financial values are unchanged. Idempotence applies to source changes and financial state, not to run IDs/timestamps. Full mode rebuilds staging and derived customer summaries; it does not truncate public financial tables or erase raw history.

## Retry, failure and recovery

Each run has a UUID, database start/end timestamps, source/mode/cutoff, attempt count, row metrics, status and safe error details. Each attempt is logged independently before processing. Database error details include type, SQLSTATE and constraint name when available, but omit SQL parameters, credentials, and raw financial values. Operational records remain in restricted raw tables, not exception messages.

Only transient database conditions retry automatically: serialization failures, deadlocks, lock waits, connection-class SQLSTATEs or invalidated connections. Attempts are bounded (1–5) with bounded exponential backoff (10-second cap). Data quality, stale/conflicting version, and deterministic source errors fail immediately. Retries reread the same version safely, preserve raw capture, and reprocess from the last committed checkpoint. A committed success audit row is an atomic commit receipt: if the acknowledgement is lost, the pipeline confirms that row and returns success rather than overwriting it or reapplying data.

If a process dies, a run may remain `running`; its data transaction is rolled back by PostgreSQL, or its atomic success is already committed. A session advisory lock guards each active run across capture/processing transactions. To close an abandoned run:

```sh
revenue-leakage etl-recover --run-id RUN_UUID
revenue-leakage etl-run --replay --as-of 2025-12-31
```

Recovery refuses a live guarded run or a record already finished, and marks the stale run/attempt abandoned without changing the processing watermark. Fix invalid source payloads using a **higher** snapshot version; replaying unchanged bad raw data will fail again. Reuse the same version only for identical content. `etl-run` exits nonzero on failure and prints the run ID; inspect `etl-runs` and the audit tables. If PostgreSQL itself is unavailable, an independent failure write may also be unavailable; after connectivity returns, inspect/recover any running audit record. Migration failures occur before audit tables can be assumed available and surface directly.

## Freshness and configuration

`etl-freshness` checks two distinct clocks. Business-event recency compares each eligible MAX(event_date) with the explicit report cutoff, not today's date. Invoices are required; missing optional payment/refund histories are reported as `missing_optional`. Observed histories older than the configured days fail. Event recency measures inactivity, not proof that an upstream system is unreachable; choose thresholds appropriate to low-volume sources. Future events are excluded from these historical recency checks but may validly exist in the full operational state.

Wall-clock health compares database current time with last accepted new-version receipt and last successful publication. Both must be within `--max-run-age-seconds`. Reprocessing cached source data does not pretend a new upstream delivery arrived. `etl-freshness` emits JSON and exits 1 when any required health check fails. A scheduler can invoke the CLI directly; no paid orchestration service is required.

| Environment option | Default | Meaning |
| --- | --- | --- |
| ETL_SOURCE | financials | Stable source namespace (letters/digits/underscore/hyphen, max 80 chars) |
| ETL_MODE | incremental | incremental or full; `--mode` overrides |
| ETL_MAX_ATTEMPTS | 3 | Maximum attempts, 1–5 |
| ETL_RETRY_DELAY_SECONDS | 0.5 | Initial backoff, finite 0–10 seconds |
| ETL_MAX_DATA_AGE_DAYS | 45 | Business-event recency limit, 0–36,500 days |
| ETL_MAX_RUN_AGE_SECONDS | 86400 | Wall-clock source/publication age threshold for health command |
| ETL_SNAPSHOT_VERSION | absent | Optional producer version; omit/unset it for `--replay` |

Existing `DATABASE_URL`, `TEST_DATABASE_URL`, `DATA_DIR`, `LOAD_BATCH_SIZE`, PostgreSQL timeouts and `ANALYTICS_AS_OF` remain shared. Prefer explicit `--snapshot-version` per delivery. Read-only reporting still uses repeatable-read snapshots. ETL publication serializes writers but permits ordinary reads. Do not write staging/raw/audit tables outside the workflow; after adopting ETL, route source corrections through its versioned capture. Direct operational writes cannot refresh ETL summary lineage and will be checked/reconciled at the next run. No retention/compaction job or delete propagation is implemented; plan raw/audit storage retention and privileges before operating at larger scale.

## Verification

Tests cover exact large cent normalization, malformed/null/duplicate/orphan/negative/fractional data, arithmetic/date/status/refund violations, version/order/completeness/source guards, late old-dated receipts, updates to existing receipts/customers, derived status, unchanged reruns, full rebuild into an empty public target vs incremental results, source extraction failure, transient retry/exhaustion, rollback, corrected higher-version recovery, lost commit acknowledgement, concurrent identical runs, abandoned/live recovery, and event/wall-clock freshness. All prior Phase 1–2 tests remain. CI also runs full-size generation, legacy loading/SQL checks, full ETL, identical rerun, raw replay and freshness on Python 3.11/3.13 with PostgreSQL 16.
