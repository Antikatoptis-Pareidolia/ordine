"""Hand-rolled SQLite schema migration runner.

Owns version N→N+1 upgrade SQL for the ledger database. Must never import
cli, web, executors, or llm. Fresh installs set ``user_version`` to the latest
schema after ``create_all``; existing databases apply each missing migration
in order.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Connection, text

from ordine.core.errors import SchemaVersionError

# Target schema version for this build. Bump only when shipping a real migration.
SCHEMA_VERSION = 2

MigrationFn = Callable[[Connection], None]


def _migrate_to_2(conn: Connection) -> None:
    """Trivial Phase 2 proof migration: schema meta bookkeeping table."""
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ordine_schema_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT OR IGNORE INTO ordine_schema_meta(key, value)
            VALUES ('framework', 'phase2-migrations')
            """
        )
    )


# Maps *target* version → migration that upgrades (target-1) → target.
MIGRATIONS: dict[int, MigrationFn] = {
    2: _migrate_to_2,
}


def get_user_version(conn: Connection) -> int:
    """Return the SQLite ``user_version`` pragma value."""
    value = conn.execute(text("PRAGMA user_version")).scalar_one()
    return int(value)


def set_user_version(conn: Connection, version: int) -> None:
    """Set the SQLite ``user_version`` pragma (must run inside a transaction)."""
    # PRAGMA does not accept bound parameters for the version integer.
    conn.execute(text(f"PRAGMA user_version = {int(version)}"))


def apply_migrations(conn: Connection, *, from_version: int, to_version: int) -> None:
    """Apply migrations ``from_version+1`` … ``to_version`` inclusive.

    Args:
        conn: Open SQLAlchemy connection (caller owns the transaction).
        from_version: Current on-disk schema version.
        to_version: Desired schema version (normally ``SCHEMA_VERSION``).

    Raises:
        SchemaVersionError: When a required migration step is missing.
    """
    if to_version < from_version:
        raise SchemaVersionError(
            f"cannot downgrade database schema from {from_version} to {to_version}"
        )
    for target in range(from_version + 1, to_version + 1):
        migrate = MIGRATIONS.get(target)
        if migrate is None:
            # Version 1 was bootstrap-only (create_all); no SQL migration entry.
            if target == 1:
                continue
            raise SchemaVersionError(f"missing migration to schema version {target}")
        migrate(conn)
