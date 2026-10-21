-- Date range predicates and foreign-key joins; deliberately preserve valid business anomalies.
CREATE INDEX ix_invoices_issued_cover ON invoices (issued_on) INCLUDE (total_cents, contract_id);
CREATE INDEX ix_invoices_due ON invoices (due_on, id) INCLUDE (total_cents);
CREATE INDEX ix_payments_paid_cover ON payments (paid_on) INCLUDE (invoice_id, amount_cents, settled_cents);
CREATE INDEX ix_payments_invoice_paid ON payments (invoice_id, paid_on);
CREATE INDEX ix_refunds_date_cover ON refunds (refunded_on) INCLUDE (payment_id, amount_cents);
CREATE INDEX ix_refunds_request ON refunds (payment_id, request_reference);
ALTER TABLE customers ADD CONSTRAINT ck_customers_positive_id CHECK (id > 0);
ALTER TABLE pricing_plans ADD CONSTRAINT ck_plans_positive_id CHECK (id > 0);
ALTER TABLE contracts ADD CONSTRAINT ck_contracts_positive_id CHECK (id > 0);
ALTER TABLE invoices ADD CONSTRAINT ck_invoices_positive_id CHECK (id > 0);
ALTER TABLE invoice_line_items ADD CONSTRAINT ck_lines_positive_id CHECK (id > 0);
ALTER TABLE payments ADD CONSTRAINT ck_payments_positive_id CHECK (id > 0);
ALTER TABLE refunds ADD CONSTRAINT ck_refunds_positive_id CHECK (id > 0);
CREATE TABLE public.data_loads (
    fingerprint varchar(64) PRIMARY KEY,
    row_counts jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_load_fingerprint CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_load_counts CHECK (jsonb_typeof(row_counts) = 'object')
);
