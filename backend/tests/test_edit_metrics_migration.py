"""Alembic and desktop SQLite migration coverage for the nullable metrics cache."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from db.session import _ensure_incremental_columns, _missing_sqlite_model_columns
from models import Base


def _isolated_env(database_url: str) -> dict[str, str]:
    allowed = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    allowed.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "APP_ENV": "development",
        "JWT_SECRET": "aicp-test-fake-jwt-secret-not-real",
        "DATABASE_URL": database_url,
    })
    return allowed


def _alembic(db_path, *args):
    url = f"sqlite+aiosqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=Path(__file__).parents[1],
        env=_isolated_env(url),
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
    )


def _sqlite_columns(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(generation_records)")}


def test_alembic_empty_and_old_database_upgrade_and_downgrade(tmp_path):
    empty_db = tmp_path / "empty.sqlite3"
    old_db = tmp_path / "old.sqlite3"

    # Existing 001-036 migrations include PostgreSQL-only JSONB declarations, so
    # exercise this cross-dialect migration from its immediately previous revision.
    # Full-chain PostgreSQL upgrade/downgrade is also verified with a disposable PG.
    _alembic(empty_db, "stamp", "036_generation_record_superseded")
    _alembic(empty_db, "upgrade", "head")
    with sqlite3.connect(old_db) as connection:
        connection.execute(
            "CREATE TABLE generation_records ("
            "id CHAR(36) PRIMARY KEY, status VARCHAR(20), accepted_version_id CHAR(36))"
        )
    _alembic(old_db, "stamp", "036_generation_record_superseded")
    _alembic(old_db, "upgrade", "head")
    with sqlite3.connect(empty_db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "generation_records" not in tables
    assert "human_edit_metrics" in _sqlite_columns(str(old_db))

    _alembic(empty_db, "downgrade", "036_generation_record_superseded")
    _alembic(old_db, "downgrade", "036_generation_record_superseded")
    assert "human_edit_metrics" not in _sqlite_columns(str(old_db))

    _alembic(old_db, "upgrade", "head")
    assert "human_edit_metrics" in _sqlite_columns(str(old_db))


def test_desktop_incremental_migration_adds_metrics_column_to_legacy_table(tmp_path):
    database_path = tmp_path / "desktop-legacy.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE generation_records DROP COLUMN human_edit_metrics"))
            before = {row[1] for row in (await conn.execute(text("PRAGMA table_info(generation_records)"))).all()}
            assert "human_edit_metrics" not in before
            await _ensure_incremental_columns(conn)
            after = {row[1] for row in (await conn.execute(text("PRAGMA table_info(generation_records)"))).all()}
            assert "human_edit_metrics" in after
            assert await _missing_sqlite_model_columns(conn) == {}

    try:
        asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())
