"""LLM 多供应商配置路由。

每个用户可以保存多个配置档案，并手动指定其中一条为当前启用配置。API Key
始终以 Fernet 密文保存；返回给前端的只有是否已设置和安全掩码。
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.rate_limiter import auth_limiter
from config.settings import settings
from db.session import get_db
from models.llm_config import LLMConfig
from schemas.api import (
    AuthUser,
    LLMConfigCreate,
    LLMConfigUpdate,
    ModelInfo,
    ModelListRequest,
)
from utils.crypto import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "yi": "https://api.lingyiwanwu.com/v1",
    "minimax": "https://api.minimax.chat/v1",
}


def _read_api_key(encrypted_key: str | None) -> str | None:
    if not encrypted_key:
        return None
    try:
        return decrypt_api_key(encrypted_key) or None
    except Exception:
        logger.warning("API Key 解密失败，将配置标记为未设置")
        return None


def _api_key_is_available(encrypted_key: str | None) -> bool:
    return bool(_read_api_key(encrypted_key))


def _mask_plain_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    prefix = "sk-" if api_key.startswith("sk-") else ""
    suffix = api_key[-4:] if len(api_key) >= 4 else ""
    return f"{prefix}{'•' * 12}{suffix}"


def _mask_api_key(encrypted_key: str | None) -> str | None:
    return _mask_plain_api_key(_read_api_key(encrypted_key))


def _user_uuid(user: AuthUser) -> uuid.UUID:
    return uuid.UUID(user.id)


def _serialize_config(config: LLMConfig) -> dict:
    return {
        "id": str(config.id),
        "name": config.name,
        "provider": config.provider,
        "api_key_set": _api_key_is_available(config.encrypted_api_key),
        "api_key_masked": _mask_api_key(config.encrypted_api_key),
        "base_url": config.base_url,
        "model_id": config.model_id,
        "is_active": bool(config.is_active),
        "source": "user",
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def _environment_config() -> dict:
    return {
        "id": None,
        "name": "环境变量默认配置",
        "provider": settings.LLM_PROVIDER,
        "api_key_set": bool(settings.LLM_API_KEY),
        "api_key_masked": _mask_plain_api_key(settings.LLM_API_KEY),
        "base_url": settings.LLM_BASE_URL or None,
        "model_id": settings.LLM_MODEL,
        "is_active": True,
        "source": "environment",
        "created_at": None,
        "updated_at": None,
    }


async def _active_config(db: AsyncSession, user_id: uuid.UUID) -> LLMConfig | None:
    result = await db.execute(
        select(LLMConfig)
        .where(LLMConfig.user_id == user_id, LLMConfig.is_active.is_(True))
        .order_by(LLMConfig.updated_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _owned_config(
    db: AsyncSession,
    user_id: uuid.UUID,
    config_id: uuid.UUID,
) -> LLMConfig:
    result = await db.execute(
        select(LLMConfig).where(LLMConfig.id == config_id, LLMConfig.user_id == user_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return config


async def _deactivate_all(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(LLMConfig)
        .where(LLMConfig.user_id == user_id, LLMConfig.is_active.is_(True))
        .values(is_active=False)
    )
    await db.flush()


def _apply_update(config: LLMConfig, req: LLMConfigUpdate) -> None:
    if req.name is not None:
        config.name = req.name.strip()
    if req.provider is not None:
        config.provider = req.provider
    if req.api_key is not None:
        config.encrypted_api_key = encrypt_api_key(req.api_key) if req.api_key else None
    if "base_url" in req.model_fields_set:
        config.base_url = req.base_url
    if "model_id" in req.model_fields_set:
        config.model_id = req.model_id


@router.get("/llm-settings")
async def get_llm_settings(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """返回当前启用配置；没有用户配置时返回环境变量摘要。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    config = await _active_config(db, _user_uuid(user))
    return _serialize_config(config) if config else _environment_config()


