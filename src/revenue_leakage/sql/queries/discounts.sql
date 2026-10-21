SELECT date_trunc('month', issued_on::timestamp)::date AS month, discount_bps,
       COUNT(*) AS invoice_count, SUM(subtotal_cents) AS subtotal_cents,
       SUM(discount_cents) AS applied_discount_cents,
       SUM(authorized_discount_cents) AS authorized_discount_cents,
       SUM(excess_discount_cents) AS signed_excess_discount_cents,
       COUNT(*) FILTER (WHERE excess_discount_cents > 0) AS excessive_discount_count,
       SUM(GREATEST(excess_discount_cents, 0)) AS excessive_discount_cents,
       ROUND(10000 * SUM(discount_cents)::numeric / NULLIF(SUM(subtotal_cents), 0), 2)
           AS weighted_applied_discount_bps
FROM analytics.discount_patterns WHERE issued_on <= CAST(:as_of AS date)
GROUP BY 1, 2 ORDER BY 1, 2;
