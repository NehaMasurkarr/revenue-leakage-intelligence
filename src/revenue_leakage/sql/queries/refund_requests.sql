SELECT r.payment_id, r.request_reference, COUNT(*) AS payout_count,
       SUM(r.amount_cents) AS total_refunded_cents,
       MIN(r.refunded_on) AS first_refunded_on, MAX(r.refunded_on) AS last_refunded_on
FROM public.refunds r WHERE r.refunded_on <= CAST(:as_of AS date)
GROUP BY r.payment_id, r.request_reference HAVING COUNT(*) > 1
ORDER BY r.payment_id, r.request_reference;
