"""章节版本管理服务"""

import uuid

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.chapter_version import ChapterVersion

MAX_VERSIONS_PER_CHAPTER = 10

VALID_SOURCES = frozenset({
    "manual",
    "ai_enhance",
    "ai_continue",
    "ai_generate",
    "ai_pipeline",
    "ai_approve",
})


async def create_version(
    db: AsyncSession, chapter_id: str | uuid.UUID, content: str | None, source: str = "manual"
) -> ChapterVersion | None:
    if content is None:
        return None
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid source '{source}', must be one of {sorted(VALID_SOURCES)}")
    if isinstance(chapter_id, str):
        chapter_id = uuid.UUID(chapter_id)

    result = await db.execute(
        select(func.max(ChapterVersion.version_number)).where(
            ChapterVersion.chapter_id == chapter_id
        )
    )
    max_ver = result.scalar() or 0

    version = ChapterVersion(
        chapter_id=chapter_id,
        content=content,
        word_count=len(content),
        version_number=max_ver + 1,
        source=source,
    )
    db.add(version)
    await db.flush()

    await _prune_old_versions(db, chapter_id)
    return version


async def _prune_old_versions(db: AsyncSession, chapter_id: str) -> None:
    result = await db.execute(
        select(ChapterVersion.version_number)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
        .offset(MAX_VERSIONS_PER_CHAPTER)
    )
    old_version_numbers = [row[0] for row in result.all()]
    if old_version_numbers:
        await db.execute(
            delete(ChapterVersion).where(
                ChapterVersion.chapter_id == chapter_id,
                ChapterVersion.version_number.in_(old_version_numbers),
            )
        )
