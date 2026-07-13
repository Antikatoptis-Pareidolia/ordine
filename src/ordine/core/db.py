"""SQLite engine factory, pragmas, and schema initialization.

Owns database connectivity and schema bootstrap. Must never contain business logic
or import from executors/web/cli/llm.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from ordine.core.errors import SchemaVersionError
from ordine.core.models import Base

SCHEMA_VERSION = 1


def _set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    dbapi_connection.execute("PRAGMA journal_mode=WAL")
    dbapi_connection.execute("PRAGMA foreign_keys=ON")
    dbapi_connection.execute("PRAGMA busy_timeout=5000")


def create_engine_for(path: Path) -> Engine:
    """Create a SQLite engine with WAL and safety pragmas."""
    absolute = path.expanduser().resolve()
    absolute.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{absolute}", future=True)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def init_db(engine: Engine) -> None:
    """Create tables and verify or set the schema user_version."""
    with engine.connect() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar_one()
        if current not in (0, SCHEMA_VERSION):
            raise SchemaVersionError(
                f"unsupported database schema version {current} (expected {SCHEMA_VERSION})"
            )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar_one()
        if current == 0:
            conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
            conn.commit()


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured SQLAlchemy session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False)
