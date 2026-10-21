"""Checksum-verified, forward-only PostgreSQL SQL migrations."""

import hashlib
import re
from importlib.resources import files

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text

from revenue_leakage.schema import TABLES

LOCK_ID = 824_619_207


def sql_resource(group, name):
    return files("revenue_leakage").joinpath("sql", group, name).read_text()


def lock(connection):
    if connection.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required")
    connection.exec_driver_sql("SET LOCAL search_path = public")
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
    if inspect(connection).has_table("leakage_labels"):
        raise ValueError("Operational database must not contain ground truth")


def _normalize(value):
    return re.sub(r"[\s()\"']", "", value).lower().replace("::text", "")


def _verify_baseline(connection):
    """Reject partial/incompatible Phase 1 schemas instead of silently adopting them."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names()) & set(TABLES)
    if not existing:
        return
    if existing != set(TABLES):
        raise ValueError("Partial operational schema; restore the complete Phase 1 schema first")
    for name, table in TABLES.items():
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        if set(actual) != set(table.columns.keys()):
            raise ValueError(f"Incompatible columns: {name}")
        for column in table.columns:
            found = actual[column.name]
            expected_type = str(column.type.compile(dialect=connection.dialect))
            if str(found["type"]) != expected_type or found["nullable"] != column.nullable:
                raise ValueError(f"Incompatible column: {name}.{column.name}")
        if inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
            raise ValueError(f"Incompatible primary key: {name}")
        fks = {
            (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
            for f in inspector.get_foreign_keys(name)
        }
        for fk in table.foreign_key_constraints:
            expected = (
                tuple(fk.column_keys),
                fk.referred_table.name,
                tuple(e.column.name for e in fk.elements),
            )
            if expected not in fks:
                raise ValueError(f"Missing foreign key: {name}")
        unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                if tuple(c.name for c in constraint.columns) not in unique:
                    raise ValueError(f"Missing unique constraint: {name}")
            if isinstance(constraint, CheckConstraint):
                # PostgreSQL rewrites IN/BETWEEN expressions; verify those through catalog DDL
                # comparison after normalizing their standard rewrites.
                expected = _canonical_check(str(constraint.sqltext))
                checks = {
                    _canonical_check(c["sqltext"]) for c in inspector.get_check_constraints(name)
                }
                if expected not in checks:
                    raise ValueError(f"Missing check constraint: {name}: {constraint.sqltext}")


def _canonical_check(value):
    value = re.sub(r"(\w+) BETWEEN (\w+) AND (\w+)", r"\1 >= \2 AND \1 <= \3", value)
    value = re.sub(r"::(?:text|bpchar|character varying|bigint|integer)(?:\[\])?", "", value)
    return _normalize(value).replace("[", "").replace("]", "").replace("=anyarray", "in")


def migrate_connection(connection):
    lock(connection)
    connection.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version text PRIMARY KEY, checksum varchar(64) NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
    """)
    applied = dict(
        connection.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    )
    resources = sorted(
        files("revenue_leakage").joinpath("sql", "migrations").iterdir(), key=lambda p: p.name
    )
    known = {p.name for p in resources if p.name.endswith(".sql")}
    if set(applied) - known:
        raise ValueError("Database contains unknown migration versions")
    completed = []
    for path in resources:
        if not path.name.endswith(".sql"):
            continue
        sql = path.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise ValueError(f"Migration checksum mismatch: {path.name}")
            continue
        if path.name.startswith("001_"):
            _verify_baseline(connection)
        # psycopg simple-query protocol supports dollar-quoted function bodies and SQL batches.
        connection.exec_driver_sql(sql)
        connection.execute(
            text("INSERT INTO schema_migrations(version, checksum) VALUES (:version, :checksum)"),
            {"version": path.name, "checksum": checksum},
        )
        completed.append(path.name)
    return completed


def migrate(engine):
    with engine.begin() as connection:
        return migrate_connection(connection)
