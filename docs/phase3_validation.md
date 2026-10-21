# Phase 3 verification

Verified on 2026-10-21 with Python 3.13.1, PostgreSQL 16.2 and the existing locked dependencies. No paid or additional orchestration service was installed.

- Existing Phase 1–2 generator, immutable ingestion, migrations, analytical SQL and tests remain available.
- Migration 004 installs raw capture, typed staging, audit/checkpoint state, quality measurements and the customer summary, without editing historical migrations.
- Full-size initial run captured/staged/published **220,271 financial records**: 10,000 customers, 3 plans, 10,000 contracts, 50,000 invoices, 99,000 line items, 48,144 payments and 3,124 refunds.
- The workflow refreshed 10,000 customer summaries. All **19 quality measurements passed**, with zero violations. Existing portfolio/monthly/customer/aging reconciliations passed.
- An identical-version incremental rerun captured/staged/published **zero financial changes**, retaining watermark 220,271 while creating a new execution audit.
- A second complete source version exercised **three changed raw records**: one corrected customer, one corrected existing processor settlement, and one late payment dated April 11, 2025 (far earlier than the latest December events).
- That late receipt was 7,600 cents. Publication inserted one payment and updated three records: the existing payment, customer and derived invoice status. The checkpoint advanced to 220,274; the final operational payment count was 48,145.
- A full raw replay/rebuild matched the incremental financial summary and required zero operational inserts/updates. Full-versus-incremental equivalence into a cleared disposable target is also automated in integration tests.
- Business-event freshness at the explicit December 31, 2025 cutoff passed: latest invoice age 30 days, latest payment/refund ages zero, against the 45-day threshold. Delivery/publication wall-clock freshness also passed.
- Normalization/quality failures, corrected higher-version recovery, transient retry/exhaustion, rollback after public writes, lost commit acknowledgement, concurrent identical runs, live/abandoned run guards, and extraction failure are covered by automated PostgreSQL tests.
- All **91 tests passed** locally with PostgreSQL enabled (zero skipped), including the preserved Phase 1–2 suite. Ruff lint/format, dependency checks, full-artifact validation and finalized SQL checks passed. The distribution wheel contains all four migrations, ten SQL queries and six ETL modules. Remote CI results are reported in the delivery message.

Initial portfolio financial totals match the Phase 2 verification report. Following the update exercise, billed revenue remains 7,198,591,725 cents; collected receipts become 6,808,378,605 cents, settled receipts 6,803,905,285 cents, refunds remain 45,290,709 cents, and outstanding becomes 390,213,120 cents. These are operational pipeline checks, not comparisons to leakage labels. The source generator and its local CSV artifacts remain unchanged; update payloads and all run-specific/raw outputs stay outside Git.
