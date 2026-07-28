"""多供应商 LLM 配置档案回归测试。"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api.llm_deps import get_user_llm_config
from api.llm_settings import (
    activate_llm_config,
    create_llm_config,
    get_llm_settings,
    list_llm_configs,
)
from models.llm_config import LLMConfig
from schemas.api import AuthUser, LLMConfigCreate


@pytest.mark.asyncio
async def test_user_can_save_multiple_profiles_and_switch_active_provider():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(LLMConfig.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    user = AuthUser(id=str(uuid.uuid4()), username="profiles", display_name="profiles")
    try:
        async with factory() as db:
            first = await create_llm_config(
                LLMConfigCreate(
                    name="OpenAI 主配置",
                    provider="openai",
                    api_key="sk-openai-1111",
                    base_url="https://openai.example/v1",
                    model_id="gpt-test",
                    is_active=True,
                ),
                db=db,
                user=user,
            )
            second = await create_llm_config(
                LLMConfigCreate(
                    name="DeepSeek 备用",
                    provider="deepseek",
                    api_key="sk-deepseek-2222",
                    base_url="https://deepseek.example/v1",
                    model_id="deepseek-test",
                    is_active=False,
                ),
                db=db,
                user=user,
            )

            profiles = await list_llm_configs(db=db, user=user)
            assert len(profiles) == 2
            assert sum(profile["is_active"] for profile in profiles) == 1
            assert profiles[0]["api_key_masked"].endswith("1111")
            assert "sk-openai-1111" not in str(profiles)

            activated = await activate_llm_config(
                uuid.UUID(second["id"]),
                db=db,
                user=user,
            )
            assert activated["name"] == "DeepSeek 备用"
            assert activated["is_active"] is True

            resolved = await get_user_llm_config(user.id, db)
            assert resolved is not None
            assert resolved["config_id"] == second["id"]
            assert resolved["provider"] == "deepseek"
            assert resolved["model"] == "deepseek-test"
            assert resolved["api_key"] == "sk-deepseek-2222"
            assert resolved["config_id"] != first["id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inactive_profile_does_not_override_environment_or_runtime():
    """仅保存但未启用的档案不能被误当成当前生成配置。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(LLMConfig.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    user = AuthUser(id=str(uuid.uuid4()), username="inactive", display_name="inactive")
    try:
        async with factory() as db:
            await create_llm_config(
                LLMConfigCreate(
                    name="暂不启用",
                    provider="deepseek",
                    api_key="sk-inactive-3333",
                    model_id="deepseek-test",
                    is_active=False,
                ),
                db=db,
                user=user,
            )

            current = await get_llm_settings(db=db, user=user)
            assert current["source"] == "environment"
            assert await get_user_llm_config(user.id, db) is None
    finally:
        await engine.dispose()
