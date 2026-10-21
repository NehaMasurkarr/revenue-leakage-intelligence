CREATE SCHEMA analytics;

-- One row per invoice. Independently aggregate child tables before joining.
CREATE VIEW analytics.invoice_financials AS
WITH receipts AS (
    SELECT invoice_id, SUM(amount_cents) AS collected_cents, SUM(settled_cents) AS settled_cents
    FROM public.payments GROUP BY invoice_id
), payouts AS (
    SELECT p.invoice_id, SUM(r.amount_cents) AS refunded_cents
    FROM public.refunds r JOIN public.payments p ON p.id = r.payment_id
    GROUP BY p.invoice_id
)
SELECT i.id AS invoice_id, c.customer_id, i.contract_id, i.issued_on, i.due_on,
       i.total_cents AS billed_cents,
       COALESCE(p.collected_cents, 0) AS collected_cents,
       COALESCE(p.settled_cents, 0) AS settled_cents,
       COALESCE(r.refunded_cents, 0) AS refunded_cents,
       i.total_cents - COALESCE(p.collected_cents, 0) AS outstanding_cents,
       COALESCE(p.settled_cents, 0) - COALESCE(r.refunded_cents, 0) AS net_cash_cents
FROM public.invoices i JOIN public.contracts c ON c.id = i.contract_id
LEFT JOIN receipts p ON p.invoice_id = i.id
LEFT JOIN payouts r ON r.invoice_id = i.id;

-- Explicit cutoff applies to invoice, payment, and refund event dates, inclusively.
CREATE FUNCTION analytics.invoice_balances(p_as_of date)
RETURNS TABLE (
    invoice_id integer, customer_id integer, contract_id integer,
    issued_on date, due_on date, billed_cents bigint,
    collected_cents numeric, settled_cents numeric, refunded_cents numeric,
    outstanding_cents numeric, net_cash_cents numeric, days_overdue integer
)
LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $function$
    WITH receipts AS (
        SELECT invoice_id, SUM(amount_cents) AS collected, SUM(settled_cents) AS settled
        FROM public.payments WHERE paid_on <= p_as_of GROUP BY invoice_id
    ), payouts AS (
        SELECT p.invoice_id, SUM(r.amount_cents) AS refunded
        FROM public.refunds r JOIN public.payments p ON p.id = r.payment_id
        WHERE r.refunded_on <= p_as_of AND p.paid_on <= p_as_of GROUP BY p.invoice_id
    )
    SELECT i.id, c.customer_id, i.contract_id, i.issued_on, i.due_on, i.total_cents,
           COALESCE(p.collected, 0)::numeric, COALESCE(p.settled, 0)::numeric,
           COALESCE(r.refunded, 0)::numeric,
           (i.total_cents - COALESCE(p.collected, 0))::numeric,
           COALESCE(p.settled, 0)::numeric - COALESCE(r.refunded, 0)::numeric,
           GREATEST(p_as_of - i.due_on, 0)
    FROM public.invoices i JOIN public.contracts c ON c.id = i.contract_id
    LEFT JOIN receipts p ON p.invoice_id = i.id
    LEFT JOIN payouts r ON r.invoice_id = i.id
    WHERE i.issued_on <= p_as_of
$function$;

CREATE VIEW analytics.payment_reconciliation AS
WITH payouts AS (
    SELECT payment_id, SUM(amount_cents) AS refunded_cents FROM public.refunds GROUP BY payment_id
)
SELECT p.id AS payment_id, p.invoice_id, p.paid_on, p.processor_reference,
       p.amount_cents AS ledger_cents, p.settled_cents,
       p.amount_cents - p.settled_cents AS discrepancy_cents,
       COALESCE(r.refunded_cents, 0) AS refunded_cents,
       p.settled_cents - COALESCE(r.refunded_cents, 0) AS net_cash_cents
FROM public.payments p LEFT JOIN payouts r ON r.payment_id = p.id;

CREATE VIEW analytics.pricing_discrepancies AS
SELECT l.id AS line_id, i.id AS invoice_id, c.customer_id, i.issued_on, l.kind,
       l.quantity, l.unit_price_cents AS billed_unit_price_cents,
       CASE l.kind WHEN 'subscription' THEN pp.seat_price_cents
                   ELSE pp.usage_price_cents END AS contracted_unit_price_cents,
       (CASE l.kind WHEN 'subscription' THEN pp.seat_price_cents
                    ELSE pp.usage_price_cents END::numeric - l.unit_price_cents)
           * l.quantity AS gross_price_gap_cents
FROM public.invoice_line_items l JOIN public.invoices i ON i.id = l.invoice_id
JOIN public.contracts c ON c.id = i.contract_id JOIN public.pricing_plans pp ON pp.id = c.plan_id
WHERE l.unit_price_cents <> CASE l.kind WHEN 'subscription' THEN pp.seat_price_cents
                                                      ELSE pp.usage_price_cents END;

CREATE VIEW analytics.discount_patterns AS
SELECT i.id AS invoice_id, c.customer_id, i.issued_on, c.discount_bps,
       i.subtotal_cents, i.discount_cents,
       FLOOR((i.subtotal_cents::numeric * c.discount_bps + 5000) / 10000)
           AS authorized_discount_cents,
       i.discount_cents - FLOOR((i.subtotal_cents::numeric * c.discount_bps + 5000) / 10000)
           AS excess_discount_cents,
       ROUND(10000 * i.discount_cents::numeric / NULLIF(i.subtotal_cents, 0), 2)
           AS applied_discount_bps
FROM public.invoices i JOIN public.contracts c ON c.id = i.contract_id;

CREATE VIEW analytics.repeated_refund_requests AS
SELECT payment_id, request_reference, COUNT(*) AS payout_count,
       SUM(amount_cents) AS total_refunded_cents,
       MIN(refunded_on) AS first_refunded_on, MAX(refunded_on) AS last_refunded_on
FROM public.refunds GROUP BY payment_id, request_reference HAVING COUNT(*) > 1;
