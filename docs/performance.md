# PostgreSQL SQL performance

The loader runs ANALYZE after a successful load. All FK columns already have indexes in migration 001. Migration 002 adds workload-specific B-tree indexes:

| Index | Purpose |
| --- | --- |
| ix_invoices_issued_cover (issued_on) INCLUDE (total_cents, contract_id) | Selective billing-date ranges; covered totals/customer-contract linkage |
| ix_invoices_due (due_on, id) INCLUDE (total_cents) | Due-date queues and selective aging investigations |
| ix_payments_paid_cover (paid_on) INCLUDE (invoice_id, amount_cents, settled_cents) | Receipt-date cutoffs and date-range cash aggregation |
| ix_payments_invoice_paid (invoice_id, paid_on) | Per-invoice receipt history through cutoff |
| ix_refunds_date_cover (refunded_on) INCLUDE (payment_id, amount_cents) | Refund-date cutoffs and cash aggregation |
| ix_refunds_request (payment_id, request_reference) | Repeated-request grouping and request history |

Indexes trade insert/storage cost for selective reads. Full-portfolio reports often correctly use sequential scans and hash aggregation. A due-date index cannot by itself accelerate a full portfolio balance calculation that still needs all receipts. The system does not force index scans, materialize views, denormalize balances, or disable planner settings. Append loads serialize writers briefly; large production volumes may justify COPY-based staging, partitioning, or incremental summaries after measurement.

## Reproduce plans

```sh
revenue-leakage explain --query monthly --as-of 2025-12-31
revenue-leakage explain --query customer_trends --as-of 2025-12-31
```

The CLI uses `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` in a read-only transaction and binds the cutoff. It executes a SELECT and reports its measured plan. Inspect actual vs estimated rows, shared hits/reads, heap fetches, join/aggregate methods, sorts/temp spills, and execution time. The fixed-search-path invoice-balances function can appear as a Function Scan; inspect `analytics.invoice_financials` or the function's SQL body for underlying joins when tuning.

For a selective billing-range benchmark, execute this SQL on a disposable populated database after ANALYZE:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT SUM(total_cents)
FROM public.invoices
WHERE issued_on >= DATE '2025-12-01'
  AND issued_on < DATE '2026-01-01';
```

Filter base DATE columns with range predicates. Wrapping the date column in `date_trunc` in a WHERE predicate generally prevents using the plain date B-tree as a simple range search. Date functions remain appropriate in projection/grouping after filtering. Preaggregate each child table at the join grain to reduce cardinality and prevent double-counting.

To measure a baseline without that new date index, use a **disposable database** and a transaction: `BEGIN; DROP INDEX public.ix_invoices_issued_cover;` then the same EXPLAIN, then `ROLLBACK;`. Rollback restores the index. This is a benchmark operation, not a production migration or a recommended application action.

## Measured local results

2026-10-21, PostgreSQL 16.2, Apple Silicon, seed 42, 50,000 invoices; analyzed full dataset. A single warm-cache comparison (not a service-level guarantee):

| Selective December billing sum | Without date index | With date index |
| --- | --- | --- |
| Scan | Sequential scan | Index-only scan, ix_invoices_issued_cover |
| Matching invoice rows | 531 | 531 |
| Rows filtered out | 49,469 | 0 |
| Shared buffer hits | 516 | 5 |
| Heap fetches | N/A | 0 |
| Execution time | 1.834 ms | 0.055 ms |

The index remained installed after the rollback comparison. Index-only scans depend on visibility-map state; recently modified tables may require heap fetches until autovacuum/VACUUM updates visibility. ANALYZE updates statistics, not visibility.

All nine operational queries were also executed and explained on the full dataset. Example execution times: monthly 85.295 ms (24 rows), reconciliation 11.852 ms (48,144 payments), customer trends 623.240 ms (156,909 customer-months), and repeated refund requests 0.977 ms (1,000 groups). These are single local observations; cache, hardware, statistics, data distribution, and PostgreSQL version affect plans and timings. Raw per-record report outputs and generated query-plan JSON stay outside Git.

PostgreSQL references: [EXPLAIN](https://www.postgresql.org/docs/16/using-explain.html), [covering/index-only scans](https://www.postgresql.org/docs/16/indexes-index-only-scans.html), and [locking](https://www.postgresql.org/docs/16/explicit-locking.html).
