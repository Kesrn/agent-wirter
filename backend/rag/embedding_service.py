"""Embedding 服务 — 供 CRUD 端点后台调用，自动生成 embedding

使用 ContextLoader 中已有的 EmbeddingService 基础设施，
封装为适用于 BackgroundTasks 的同步友好接口。
"""

import logging
import uuid

from config.settings import settings

logger = logging.getLogger(__name__)


async def generate_embedding(text: str) -> list[float] | None:
    """生成文本 embedding 向量

    - 截断过长文本（超过 8000 字符截断到前 8000 字符）
    - 如果 embedding provider 不支持（如 mock），返回确定性伪向量而非报错
    - 使用 settings.EMBEDDING_DIMENSIONS 作为维度

    Args:
        text: 待生成 embedding 的文本

    Returns:
        embedding 向量列表，失败时返回 None
    """
    if not text or not text.strip():
        return None

    # 截断过长文本
    if len(text) > 8000:
        text = text[:8000]

    try:
        from rag.context_loader import EmbeddingService
        service = EmbeddingService()
        vec = await service.embed_text(text)
        return vec
    except Exception as e:
        logger.warning(f"Embedding 生成失败，跳过: {e}")
        return None


async def _update_embedding_bg(model_class, entry_id: uuid.UUID, text: str) -> None:
    """后台任务：生成 embedding 并写入数据库

    失败时静默跳过，不影响主流程。
    """
    vec = await generate_embedding(text)
    if vec is None:
        logger.info(f"Embedding 为空，跳过更新: {model_class.__name__} id={entry_id}")
        return

    try:
        from db.session import async_session
        from sqlalchemy import update

        async with async_session() as session:
            await session.execute(
                update(model_class)
                .where(model_class.id == entry_id)
                .values(embedding=vec)
            )
            await session.commit()
            logger.info(f"Embedding 更新完成: {model_class.__name__} id={entry_id}")
    except Exception as e:
        logger.warning(f"Embedding 后台写入失败: {e}")
