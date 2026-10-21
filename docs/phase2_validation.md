# Phase 2 validation

Verified locally on 2026-10-21 with Python 3.13.1, PostgreSQL 16.2, and the committed dependency lock.

- Phase 1 dataset and generator retained unchanged.
- Existing Phase 1 database adopted by migrations without replacing records.
- Fresh database: all seven generated operational tables loaded, including 10,000 customers and 50,000 invoices.
- Repeat full-size load: zero inserted rows for every table, identical operational fingerprint, one load-audit entry.
- All nine operational SQL reports executed and explained successfully on the full dataset.
- SQL quality checks: zero violations across all seven aggregate/chronology assertions.
- Summary reconciled to base-table sums, monthly event totals, customer-month totals, receivables arithmetic, and aging.
- PostgreSQL integration suite includes migration replay/checksum failure, baseline adoption/rejection, immutable-record rollback, append/sequence behavior, concurrent loads, split payments/refunds, date cutoffs, dense month windows, and aging boundaries.
- All 44 tests passed locally with PostgreSQL integration enabled (zero skipped), including the preserved Phase 1 suite. Remote CI results are reported in the delivery message.
- Ruff lint/format and dependency checks passed. A distribution wheel was built and verified to contain all three migrations and ten query files.
- No label files are opened by ingestion or analytics; no ground-truth table is installed in operations.
- Generated datasets, raw analytics results/plans, temporary databases, and secrets remain untracked.

## Default dataset metrics as of 2025-12-31

| Metric | Integer cents | USD interpretation |
| --- | ---: | ---: |
| Billed revenue | 7,198,591,725 | $71,985,917.25 |
| Gross collected revenue | 6,808,371,005 | $68,083,710.05 |
| Settled receipts | 6,803,897,705 | $68,038,977.05 |
| Actual refunds | 45,290,709 | $452,907.09 |
| Net collected | 6,763,080,296 | $67,630,802.96 |
| Net cash | 6,758,606,996 | $67,586,069.96 |
| Outstanding receivables | 390,220,720 | $3,902,207.20 |
| Signed settlement gap | 4,473,300 | $44,733.00 |

The 3,833 positive-balance invoices reconcile across aging: current 3,938,158 cents; 1–30 days 6,058,787 cents; 31–60 days zero; 61–90 days 6,586,378 cents; 91+ days 373,637,397 cents. Refunds do not reopen receivables. Monthly reporting has 24 rows; customer reporting has 156,909 customer-month rows. Operational SQL finds 1,000 nonzero payment gaps, 1,000 price-variance lines, and 1,000 repeated payment/request groups. These are SQL observations, not model outputs or truth-label comparisons. Gross price variance is 28,877,080 cents; positive excess discount is 14,131,051 cents; those measures must not be added as a recovery estimate.

See [metric definitions](sql_analytics.md) and [measured query plans](performance.md).
