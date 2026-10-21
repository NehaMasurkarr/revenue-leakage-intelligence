-- One row per payment. Refund cutoff is independent of the receipt date cutoff.
WITH payouts AS (
    SELECT payment_id, SUM(amount_cents) AS refunded_cents FROM public.refunds
    WHERE refunded_on <= CAST(:as_of AS date) GROUP BY payment_id
)
SELECT p.id AS payment_id, p.invoice_id, p.paid_on, p.processor_reference,
       p.amount_cents AS ledger_cents, p.settled_cents,
       p.amount_cents - p.settled_cents AS discrepancy_cents,
       COALESCE(r.refunded_cents, 0) AS refunded_cents,
       p.settled_cents - COALESCE(r.refunded_cents, 0) AS net_cash_cents
FROM public.payments p LEFT JOIN payouts r ON r.payment_id = p.id
WHERE p.paid_on <= CAST(:as_of AS date) ORDER BY p.id;
