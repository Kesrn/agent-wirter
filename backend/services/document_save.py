"""文档内容保存服务 — 统一手动 PATCH 和 HITL approve 的正文处理逻辑

与 chapter_save.py 对称，但操作 Document 模型。
确保 sanitize、word_count、create_document_version 的 source 一致。
事务提交由调用方负责。
"""

import re
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models.document import Document
from services.content_sanitizer import sanitize_chapter_content
from services.document_version_service import create_document_version

logger = logging.getLogger(__name__)


def _count_non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


async def save_document_content(
    db: AsyncSession,
    document: Document,
    raw_content: str,
    source: str = "manual",
    set_status: str | None = None,
) -> Document:
    """保存文档正文内容（统一入口）

    Args:
        db: 数据库会话
        document: Document ORM 对象（已加载）
        raw_content: 原始内容（未经清洗）
        source: 版本来源 ("manual" | "ai_approve" | "ai_enhance" | "ai_continue")
        set_status: 若非 None，将 document.status 设为此值

    Returns:
        已更新但尚未提交的 Document 对象。
    """
    clean_content = sanitize_chapter_content(raw_content)
    content_changed = document.content != clean_content

    document.content = clean_content
    document.word_count = _count_non_space_chars(clean_content)

    if set_status is not None:
        document.status = set_status

    if content_changed and clean_content:
        await create_document_version(db, document.id, clean_content, source=source)

    return document
