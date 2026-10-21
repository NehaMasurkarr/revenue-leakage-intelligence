WITH cutoff AS (SELECT CAST(:as_of AS date) AS as_of),
events AS (
    SELECT c.customer_id, i.issued_on AS event_on, i.total_cents::numeric AS billed_cents,
           0::numeric AS collected_cents, 0::numeric AS settled_cents, 0::numeric AS refunded_cents
    FROM public.invoices i JOIN public.contracts c ON c.id = i.contract_id, cutoff
    WHERE i.issued_on <= as_of
    UNION ALL
    SELECT c.customer_id, p.paid_on, 0, p.amount_cents, p.settled_cents, 0
    FROM public.payments p JOIN public.invoices i ON i.id = p.invoice_id
    JOIN public.contracts c ON c.id = i.contract_id, cutoff WHERE p.paid_on <= as_of
    UNION ALL
    SELECT c.customer_id, r.refunded_on, 0, 0, 0, r.amount_cents
    FROM public.refunds r JOIN public.payments p ON p.id = r.payment_id
    JOIN public.invoices i ON i.id = p.invoice_id
    JOIN public.contracts c ON c.id = i.contract_id, cutoff WHERE r.refunded_on <= as_of
), months AS (
    SELECT generate_series(date_trunc('month', MIN(event_on)::timestamp),
                           date_trunc('month', (SELECT as_of FROM cutoff)::timestamp),
                           interval '1 month')::date AS month FROM events
), monthly AS (
    SELECT customer_id, date_trunc('month', event_on::timestamp)::date AS month,
           SUM(billed_cents) AS billed_cents, SUM(collected_cents) AS collected_cents,
           SUM(settled_cents) AS settled_cents, SUM(refunded_cents) AS refunded_cents
    FROM events GROUP BY 1, 2
), dense AS (
    SELECT c.id AS customer_id, m.month,
           COALESCE(t.billed_cents, 0) AS billed_cents,
           COALESCE(t.collected_cents, 0) AS collected_cents,
           COALESCE(t.settled_cents, 0) AS settled_cents,
           COALESCE(t.refunded_cents, 0) AS refunded_cents
    FROM public.customers c CROSS JOIN months m CROSS JOIN cutoff
    LEFT JOIN monthly t ON t.customer_id = c.id AND t.month = m.month
    WHERE c.created_on < (m.month + interval '1 month') AND c.created_on <= cutoff.as_of
), trends AS (
    SELECT *, LAG(billed_cents) OVER (PARTITION BY customer_id ORDER BY month)
                  AS previous_month_billed_cents,
           SUM(billed_cents) OVER (PARTITION BY customer_id ORDER BY month
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                  AS cumulative_billed_cents,
           SUM(billed_cents) OVER (PARTITION BY customer_id ORDER BY month
                                  ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
                  AS trailing_three_month_billed_cents
    FROM dense
)
SELECT *, settled_cents - refunded_cents AS net_cash_cents,
       billed_cents - previous_month_billed_cents AS month_over_month_change_cents,
       ROUND(100 * (billed_cents - previous_month_billed_cents)::numeric
             / NULLIF(previous_month_billed_cents, 0), 2) AS month_over_month_change_pct
FROM trends ORDER BY customer_id, month;
