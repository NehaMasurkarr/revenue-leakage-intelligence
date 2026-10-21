WITH buckets(sort_order, bucket) AS (
    VALUES (1, 'current'), (2, '1-30'), (3, '31-60'), (4, '61-90'), (5, '91+')
), balances AS (
    SELECT *, CASE WHEN days_overdue = 0 THEN 'current'
                   WHEN days_overdue <= 30 THEN '1-30'
                   WHEN days_overdue <= 60 THEN '31-60'
                   WHEN days_overdue <= 90 THEN '61-90' ELSE '91+' END AS bucket
    FROM analytics.invoice_balances(CAST(:as_of AS date)) WHERE outstanding_cents > 0
)
SELECT b.bucket, COUNT(i.invoice_id) AS invoice_count,
       COALESCE(SUM(i.outstanding_cents), 0) AS outstanding_cents
FROM buckets b LEFT JOIN balances i USING (bucket)
GROUP BY b.sort_order, b.bucket ORDER BY b.sort_order;
