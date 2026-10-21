SELECT * FROM analytics.pricing_discrepancies
WHERE issued_on <= CAST(:as_of AS date) ORDER BY invoice_id, line_id;
