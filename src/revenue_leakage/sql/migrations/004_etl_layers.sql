CREATE SCHEMA etl;
CREATE SCHEMA raw;
CREATE SCHEMA staging;
CREATE TABLE etl.pipeline_runs (
    run_id uuid PRIMARY KEY,
    source_name varchar(80) NOT NULL,
    mode text NOT NULL CHECK (mode IN ('incremental', 'full')),
    as_of date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'abandoned')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    metrics jsonb NOT NULL DEFAULT '{}',
    error_type text, error_message text,
    CHECK ((status = 'running') = (ended_at IS NULL))
);
CREATE INDEX ix_pipeline_runs_started ON etl.pipeline_runs (started_at DESC);
CREATE TABLE etl.run_attempts (
    run_id uuid NOT NULL REFERENCES etl.pipeline_runs(run_id),
    attempt integer NOT NULL CHECK (attempt > 0),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'abandoned')),
    metrics jsonb NOT NULL DEFAULT '{}',
    error_type text, error_message text, sqlstate text,
    PRIMARY KEY (run_id, attempt),
    CHECK ((status = 'running') = (ended_at IS NULL))
);
-- One authoritative source namespace per operational database.
CREATE TABLE etl.source_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    source_name varchar(80) NOT NULL UNIQUE,
    snapshot_version bigint NOT NULL DEFAULT 0 CHECK (snapshot_version >= 0),
    snapshot_fingerprint varchar(64),
    watermark bigint NOT NULL DEFAULT 0 CHECK (watermark >= 0),
    last_received_at timestamptz,
    last_success_at timestamptz,
    last_run_id uuid REFERENCES etl.pipeline_runs(run_id)
);
CREATE TABLE raw.deliveries (
    source_name varchar(80) NOT NULL REFERENCES etl.source_state(source_name),
    snapshot_version bigint NOT NULL CHECK (snapshot_version > 0),
    fingerprint varchar(64) NOT NULL,
    run_id uuid NOT NULL REFERENCES etl.pipeline_runs(run_id),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    row_counts jsonb NOT NULL,
    changed_rows bigint NOT NULL CHECK (changed_rows >= 0),
    PRIMARY KEY (source_name, snapshot_version)
);
CREATE TABLE raw.changes (
    change_id bigserial PRIMARY KEY,
    source_name varchar(80) NOT NULL,
    snapshot_version bigint NOT NULL,
    table_name text NOT NULL CHECK (table_name IN (
        'customers', 'pricing_plans', 'contracts', 'invoices',
        'invoice_line_items', 'payments', 'refunds'
    )),
    entity_id integer NOT NULL CHECK (entity_id > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_hash varchar(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (source_name, snapshot_version)
        REFERENCES raw.deliveries(source_name, snapshot_version),
    UNIQUE (source_name, snapshot_version, table_name, entity_id)
);
CREATE INDEX ix_raw_changes_watermark ON raw.changes (source_name, change_id);
CREATE TABLE raw.current_records (
    source_name varchar(80) NOT NULL REFERENCES etl.source_state(source_name),
    table_name text NOT NULL,
    entity_id integer NOT NULL,
    change_id bigint NOT NULL REFERENCES raw.changes(change_id),
    payload_hash varchar(64) NOT NULL,
    PRIMARY KEY (source_name, table_name, entity_id)
);
CREATE TABLE etl.quality_results (
    run_id uuid NOT NULL REFERENCES etl.pipeline_runs(run_id),
    attempt integer NOT NULL,
    check_name text NOT NULL,
    violations bigint NOT NULL CHECK (violations >= 0),
    details jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, attempt, check_name),
    FOREIGN KEY (run_id, attempt) REFERENCES etl.run_attempts(run_id, attempt)
);
CREATE TABLE analytics.customer_financials (
    customer_id integer PRIMARY KEY REFERENCES public.customers(id),
    as_of date NOT NULL,
    invoice_count bigint NOT NULL,
    billed_cents numeric(30,0) NOT NULL,
    collected_cents numeric(30,0) NOT NULL,
    settled_cents numeric(30,0) NOT NULL,
    refunded_cents numeric(30,0) NOT NULL,
    outstanding_cents numeric(30,0) NOT NULL,
    net_cash_cents numeric(30,0) NOT NULL,
    run_id uuid NOT NULL REFERENCES etl.pipeline_runs(run_id),
    refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Typed normalized state, with the same operational constraints.
CREATE TABLE staging.customers (
	id SERIAL NOT NULL,
	name VARCHAR(160) NOT NULL,
	email VARCHAR(160) NOT NULL,
	segment VARCHAR(20) NOT NULL,
	created_on DATE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (email)
);

CREATE TABLE staging.pricing_plans (
	id SERIAL NOT NULL,
	name VARCHAR(40) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	seat_price_cents BIGINT NOT NULL,
	usage_price_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (seat_price_cents > 0),
	CHECK (usage_price_cents > 0),
	CHECK (currency = 'USD')
);

CREATE TABLE staging.contracts (
	id SERIAL NOT NULL,
	customer_id INTEGER NOT NULL,
	plan_id INTEGER NOT NULL,
	starts_on DATE NOT NULL,
	ends_on DATE NOT NULL,
	seats INTEGER NOT NULL,
	discount_bps INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (seats > 0),
	FOREIGN KEY(customer_id) REFERENCES staging.customers (id),
	CHECK (ends_on >= starts_on),
	FOREIGN KEY(plan_id) REFERENCES staging.pricing_plans (id),
	CHECK (discount_bps BETWEEN 0 AND 10000)
);

CREATE INDEX ix_staging_contracts_customer_id ON staging.contracts (customer_id);

CREATE INDEX ix_staging_contracts_plan_id ON staging.contracts (plan_id);

CREATE TABLE staging.invoices (
	id SERIAL NOT NULL,
	contract_id INTEGER NOT NULL,
	issued_on DATE NOT NULL,
	due_on DATE NOT NULL,
	currency VARCHAR(3) NOT NULL,
	subtotal_cents BIGINT NOT NULL,
	discount_cents BIGINT NOT NULL,
	total_cents BIGINT NOT NULL,
	status VARCHAR(20) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(contract_id) REFERENCES staging.contracts (id),
	CHECK (due_on >= issued_on),
	CHECK (status IN ('paid', 'partial', 'unpaid')),
	CHECK (subtotal_cents >= 0),
	CHECK (currency = 'USD'),
	CHECK (discount_cents BETWEEN 0 AND subtotal_cents),
	CHECK (total_cents = subtotal_cents - discount_cents),
	UNIQUE (contract_id, issued_on)
);

CREATE INDEX ix_staging_invoices_contract_id ON staging.invoices (contract_id);

CREATE TABLE staging.invoice_line_items (
	id SERIAL NOT NULL,
	invoice_id INTEGER NOT NULL,
	kind VARCHAR(20) NOT NULL,
	quantity INTEGER NOT NULL,
	unit_price_cents BIGINT NOT NULL,
	amount_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (quantity > 0),
	CHECK (kind IN ('subscription', 'usage')),
	UNIQUE (invoice_id, kind),
	CHECK (amount_cents = quantity * unit_price_cents),
	FOREIGN KEY(invoice_id) REFERENCES staging.invoices (id),
	CHECK (unit_price_cents > 0)
);

CREATE INDEX ix_staging_invoice_line_items_invoice_id ON staging.invoice_line_items (invoice_id);

CREATE TABLE staging.payments (
	id SERIAL NOT NULL,
	invoice_id INTEGER NOT NULL,
	paid_on DATE NOT NULL,
	amount_cents BIGINT NOT NULL,
	settled_cents BIGINT NOT NULL,
	processor_reference VARCHAR(80) NOT NULL,
	PRIMARY KEY (id),
	CHECK (amount_cents > 0),
	UNIQUE (processor_reference),
	FOREIGN KEY(invoice_id) REFERENCES staging.invoices (id),
	CHECK (settled_cents >= 0)
);

CREATE INDEX ix_staging_payments_invoice_id ON staging.payments (invoice_id);

CREATE TABLE staging.refunds (
	id SERIAL NOT NULL,
	payment_id INTEGER NOT NULL,
	refunded_on DATE NOT NULL,
	amount_cents BIGINT NOT NULL,
	request_reference VARCHAR(80) NOT NULL,
	PRIMARY KEY (id),
	CHECK (amount_cents > 0),
	FOREIGN KEY(payment_id) REFERENCES staging.payments (id)
);

CREATE INDEX ix_staging_refunds_payment_id ON staging.refunds (payment_id);
