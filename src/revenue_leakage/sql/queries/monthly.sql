WITH cutoff AS (SELECT CAST(:as_of AS date) AS as_of),
events AS (
    SELECT issued_on AS event_on, total_cents::numeric AS billed_cents,
           0::numeric AS collected_cents, 0::numeric AS settled_cents, 0::numeric AS refunded_cents
    FROM public.invoices, cutoff WHERE issued_on <= as_of
    UNION ALL
    SELECT paid_on, 0, amount_cents, settled_cents, 0
    FROM public.payments, cutoff WHERE paid_on <= as_of
    UNION ALL
    SELECT refunded_on, 0, 0, 0, amount_cents
    FROM public.refunds, cutoff WHERE refunded_on <= as_of
), months AS (
    SELECT generate_series(date_trunc('month', MIN(event_on)::timestamp),
                           date_trunc('month', (SELECT as_of FROM cutoff)::timestamp),
                           interval '1 month')::date AS month FROM events
), totals AS (
    SELECT date_trunc('month', event_on::timestamp)::date AS month,
           SUM(billed_cents) AS billed_cents, SUM(collected_cents) AS collected_cents,
           SUM(settled_cents) AS settled_cents, SUM(refunded_cents) AS refunded_cents
    FROM events GROUP BY 1
)
SELECT m.month, COALESCE(t.billed_cents, 0) AS billed_cents,
       COALESCE(t.collected_cents, 0) AS collected_cents,
       COALESCE(t.settled_cents, 0) AS settled_cents,
       COALESCE(t.refunded_cents, 0) AS refunded_cents,
       COALESCE(t.collected_cents, 0) - COALESCE(t.refunded_cents, 0) AS net_collected_cents,
       COALESCE(t.settled_cents, 0) - COALESCE(t.refunded_cents, 0) AS net_cash_cents
FROM months m LEFT JOIN totals t USING (month) ORDER BY m.month;
