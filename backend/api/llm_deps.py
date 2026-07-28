"""LLM 配置依赖：读取当前启用的用户配置并解密 API Key。"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_config import LLMConfig
from utils.crypto import decrypt_api_key

logger = logging.getLogger(__name__)


async def get_user_llm_config(user_id: str, db: AsyncSession) -> dict | None:
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        return None

    result = await db.execute(
        select(LLMConfig)
        .where(LLMConfig.user_id == uid, LLMConfig.is_active.is_(True))
        .order_by(LLMConfig.updated_at.desc())
        .limit(1)
    )
    config = result.scalar_one_or_none()
    if not config:
        return None

    api_key = ""
    if config.encrypted_api_key:
        try:
            api_key = decrypt_api_key(config.encrypted_api_key)
        except Exception:
            logger.exception("解密 API Key 失败")

    return {
        "config_id": str(config.id),
        "config_name": config.name,
        "provider": config.provider,
        "api_key": api_key,
        "base_url": config.base_url,
        "model": config.model_id,
    }
