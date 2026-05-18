"""LLM 配置依赖：从 DB 读取用户配置并解密 API Key"""

import uuid
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.llm_config import LLMConfig
from utils.crypto import decrypt_api_key

logger = logging.getLogger(__name__)


async def get_user_llm_config(user_id: str, db: AsyncSession) -> dict | None:
    """获取用户 LLM 配置，解密 API Key 后返回。

    Returns:
        {"provider": ..., "api_key": ..., "base_url": ..., "model": ...} 或 None
    """
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        return None

    result = await db.execute(
        select(LLMConfig).where(LLMConfig.user_id == uid)
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
            api_key = ""

    return {
        "provider": config.provider,
        "api_key": api_key,
        "base_url": config.base_url,
        "model": config.model_id,
    }