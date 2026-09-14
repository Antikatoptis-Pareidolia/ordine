"""A2: schema migration framework."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ordine.core.db import SCHEMA_VERSION, create_engine_for, init_db
from ordine.core.errors import SchemaVersionError
from ordine.core.migrations import MIGRATIONS, apply_migrations, get_user_version, set_user_version


def test_fresh_install_stamps_latest(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "fresh.db")
    init_db(engine)
    with engine.connect() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        rows = conn.execute(
            text("SELECT value FROM ordine_schema_meta WHERE key='framework'")
        ).all()
        assert rows == [("phase2-migrations",)]


def test_upgrade_from_version_1(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    engine = create_engine_for(path)
    # Simulate a Phase-1 database (user_version=1, no meta table).
    with engine.begin() as conn:
        set_user_version(conn, 1)
    assert SCHEMA_VERSION >= 2
    init_db(engine)
    with engine.connect() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        value = conn.execute(
            text("SELECT value FROM ordine_schema_meta WHERE key='framework'")
        ).scalar_one()
        assert value == "phase2-migrations"


def test_missing_migration_raises(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "gap.db")
    with engine.begin() as conn:
        set_user_version(conn, 0)
        with pytest.raises(SchemaVersionError, match="missing migration"):
            apply_migrations(conn, from_version=50, to_version=51)


def test_migrations_registry_covers_targets() -> None:
    assert 2 in MIGRATIONS
    assert SCHEMA_VERSION >= 2
