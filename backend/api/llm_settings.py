"""LLM 设置路由。

用户可以在设置页保存自己的模型供应商、base_url、model_id 和 API Key。
运行生成任务时，api/llm_deps.py 会优先读取用户配置；如果用户未配置，
再回退到 config/settings.py 中的全局默认值。

安全策略：
- API Key 使用 Fernet 加密后存库；
- 任何 GET/status 接口只返回 api_key_set/has_api_key 布尔值；
- 不把明文或密文 API Key 返回给前端。
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.session import get_db
from models.llm_config import LLMConfig
from schemas.api import (
    LLMConfigCreate, LLMConfigUpdate, LLMConfigResponse,
    ModelListRequest, ModelInfo,
)
from api.auth import get_current_user
from api.rate_limiter import auth_limiter
from schemas.api import AuthUser
from utils.crypto import encrypt_api_key, decrypt_api_key
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

PROVIDER_BASE_URLS = {
    # 这些厂商都提供 OpenAI 兼容接口，因此后端可以复用同一套 OpenAI SDK 调用逻辑。
    # custom 场景由用户手动填写 base_url，不放在这里。
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "yi": "https://api.lingyiwanwu.com/v1",
    "minimax": "https://api.minimax.chat/v1",
}


def _api_key_is_available(encrypted_key: str | None) -> bool:
    """判断数据库里的加密 API Key 是否可用。

    解密失败时返回 False，而不是把异常抛到前端。常见原因是 JWT_SECRET 变了，
    旧密文无法用新的派生密钥解密。
    """
    if not encrypted_key:
        return False
    try:
        return bool(decrypt_api_key(encrypted_key))
    except Exception:
        logger.warning("API Key 解密失败，将配置标记为未设置")
        return False


def _user_uuid(user: AuthUser) -> uuid.UUID:
    """AuthUser 为接口响应友好的 str，这里转回数据库使用的 UUID。"""
    return uuid.UUID(user.id)


@router.get("/llm-settings")
async def get_llm_settings(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """读取当前用户的 LLM 配置。

    有用户级配置时返回配置摘要；没有时返回环境变量默认值，让前端仍能展示
    “当前将使用哪个 provider/model”。
    """
    auth_limiter.check(f"llm_settings:{user.id}")
    result = await db.execute(
        select(LLMConfig).where(LLMConfig.user_id == _user_uuid(user))
    )
    config = result.scalar_one_or_none()
    if config:
        return {
            "id": str(config.id),
            "provider": config.provider,
            "api_key_set": _api_key_is_available(config.encrypted_api_key),
            "base_url": config.base_url,
            "model_id": config.model_id,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }
    # 无配置时返回 env 默认值
    return {
        "provider": settings.LLM_PROVIDER,
        "api_key_set": bool(settings.LLM_API_KEY),
        "base_url": settings.LLM_BASE_URL or None,
        "model_id": settings.LLM_MODEL,
    }


@router.put("/llm-settings")
async def upsert_llm_settings(
    req: LLMConfigUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """创建或更新当前用户的 LLM 配置。

    api_key 字段有三种语义：
    - None：不修改已有 key；
    - ""：清空已有 key；
    - 非空字符串：加密后覆盖保存。
    """
    auth_limiter.check(f"llm_settings:{user.id}")
    result = await db.execute(
        select(LLMConfig).where(LLMConfig.user_id == _user_uuid(user))
    )
    config = result.scalar_one_or_none()

    if not config:
        # 创建新配置
        provider = req.provider or "mock"
        encrypted_key = None
        if req.api_key is not None and req.api_key != "":
            encrypted_key = encrypt_api_key(req.api_key)
        config = LLMConfig(
            user_id=_user_uuid(user),
            provider=provider,
            encrypted_api_key=encrypted_key,
            base_url=req.base_url,
            model_id=req.model_id,
        )
        db.add(config)
    else:
        # 更新已有配置
        if req.provider is not None:
            config.provider = req.provider
        if req.api_key is not None:
            if req.api_key == "":
                config.encrypted_api_key = None
            else:
                config.encrypted_api_key = encrypt_api_key(req.api_key)
        if req.base_url is not None:
            config.base_url = req.base_url
        if req.model_id is not None:
            config.model_id = req.model_id

    await db.commit()
    await db.refresh(config)
    return {
        "id": str(config.id),
        "provider": config.provider,
        "api_key_set": _api_key_is_available(config.encrypted_api_key),
        "base_url": config.base_url,
        "model_id": config.model_id,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    }


@router.post("/llm-settings/models", response_model=list[ModelInfo])
async def list_models(
    req: ModelListRequest,
    user: AuthUser = Depends(get_current_user),
):
    """实时请求 provider 的 /models 列表，帮助用户检查 base_url/API Key 是否可用。

    这个接口不落库，只用本次请求传入的 provider/base_url/api_key 试探模型列表。
    """
    auth_limiter.check(f"llm_settings:{user.id}")
    base_url = req.base_url or PROVIDER_BASE_URLS.get(req.provider, "")
    if not base_url:
        raise HTTPException(status_code=400, detail="无法确定 API base URL")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=req.api_key, base_url=base_url, timeout=5.0)
        resp = await client.models.list()
        models = []
        for m in resp.data:
            models.append(ModelInfo(id=m.id, owned_by=getattr(m, "owned_by", None)))
        return models
    except Exception as e:
        logger.exception("获取模型列表失败")
        raise HTTPException(status_code=502, detail=f"获取模型列表失败: {str(e)}")


@router.get("/llm-settings/status")
async def get_llm_status(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """返回最小化状态，用于设置页或生成入口判断是否已配置可用模型。"""
    auth_limiter.check(f"llm_settings:{user.id}")
    result = await db.execute(
        select(LLMConfig).where(LLMConfig.user_id == _user_uuid(user))
    )
    config = result.scalar_one_or_none()
    if config:
        return {
            "has_config": True,
            "provider": config.provider,
            "model_id": config.model_id,
            "has_api_key": _api_key_is_available(config.encrypted_api_key),
        }
    return {
        "has_config": False,
        "provider": settings.LLM_PROVIDER,
        "model_id": settings.LLM_MODEL,
        "has_api_key": bool(settings.LLM_API_KEY),
    }
