-- Generated from revenue_leakage.schema; apply to a fresh database.

CREATE TABLE customers (
	id SERIAL NOT NULL,
	name VARCHAR(160) NOT NULL,
	email VARCHAR(160) NOT NULL,
	segment VARCHAR(20) NOT NULL,
	created_on DATE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (email)
);

CREATE TABLE pricing_plans (
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

CREATE TABLE contracts (
	id SERIAL NOT NULL,
	customer_id INTEGER NOT NULL,
	plan_id INTEGER NOT NULL,
	starts_on DATE NOT NULL,
	ends_on DATE NOT NULL,
	seats INTEGER NOT NULL,
	discount_bps INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (ends_on >= starts_on),
	CHECK (seats > 0),
	CHECK (discount_bps BETWEEN 0 AND 10000),
	FOREIGN KEY(customer_id) REFERENCES customers (id),
	FOREIGN KEY(plan_id) REFERENCES pricing_plans (id)
);

CREATE INDEX ix_contracts_customer_id ON contracts (customer_id);

CREATE INDEX ix_contracts_plan_id ON contracts (plan_id);

CREATE TABLE invoices (
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
	CHECK (due_on >= issued_on),
	CHECK (currency = 'USD'),
	CHECK (subtotal_cents >= 0),
	CHECK (discount_cents BETWEEN 0 AND subtotal_cents),
	CHECK (total_cents = subtotal_cents - discount_cents),
	CHECK (status IN ('paid', 'partial', 'unpaid')),
	UNIQUE (contract_id, issued_on),
	FOREIGN KEY(contract_id) REFERENCES contracts (id)
);

CREATE INDEX ix_invoices_contract_id ON invoices (contract_id);

CREATE TABLE invoice_line_items (
	id SERIAL NOT NULL,
	invoice_id INTEGER NOT NULL,
	kind VARCHAR(20) NOT NULL,
	quantity INTEGER NOT NULL,
	unit_price_cents BIGINT NOT NULL,
	amount_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (kind IN ('subscription', 'usage')),
	CHECK (quantity > 0),
	CHECK (unit_price_cents > 0),
	CHECK (amount_cents = quantity * unit_price_cents),
	UNIQUE (invoice_id, kind),
	FOREIGN KEY(invoice_id) REFERENCES invoices (id)
);

CREATE INDEX ix_invoice_line_items_invoice_id ON invoice_line_items (invoice_id);

CREATE TABLE payments (
	id SERIAL NOT NULL,
	invoice_id INTEGER NOT NULL,
	paid_on DATE NOT NULL,
	amount_cents BIGINT NOT NULL,
	settled_cents BIGINT NOT NULL,
	processor_reference VARCHAR(80) NOT NULL,
	PRIMARY KEY (id),
	CHECK (amount_cents > 0),
	CHECK (settled_cents >= 0),
	UNIQUE (processor_reference),
	FOREIGN KEY(invoice_id) REFERENCES invoices (id)
);

CREATE INDEX ix_payments_invoice_id ON payments (invoice_id);

CREATE TABLE refunds (
	id SERIAL NOT NULL,
	payment_id INTEGER NOT NULL,
	refunded_on DATE NOT NULL,
	amount_cents BIGINT NOT NULL,
	request_reference VARCHAR(80) NOT NULL,
	PRIMARY KEY (id),
	CHECK (amount_cents > 0),
	FOREIGN KEY(payment_id) REFERENCES payments (id)
);

CREATE INDEX ix_refunds_payment_id ON refunds (payment_id);
