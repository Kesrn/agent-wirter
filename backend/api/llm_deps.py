"""LLM 配置依赖：读取当前启用的用户配置并解密 API Key。"""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from models.llm_config import LLMConfig
from utils.url_guard import UnsafeURLError, validate_url

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

    # 运行时兜底：保存时的校验挡不住“升级前就已存进库里的内网地址”，所以每次取用
    # 用户配置都再校验一次。环境变量里的默认配置（settings.LLM_BASE_URL）不走这里，
    # 那是管理员可信配置。
    base_url = (config.base_url or "").strip()
    if base_url:
        try:
            await validate_url(
                base_url,
                allow_private=settings.ALLOW_PRIVATE_LLM_ENDPOINTS,
            )
        except UnsafeURLError as exc:
            logger.warning("用户保存的模型地址不被允许 config_id=%s", config.id)
            raise HTTPException(
                status_code=400,
                detail="模型服务地址不被允许，请到模型设置中修改",
            ) from exc

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
