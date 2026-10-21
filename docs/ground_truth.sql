-- Generated from revenue_leakage.schema; apply to a fresh database.

CREATE TABLE leakage_labels (
	id SERIAL NOT NULL,
	invoice_id INTEGER NOT NULL,
	leakage_type VARCHAR(40) NOT NULL,
	entity_table VARCHAR(40) NOT NULL,
	entity_id INTEGER NOT NULL,
	expected_cents BIGINT NOT NULL,
	observed_cents BIGINT NOT NULL,
	leakage_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (leakage_cents > 0),
	CHECK (leakage_cents = expected_cents - observed_cents),
	UNIQUE (invoice_id)
);
