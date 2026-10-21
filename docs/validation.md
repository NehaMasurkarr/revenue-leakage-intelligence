# Phase 1 validation

Verified locally on 2026-10-21 with Python 3.13.1 and the versions in `requirements.lock`.

- Default generation: passed for January 2024 through December 2025 (24 distinct billing months).
- Customers: 10,000; contracts: 10,000; pricing plans: 3.
- Invoices: 50,000; line items: 99,000; payments: 48,144; refunds: 3,124.
- Separate labels: 5,000, with exactly 1,000 events for each of the five leakage types.
- Total injected leakage: 66,872,421 cents ($668,724.21).
- Two independent full-size runs: identical manifests and SHA-256 checksums for all eight CSV artifacts.
- CLI validation: passed, including checksums, relational integrity, arithmetic, chronology, and truth validation.
- Full PostgreSQL operational load: passed; 50,000 persisted invoices and no label table in the operational database.
- pytest with PostgreSQL integration enabled: 24 passed, zero skipped.
- Ruff lint and formatting checks: passed.
- `pip check`: passed.

PostgreSQL was started from temporary binaries on a local Unix socket for verification; no system database service was installed. Generated CSVs, virtual environments, and local database files are excluded from Git. The temporary database server was stopped after verification. The optional Docker Compose configuration supports ongoing local PostgreSQL use.

CI is configured to repeat tests on Python 3.11 and 3.13 with PostgreSQL 16 and run default generation plus CLI validation. Local Python 3.11 execution and remote CI results are not implied by this report.
