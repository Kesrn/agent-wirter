"""PostgreSQL startup must validate Alembic state; SQLite remains auto-created."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.session as db_session
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.asyncio
async def test_auto_create_disabled_rejects_database_behind_head(monkeypatch):
    monkeypatch.setattr(db_session, "settings", SimpleNamespace(AUTO_CREATE_TABLES=False))
    monkeypatch.setattr(db_session, "_get_database_alembic_heads", AsyncMock(return_value=set()))

    with pytest.raises(RuntimeError, match="请先执行 `alembic upgrade head`"):
        await db_session.init_db()


@pytest.mark.asyncio
async def test_auto_create_disabled_accepts_database_at_head(monkeypatch):
    config = Config(str(db_session.Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    monkeypatch.setattr(db_session, "settings", SimpleNamespace(AUTO_CREATE_TABLES=False))
    monkeypatch.setattr(
        db_session,
        "_get_database_alembic_heads",
        AsyncMock(return_value=expected_heads),
    )

    await db_session.init_db()


def test_sqlite_default_keeps_auto_create_enabled():
    from config.settings import settings

    assert settings.DATABASE_URL.startswith("sqlite")
    assert settings.AUTO_CREATE_TABLES is True
