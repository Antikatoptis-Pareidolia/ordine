"""SQLite engine factory, pragmas, schema initialization, and migrations.

Owns database connectivity and schema bootstrap/upgrade. Must never contain
business logic or import from executors/web/cli/llm.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ordine.core.errors import SchemaVersionError
from ordine.core.migrations import (
    SCHEMA_VERSION,
    apply_migrations,
    get_user_version,
    set_user_version,
)
from ordine.core.models import Base

# Re-export so existing ``from ordine.core.db import SCHEMA_VERSION`` keeps working.
__all__ = [
    "SCHEMA_VERSION",
    "create_engine_for",
    "init_db",
    "session_factory",
]


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
    """Create tables and migrate ``user_version`` up to ``SCHEMA_VERSION``.

    Fresh databases (``user_version == 0``) receive ``create_all`` then jump to
    the latest version after applying any post-bootstrap migrations. Existing
    databases apply each N→N+1 migration in order. Unsupported (newer) versions
    hard-fail.
    """
    with engine.connect() as conn:
        current = get_user_version(conn)
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"unsupported database schema version {current} "
                f"(expected 0..{SCHEMA_VERSION}; this build cannot open newer DBs)"
            )

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        current = get_user_version(conn)
        if current == 0:
            # Fresh install: apply migrations that add non-ORM artifacts, then stamp latest.
            apply_migrations(conn, from_version=0, to_version=SCHEMA_VERSION)
            set_user_version(conn, SCHEMA_VERSION)
        elif current < SCHEMA_VERSION:
            apply_migrations(conn, from_version=current, to_version=SCHEMA_VERSION)
            set_user_version(conn, SCHEMA_VERSION)
        # current == SCHEMA_VERSION: nothing to do


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured SQLAlchemy session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False)
