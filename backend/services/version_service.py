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
    "ai_draft",
    "finalize",
    "rollback",
    "import",
})


async def create_version(
    db: AsyncSession,
    chapter_id: str | uuid.UUID,
    content: str | None,
    source: str = "manual",
    *,
    run_id: str | uuid.UUID | None = None,
    parent_version_id: str | uuid.UUID | None = None,
    rollback_from_version_id: str | uuid.UUID | None = None,
    project_id: str | uuid.UUID | None = None,
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
    if run_id is not None:
        version.run_id = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    if parent_version_id is not None:
        version.parent_version_id = uuid.UUID(parent_version_id) if isinstance(parent_version_id, str) else parent_version_id
    if rollback_from_version_id is not None:
        version.rollback_from_version_id = uuid.UUID(rollback_from_version_id) if isinstance(rollback_from_version_id, str) else rollback_from_version_id
    if project_id is not None:
        version.project_id = uuid.UUID(project_id) if isinstance(project_id, str) else project_id
    db.add(version)
    await db.flush()

    await _prune_old_versions(db, chapter_id)
    return version


async def _prune_old_versions(db: AsyncSession, chapter_id: str) -> None:
    """删除超过 MAX_VERSIONS_PER_CHAPTER 的旧版本，但跳过被 generation_record 引用的版本。"""
    # 查询被 generation_record.accepted_version_id 引用的版本 ID
    from models.generation_record import GenerationRecord
    referenced_result = await db.execute(
        select(GenerationRecord.accepted_version_id).where(
            GenerationRecord.accepted_version_id.isnot(None)
        )
    )
    referenced_ids = {row[0] for row in referenced_result.all()}

    result = await db.execute(
        select(ChapterVersion.id, ChapterVersion.version_number)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
        .offset(MAX_VERSIONS_PER_CHAPTER)
    )
    old_rows = result.all()
    # 跳过被审计引用的版本
    deletable_ids = [row[0] for row in old_rows if row[0] not in referenced_ids]
    if deletable_ids:
        await db.execute(
            delete(ChapterVersion).where(
                ChapterVersion.id.in_(deletable_ids),
            )
        )
