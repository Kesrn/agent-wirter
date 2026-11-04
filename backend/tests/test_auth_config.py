"""任务 0.9：公开接口 GET /api/auth/config（注册是否开放）。

登录页要根据这个接口决定是否显示“注册新账号”入口。验证点：
- 开发默认配置下 registration_enabled=true；
- ALLOW_REGISTRATION=False 时为 false，且与 POST /api/auth/register 的 403 行为一致；
- 无需登录、响应体只有一个键、带 Cache-Control: no-store。

全部走 TestClient + 内存 SQLite，不发真实请求。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api.auth import router as auth_router
from config.settings import settings
from db.session import get_db
from models.base import Base
from models.user import User


@contextmanager
def settings_override(**changes):
    """临时改 frozen dataclass 的字段（沿用现有测试的做法：object.__setattr__）。"""
    original = {key: getattr(settings, key) for key in changes}
    for key, value in changes.items():
        object.__setattr__(settings, key, value)
    try:
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(settings, key, value)


@pytest.fixture()
def client():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[User.__table__])

    asyncio.run(_create_tables())

    app = FastAPI()
    app.include_router(auth_router)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, client=("203.0.113.120", 5000)) as test_client:
        yield test_client


def test_config_registration_enabled_by_default(client):
    """开发默认配置（ALLOW_REGISTRATION=True）时返回 true。"""
    response = client.get("/api/auth/config")

    assert response.status_code == 200
    assert response.json() == {"registration_enabled": True}


def test_config_registration_disabled(client):
    """ALLOW_REGISTRATION=False 时返回 false。"""
    with settings_override(ALLOW_REGISTRATION=False):
        response = client.get("/api/auth/config")

    assert response.status_code == 200
    assert response.json() == {"registration_enabled": False}


def test_config_needs_no_authorization(client):
    """不带任何凭证即可访问（登录页在登录前就要用它）。"""
    response = client.get("/api/auth/config", headers={"Authorization": ""})

    assert response.status_code == 200


def test_config_response_has_exactly_one_key_and_no_store(client):
    """响应体键集合恰好是 {registration_enabled}，且带 no-store 缓存头。"""
    response = client.get("/api/auth/config")

    assert set(response.json().keys()) == {"registration_enabled"}
    assert response.headers["cache-control"] == "no-store"


def test_config_matches_register_behavior(client):
    """接口说的“关闭”必须和 register 的实际行为一致：403 注册已关闭。"""
    with settings_override(ALLOW_REGISTRATION=False):
        config = client.get("/api/auth/config")
        register = client.post(
            "/api/auth/register",
            json={"username": "shouldfail", "password": "secret123"},
        )

    assert config.json() == {"registration_enabled": False}
    assert register.status_code == 403
    assert register.json() == {"detail": "注册已关闭"}
