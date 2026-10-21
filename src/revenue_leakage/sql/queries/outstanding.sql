SELECT * FROM analytics.invoice_balances(CAST(:as_of AS date))
WHERE outstanding_cents > 0 ORDER BY due_on, invoice_id;
