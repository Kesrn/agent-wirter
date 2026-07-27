"""章节内容保存服务 — 统一手动 PATCH 和 HITL approve 的正文处理逻辑

所有章节正文保存都经过此服务，确保：
- sanitize_chapter_content 清洗一致
- word_count 使用 _count_non_space_chars（中文字数）
- create_version 的 source 参数一致

事务提交由调用方负责，避免 PATCH 在保存正文后继续修改 title/status 时丢失提交。
"""

import re
import uuid
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models.chapter import Chapter
from models.chapter_version import ChapterVersion
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
    *,
    run_id: str | uuid.UUID | None = None,
    parent_version_id: str | uuid.UUID | None = None,
    rollback_from_version_id: str | uuid.UUID | None = None,
) -> Chapter:
    """保存章节正文内容（统一入口）

    Args:
        db: 数据库会话
        chapter: Chapter ORM 对象（已加载）
        raw_content: 原始内容（未经清洗）
        source: 版本来源 ("manual" | "ai_approve" | "ai_enhance" | "ai_continue")
        set_status: 若非 None，将 chapter.status 设为此值
        run_id: 关联的 AI Run ID（可选，写入 ChapterVersion.run_id）
        parent_version_id: 父版本 ID（可选，写入 ChapterVersion.parent_version_id）
        rollback_from_version_id: 回滚来源版本 ID（可选，写入 ChapterVersion.rollback_from_version_id）

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
        await create_version(
            db, chapter.id, clean_content, source=source,
            run_id=run_id, parent_version_id=parent_version_id,
            rollback_from_version_id=rollback_from_version_id,
        )

    return chapter


async def finalize_chapter_content(
    db: AsyncSession,
    chapter: Chapter,
    raw_content: str | None = None,
) -> Chapter:
    """将章节定稿为不可变版本，并把该快照作为后续章节的可信上文。

    相同内容重复定稿是幂等的；若内容变化，则创建新的 ``finalize`` 版本并更新
    ``final_version_id``。事务仍由路由层统一提交，保证正文、状态与版本一起落库。
    """
    clean_content = sanitize_chapter_content(raw_content if raw_content is not None else (chapter.content or ""))
    if not clean_content.strip():
        raise ValueError("章节正文为空，无法定稿")

    existing_final: ChapterVersion | None = None
    if chapter.final_version_id:
        existing_final = await db.get(ChapterVersion, chapter.final_version_id)

    chapter.content = clean_content
    chapter.word_count = _count_non_space_chars(clean_content)
    chapter.status = "final"

    # 防止重复点击“定稿”时不断新增相同的版本。
    if existing_final and existing_final.content == clean_content:
        return chapter

    version = await create_version(
        db,
        chapter.id,
        clean_content,
        source="finalize",
        project_id=chapter.project_id,
    )
    if version is None:  # clean_content 已校验非空，这里仅保留防御性分支。
        raise RuntimeError("定稿版本创建失败")
    chapter.final_version_id = version.id
    return chapter
