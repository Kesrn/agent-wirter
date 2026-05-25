"""章节内容保存服务 — 统一手动 PATCH 和 HITL approve 的正文处理逻辑

所有章节正文保存都经过此服务，确保：
- sanitize_chapter_content 清洗一致
- word_count 使用 _count_non_space_chars（中文字数）
- create_version 的 source 参数一致

事务提交由调用方负责，避免 PATCH 在保存正文后继续修改 title/status 时丢失提交。
"""

import re
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models.chapter import Chapter
from services.content_sanitizer import sanitize_chapter_content
from services.version_service import create_version

logger = logging.getLogger(__name__)


def _count_non_space_chars(text: str) -> int:
    """中文字数：忽略空白字符，其余字符计 1"""
    return len(re.sub(r"\s+", "", text or ""))


async def save_chapter_content(
    db: AsyncSession,
    chapter: Chapter,
    raw_content: str,
    source: str = "manual",
    set_status: str | None = None,
) -> Chapter:
    """保存章节正文内容（统一入口）

    Args:
        db: 数据库会话
        chapter: Chapter ORM 对象（已加载）
        raw_content: 原始内容（未经清洗）
        source: 版本来源 ("manual" | "ai_approve" | "ai_enhance" | "ai_continue")
        set_status: 若非 None，将 chapter.status 设为此值

    Returns:
        已更新但尚未提交的 Chapter 对象。
    """
    clean_content = sanitize_chapter_content(raw_content)
    content_changed = chapter.content != clean_content

    chapter.content = clean_content
    chapter.word_count = _count_non_space_chars(clean_content)

    if set_status is not None:
        chapter.status = set_status

    if content_changed and clean_content:
        await create_version(db, chapter.id, clean_content, source=source)

    return chapter
