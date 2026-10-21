# Financial SQL definitions and interpretation

All amounts are USD cents. Amount columns are integral; ratios use exact NUMERIC division with explicit rounding. All activity uses operational records, with no label lookup, model, or inferred missing usage. `:as_of` is a bound DATE parameter (inclusive). Reports run in a repeatable-read read-only snapshot. A historical month containing the cutoff is month-to-date; future invoices, payments, and refunds are excluded using each event's own date. Stored invoice status reflects all-time ledger payments and is not used for historical balances.

## Metric contract

| Metric | Definition | Grain and timing |
| --- | --- | --- |
| Billed revenue | SUM(invoice.total_cents), after applied discounts | One invoice, issued_on |
| Collected revenue | SUM(payment.amount_cents), gross ledger receipts | One payment, paid_on |
| Settled receipts | SUM(payment.settled_cents) | One payment, paid_on proxy; no settlement-date column exists |
| Refunds | SUM(refund.amount_cents), positive cash outflows including repeated payouts | One refund, refunded_on |
| Net collected | Collected receipts minus refunds | Event-dated cash measure based on the ledger |
| Net cash | Settled receipts minus refunds | Event-dated processor cash proxy, no fees modeled |
| Outstanding | Billed minus ledger receipts through cutoff | One invoice as of cutoff; refunds do not reopen receivables |
| Reconciliation gap | Ledger receipts minus settled receipts | One payment; positive is a settlement shortfall, negative an excess |
| Days overdue | GREATEST(as_of − due_on, 0) | One issued invoice; zero includes future due dates and invoices due on cutoff |
| Aging | Positive outstanding assigned to current, 1–30, 31–60, 61–90, 91+ days overdue | One invoice in exactly one bucket |
| Gross price gap | (Contractual unit price − billed unit price) × billed quantity | Existing line item, before discounts; signed |
| Authorized discount | FLOOR((actual billed subtotal × contractual bps + 5000) / 10000) | One invoice; same half-up cent rounding as generator |
| Excess discount | Applied discount minus authorized discount on actual subtotal | One invoice; positive means more discount than authorized |
| Weighted applied discount bps | 10,000 × SUM(discount) / SUM(subtotal) | Monthly contractual-discount cohort; NULL if subtotal is zero |

Billing and collections are operational revenue measures, not GAAP/IFRS recognized revenue. Refunds are actual payouts, not credit notes; no cancellation/write-off model exists. Receivables use ledger amounts; settlement gaps stay separate so they are not counted twice as unpaid invoices. Negative net monthly cash is possible in a refund-heavy month. No collected/billed percentage is labeled a cohort collection rate because these event-month amounts can refer to different invoices.

## Packaged queries

Run `revenue-leakage analytics --query NAME --as-of YYYY-MM-DD`. Source SQL is in `src/revenue_leakage/sql/queries` and distributed in the Python package.

| Name | Output grain and SQL techniques | Interpretation |
| --- | --- | --- |
| summary | One cutoff total; SUM and cutoff-aware SQL function | Portfolio billed, collected, settled, refunds, net cash, outstanding, signed settlement gap |
| monthly | One calendar month; CTEs, UNION ALL event stream, date_trunc, generate_series, subquery, left join | Independent event-month revenue/cash; zero months retained from first eligible event through cutoff |
| outstanding | One invoice with positive balance; function and filtering | Operational receivables queue, ordered by due date |
| aging | Five fixed buckets; CASE, CTE, left join and aggregation | Receivables aging; zero buckets retained, fully paid invoices excluded |
| reconciliation | One payment; refund preaggregation and left join | Signed settlement difference, cutoff-specific refund total and net cash |
| pricing | One existing line with different unit price; multi-table joins and CASE | Signed gross contract-price variance, not estimated recoverable loss |
| discounts | One month/contractual-bps cohort; conditional aggregation, FILTER, NUMERIC ratios | Discount mix and excess relative to authorized discount on billed subtotal |
| customer_trends | One customer/calendar month; dense calendar, UNION ALL, LAG, cumulative/trailing windows | Billed and cash activity with month-over-month absolute and percentage changes |
| refund_requests | One payment/request with multiple payouts by cutoff; GROUP BY/HAVING | Repeated logical request; investigation evidence, not proof of an improper refund |

Customer calendars begin at the portfolio's first eligible event month, retaining customers created by that month-end, and continue through the cutoff. Customers not yet created at the cutoff are excluded. Inactive months are zeros, so a LAG compares adjacent calendar months rather than adjacent invoice months. First-month prior revenue and percentage changes from a zero denominator are NULL. Trailing three-month billed sums include the current and prior two calendar months; at the beginning they use available history. Portfolio-restricted cumulative totals start at this report calendar, not at any unmodeled earlier history. Late receipts/refunds remain attributed to their actual event month and original customer.

## Views and function

Migration 003 creates five **all-time** views: `analytics.invoice_financials` (one invoice), `payment_reconciliation` (one payment), `pricing_discrepancies` (one differing line), `discount_patterns` (one invoice), and `repeated_refund_requests` (one repeated payment/request). The SQL function `analytics.invoice_balances(date)` returns cutoff-specific invoice balances and days overdue. The views always use all stored records; use the CLI queries/function for historical reporting. There are five views and one function in total.

Payments and refunds are independently aggregated to invoice grain before joining invoices. Refunds are aggregated to payment grain before joining payment rows. Monthly and customer cash queries use UNION ALL events rather than joining raw invoices, payments, and refunds. Never SUM invoice totals over raw child joins: two payments and two refunds would multiply invoice and receipt values. Tests include split receipts and multiple refunds on one payment to catch that failure.

The `pricing` report can compare prices only on recorded quantities. It cannot quantify a missing usage line without an independent operational usage source, and does not consult truth. Pricing gaps are gross, while excess discounts depend on the billed subtotal: do not add these reports as an estimate of total recovery. Tier prices are fixed in Phase 1; real price-history/contract amendments would require effective-date modeling. Repeated refund requests remain visible without asserting authorization or assigning leakage labels.

## Validation

`revenue-leakage sql-check --as-of 2025-12-31` requires zero line-total, payment-status, refund-limit, and chronology violations. It reconciles summary metrics with direct base-table subqueries, independent monthly totals, customer-month totals, aging totals, and billed-minus-collected receivables. Foreign keys, row arithmetic checks, unique keys, and positive ID checks are enforced by PostgreSQL on every write. Quality checks intentionally permit pricing differences, excess discounts, repeated refund requests, and processor shortfalls so operational analysis remains possible.
