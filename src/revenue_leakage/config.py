"""Environment-based PostgreSQL connection and bounded pipeline settings."""

import os

from sqlalchemy import event

from revenue_leakage.storage import engine_for


def configured_engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("Set DATABASE_URL to a PostgreSQL connection URL")
    engine = engine_for(url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("DATABASE_URL must use PostgreSQL")
    statement_timeout = int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "120000"))
    lock_timeout = int(os.environ.get("PG_LOCK_TIMEOUT_MS", "10000"))
    if statement_timeout <= 0 or lock_timeout <= 0:
        engine.dispose()
        raise ValueError("PostgreSQL timeouts must be positive milliseconds")

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        # Set session defaults outside a transaction so SQLAlchemy's rollback cannot undo them.
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, false)", (str(statement_timeout),)
            )
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", (str(lock_timeout),))
            cursor.execute("SELECT set_config('search_path', 'public', false)")
        connection.autocommit = False

    return engine