@router.get("/llm-settings/configs")
async def list_llm_configs(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出当前用户保存的全部供应商配置。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    result = await db.execute(
        select(LLMConfig)
        .where(LLMConfig.user_id == _user_uuid(user))
        .order_by(LLMConfig.is_active.desc(), LLMConfig.updated_at.desc())
    )
    return [_serialize_config(config) for config in result.scalars().all()]


@router.post("/llm-settings/configs", status_code=201)
async def create_llm_config(
    req: LLMConfigCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """新增配置档案；默认保存后立即切换为当前配置。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    uid = _user_uuid(user)
    if req.is_active:
        await _deactivate_all(db, uid)
    config = LLMConfig(
        user_id=uid,
        name=req.name.strip(),
        provider=req.provider,
        encrypted_api_key=encrypt_api_key(req.api_key) if req.api_key else None,
        base_url=req.base_url,
        model_id=req.model_id,
        is_active=req.is_active,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return _serialize_config(config)


@router.put("/llm-settings/configs/{config_id}")
async def update_llm_config(
    config_id: uuid.UUID,
    req: LLMConfigUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    auth_limiter.check(f"llm_settings:{user.id}")
    uid = _user_uuid(user)
    config = await _owned_config(db, uid, config_id)
    if req.is_active is True and not config.is_active:
        await _deactivate_all(db, uid)
        config.is_active = True
    _apply_update(config, req)
    await db.commit()
    await db.refresh(config)
    return _serialize_config(config)


@router.post("/llm-settings/configs/{config_id}/activate")
async def activate_llm_config(
    config_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    auth_limiter.check(f"llm_settings:{user.id}")
    uid = _user_uuid(user)
    config = await _owned_config(db, uid, config_id)
    await _deactivate_all(db, uid)
    config.is_active = True
    await db.commit()
    await db.refresh(config)
    return _serialize_config(config)


@router.delete("/llm-settings/configs/{config_id}", status_code=204)
async def delete_llm_config(
    config_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    auth_limiter.check(f"llm_settings:{user.id}")
    uid = _user_uuid(user)
    config = await _owned_config(db, uid, config_id)
    was_active = bool(config.is_active)
    await db.delete(config)
    await db.flush()
    if was_active:
        result = await db.execute(
            select(LLMConfig)
            .where(LLMConfig.user_id == uid)
            .order_by(LLMConfig.updated_at.desc())
            .limit(1)
        )
        replacement = result.scalar_one_or_none()
        if replacement:
            replacement.is_active = True
    await db.commit()


@router.put("/llm-settings")
async def upsert_llm_settings(
    req: LLMConfigUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """兼容旧客户端：更新当前启用配置；不存在时创建一条。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    uid = _user_uuid(user)
    config = await _active_config(db, uid)
    if config is None:
        provider = req.provider or settings.LLM_PROVIDER
        config = LLMConfig(
            user_id=uid,
            name=(req.name or f"{provider} 配置").strip(),
            provider=provider,
            encrypted_api_key=encrypt_api_key(req.api_key) if req.api_key else None,
            base_url=req.base_url,
            model_id=req.model_id,
            is_active=True,
        )
        db.add(config)
    else:
        _apply_update(config, req)
    await db.commit()
    await db.refresh(config)
    return _serialize_config(config)


@router.post("/llm-settings/models", response_model=list[ModelInfo])
async def list_models(
    req: ModelListRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """请求供应商模型列表；编辑已有档案时可复用其已保存密钥。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    saved_config: LLMConfig | None = None
    if req.config_id:
        saved_config = await _owned_config(db, _user_uuid(user), req.config_id)

    provider = req.provider or (saved_config.provider if saved_config else None)
    if not provider or provider == "mock":
        raise HTTPException(status_code=400, detail="请选择真实模型供应商")
    api_key = req.api_key or (_read_api_key(saved_config.encrypted_api_key) if saved_config else None)
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key 或选择已保存密钥的配置")
    base_url = req.base_url or (saved_config.base_url if saved_config else None) or PROVIDER_BASE_URLS.get(provider, "")
    if not base_url:
        raise HTTPException(status_code=400, detail="无法确定 API base URL")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=5.0)
        resp = await client.models.list()
        return [ModelInfo(id=model.id, owned_by=getattr(model, "owned_by", None)) for model in resp.data]
    except Exception as exc:
        logger.exception("获取模型列表失败")
        raise HTTPException(status_code=502, detail=f"获取模型列表失败: {str(exc)}")


@router.get("/llm-settings/status")
async def get_llm_status(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    auth_limiter.check(f"llm_settings:{user.id}")
    config = await _active_config(db, _user_uuid(user))
    if config:
        return {
            "has_config": True,
            "config_id": str(config.id),
            "config_name": config.name,
            "provider": config.provider,
            "model_id": config.model_id,
            "has_api_key": _api_key_is_available(config.encrypted_api_key),
        }
    return {
        "has_config": False,
        "config_id": None,
        "config_name": "环境变量默认配置",
        "provider": settings.LLM_PROVIDER,
        "model_id": settings.LLM_MODEL,
        "has_api_key": bool(settings.LLM_API_KEY),
    }
