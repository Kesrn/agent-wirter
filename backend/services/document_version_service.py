"""文档版本服务 — DocumentVersion 的创建和查询

与 version_service.py 对称，但操作 DocumentVersion 模型。
"""

import logging
import re

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.document_version import DocumentVersion

logger = logging.getLogger(__name__)

VALID_SOURCES = frozenset({
    "manual",
    "ai_enhance",
    "ai_continue",
    "ai_generate",
    "ai_pipeline",
    "ai_approve",
    "restore",
})


def _count_non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


async def create_document_version(
    db: AsyncSession,
    document_id: str,
    content: str,
    source: str = "manual",
) -> DocumentVersion:
    """创建文档版本记录"""
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid source '{source}', must be one of {sorted(VALID_SOURCES)}")

    result = await db.execute(
        select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.document_id == document_id
        )
    )
    max_ver = result.scalar() or 0
    version = DocumentVersion(
        document_id=document_id,
        version_number=max_ver + 1,
        content=content,
        word_count=_count_non_space_chars(content),
        source=source,
    )
    db.add(version)
    return version


async def get_document_versions(
    db: AsyncSession,
    document_id: str,
) -> list[DocumentVersion]:
    """获取文档的所有版本"""
    result = await db.execute(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return result.scalars().all()


async def get_document_version(
    db: AsyncSession,
    version_id: str,
) -> DocumentVersion | None:
    """获取单个文档版本"""
    result = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == version_id)
    )
    return result.scalar_one_or_none()
