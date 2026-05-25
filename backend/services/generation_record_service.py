"""AI 生成历史服务"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.generation_record import GenerationRecord
from schemas.api import GenerateRequest
from services.content_sanitizer import sanitize_chapter_content

VALID_GENERATION_STATUSES = frozenset({"candidate", "applied", "discarded"})


def _count_non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _request_snapshot(req: GenerateRequest) -> dict[str, Any]:
    return req.model_dump(
        exclude={
            "chapter_id",
            "document_id",
            "chapter_num",
            "selected_outline_ids",
            "selected_character_ids",
            "selected_world_entry_ids",
            "selected_hidden_thread_ids",
        },
        exclude_none=True,
    )


def _record_direction(req: GenerateRequest) -> str | None:
    return req.enhance_direction or req.turn_direction


async def create_generation_record(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    mode: str,
    content: str,
    req: GenerateRequest | None = None,
    chapter_id: str | uuid.UUID | None = None,
    document_id: str | uuid.UUID | None = None,
    expert_id: str | uuid.UUID | None = None,
    review_results: dict | None = None,
    status: str = "candidate",
) -> GenerationRecord | None:
    clean_content = sanitize_chapter_content(content or "")
    if not clean_content.strip():
        return None
    if status not in VALID_GENERATION_STATUSES:
        raise ValueError(f"invalid generation status '{status}'")

    record = GenerationRecord(
        project_id=project_id,
        chapter_id=chapter_id,
        document_id=document_id,
        mode=mode,
        expert_id=expert_id,
        direction=_record_direction(req) if req else None,
        user_note=req.user_note if req else None,
        target_words=req.target_words if req else None,
        content=clean_content,
        word_count=_count_non_space_chars(clean_content),
        status=status,
        review_results=review_results,
        request_params=_request_snapshot(req) if req else None,
    )
    db.add(record)
    await db.flush()
    return record


async def list_generation_records_for_chapter(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    chapter_id: str | uuid.UUID,
) -> list[GenerationRecord]:
    result = await db.execute(
        select(GenerationRecord)
        .where(GenerationRecord.project_id == project_id, GenerationRecord.chapter_id == chapter_id)
        .order_by(GenerationRecord.created_at.desc())
    )
    return list(result.scalars().all())


async def list_generation_records_for_document(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    document_id: str | uuid.UUID,
) -> list[GenerationRecord]:
    result = await db.execute(
        select(GenerationRecord)
        .where(GenerationRecord.project_id == project_id, GenerationRecord.document_id == document_id)
        .order_by(GenerationRecord.created_at.desc())
    )
    return list(result.scalars().all())


async def get_generation_record(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    record_id: str | uuid.UUID,
) -> GenerationRecord | None:
    result = await db.execute(
        select(GenerationRecord).where(
            GenerationRecord.id == record_id,
            GenerationRecord.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def update_generation_record_status(
    db: AsyncSession,
    record: GenerationRecord,
    status: str,
    accepted_version_id: str | uuid.UUID | None = None,
) -> GenerationRecord:
    if status not in VALID_GENERATION_STATUSES:
        raise ValueError(f"invalid generation status '{status}'")
    record.status = status
    if accepted_version_id is not None:
        record.accepted_version_id = accepted_version_id
    return record
