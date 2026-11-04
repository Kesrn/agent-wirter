"""LLM 配置依赖：读取当前启用的用户配置并解密 API Key。"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_config import LLMConfig
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

    return {
        "config_id": str(config.id),
        "config_name": config.name,
        "provider": config.provider,
        # 只携带密文，避免明文进入 LangGraph checkpoint 和任务快照。
        # Provider 真正发起请求前才在进程内存中解密。
        "encrypted_api_key": config.encrypted_api_key or "",
        "base_url": config.base_url,
        "model": config.model_id,
    }
