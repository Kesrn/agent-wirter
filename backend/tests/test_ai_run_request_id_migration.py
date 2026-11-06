from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from db.session import _ensure_incremental_columns
from models import Base


def _isolated_env(database_url: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "APP_ENV": "development",
        "JWT_SECRET": "aicp-test-fake-jwt-secret-not-real",
        "DATABASE_URL": database_url,
    })
    return env


def _alembic(database_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=Path(__file__).parents[1],
        env=_isolated_env(f"sqlite+aiosqlite:///{database_path}"),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def test_new_metadata_and_desktop_incremental_migration_include_request_id(tmp_path):
    database_path = tmp_path / "desktop.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    async def run():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("DROP INDEX ix_ai_runs_request_id"))
            await connection.execute(text("ALTER TABLE ai_runs DROP COLUMN request_id"))
            await _ensure_incremental_columns(connection)

    try:
        asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(ai_runs)")}
    assert "request_id" in columns
    assert "ix_ai_runs_request_id" in indexes


def test_alembic_request_id_upgrade_and_downgrade_from_old_ai_runs_table(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE ai_runs (id CHAR(36) PRIMARY KEY, project_id CHAR(36), run_type VARCHAR(50))"
        )

    _alembic(database_path, "stamp", "037_generation_record_edit_metrics")
    _alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(ai_runs)")}
    assert "request_id" in columns
    assert "ix_ai_runs_request_id" in indexes

    _alembic(database_path, "downgrade", "037_generation_record_edit_metrics")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(ai_runs)")}
    assert "request_id" not in columns
    assert "ix_ai_runs_request_id" not in indexes
