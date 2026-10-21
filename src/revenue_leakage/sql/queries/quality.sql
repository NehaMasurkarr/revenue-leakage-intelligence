-- Each result is an invariant violation count, not a leakage label.
WITH line_totals AS (
    SELECT invoice_id, SUM(amount_cents) AS subtotal, COUNT(*) AS line_count
    FROM public.invoice_line_items GROUP BY invoice_id
), payment_totals AS (
    SELECT invoice_id, SUM(amount_cents) AS collected FROM public.payments GROUP BY invoice_id
), refund_totals AS (
    SELECT payment_id, SUM(amount_cents) AS refunded FROM public.refunds GROUP BY payment_id
)
SELECT 'invoice_line_totals' AS check_name, COUNT(*) AS violations
FROM public.invoices i LEFT JOIN line_totals l ON l.invoice_id = i.id
WHERE l.line_count IS NULL OR i.subtotal_cents <> l.subtotal
UNION ALL
SELECT 'invoice_payment_status', COUNT(*)
FROM public.invoices i LEFT JOIN payment_totals p ON p.invoice_id = i.id
WHERE COALESCE(p.collected, 0) > i.total_cents
   OR (i.status = 'paid') <> (COALESCE(p.collected, 0) = i.total_cents)
   OR (i.status = 'unpaid') <> (COALESCE(p.collected, 0) = 0)
UNION ALL
SELECT 'refund_limit', COUNT(*) FROM public.payments p
JOIN refund_totals r ON r.payment_id = p.id WHERE r.refunded > p.amount_cents
UNION ALL
SELECT 'invoice_contract_dates', COUNT(*) FROM public.invoices i
JOIN public.contracts c ON c.id = i.contract_id
WHERE i.issued_on < c.starts_on OR i.issued_on > c.ends_on
UNION ALL
SELECT 'customer_contract_dates', COUNT(*) FROM public.contracts c
JOIN public.customers u ON u.id = c.customer_id WHERE u.created_on > c.starts_on
UNION ALL
SELECT 'payment_dates', COUNT(*) FROM public.payments p
JOIN public.invoices i ON i.id = p.invoice_id WHERE p.paid_on < i.issued_on
UNION ALL
SELECT 'refund_dates', COUNT(*) FROM public.refunds r
JOIN public.payments p ON p.id = r.payment_id WHERE r.refunded_on < p.paid_on;
