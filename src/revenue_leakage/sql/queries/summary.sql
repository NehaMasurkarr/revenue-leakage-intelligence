SELECT COUNT(*) AS invoice_count,
       COALESCE(SUM(billed_cents), 0) AS billed_cents,
       COALESCE(SUM(collected_cents), 0) AS collected_cents,
       COALESCE(SUM(settled_cents), 0) AS settled_cents,
       COALESCE(SUM(refunded_cents), 0) AS refunded_cents,
       COALESCE(SUM(collected_cents - refunded_cents), 0) AS net_collected_cents,
       COALESCE(SUM(net_cash_cents), 0) AS net_cash_cents,
       COALESCE(SUM(outstanding_cents), 0) AS outstanding_cents,
       COALESCE(SUM(collected_cents - settled_cents), 0) AS reconciliation_gap_cents
FROM analytics.invoice_balances(CAST(:as_of AS date));
