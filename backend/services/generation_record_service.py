"""AI 生成历史服务"""

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.generation_record import GenerationRecord
from schemas.api import GenerateRequest
from services.content_sanitizer import sanitize_chapter_content

logger = logging.getLogger(__name__)

# 对话式创作（036）：superseded = 被新候选稿替代的只读终态（区别于用户主动丢弃 discarded）。
VALID_GENERATION_STATUSES = frozenset({"candidate", "applied", "discarded", "superseded"})


def _count_non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _request_snapshot(req: GenerateRequest, *, skill_packs: list[dict] | None = None) -> dict[str, Any]:
    snapshot = req.model_dump(
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
    if skill_packs:
        snapshot["skill_packs"] = skill_packs
    return snapshot


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
    skill_packs: list[dict] | None = None,
    langfuse_trace_id: str | None = None,
    run_id: str | uuid.UUID | None = None,
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
        request_params=_request_snapshot(req, skill_packs=skill_packs) if req else ({"skill_packs": skill_packs} if skill_packs else None),
        langfuse_trace_id=langfuse_trace_id,
        run_id=run_id,
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


# ── run-level 候选记录一致性 ──────────────────────────────


async def get_candidate_for_run(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    statuses: tuple[str, ...] = ("candidate",),
) -> GenerationRecord | None:
    """取该 run_id 最新一条 status in statuses 的记录。"""
    run_uuid = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    result = await db.execute(
        select(GenerationRecord)
        .where(
            GenerationRecord.run_id == run_uuid,
            GenerationRecord.status.in_(tuple(statuses)),
        )
        .order_by(GenerationRecord.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_candidate_for_run(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    project_id: str | uuid.UUID,
    mode: str,
    content: str,
    chapter_id: str | uuid.UUID | None = None,
    document_id: str | uuid.UUID | None = None,
    expert_id: str | uuid.UUID | None = None,
    review_results: dict | None = None,
    skill_packs: list[dict] | None = None,
    langfuse_trace_id: str | None = None,
    req: GenerateRequest | None = None,
) -> GenerationRecord | None:
    """每 run_id 最多一条 candidate/applied。

    - 已 applied → 不覆盖（理论不应发生，抛 RuntimeError 防御）
    - candidate 存在 → 覆写 content/word_count/request_params/langfuse_trace_id
    - 否则新建；并发插入命中唯一约束时 rollback 后重查命中已 insert 的行
    """
    clean_content = sanitize_chapter_content(content or "")
    if not clean_content.strip():
        return None

    run_uuid = uuid.UUID(run_id) if isinstance(run_id, str) else run_id

    existing = await get_candidate_for_run(db, run_id=run_uuid, statuses=("candidate", "applied"))
    if isinstance(existing, GenerationRecord):
        if existing.status == "applied":
            raise RuntimeError(f"run {run_uuid} 已有 applied 记录，不允许再 upsert 候选稿")
        # 覆写 candidate
        existing.content = clean_content
        existing.word_count = _count_non_space_chars(clean_content)
        if expert_id is not None:
            existing.expert_id = expert_id
        if review_results is not None:
            existing.review_results = review_results
        if req is not None:
            existing.direction = _record_direction(req)
            existing.user_note = req.user_note
            existing.target_words = req.target_words
            existing.request_params = _request_snapshot(req, skill_packs=skill_packs)
        elif skill_packs is not None:
            request_params = dict(existing.request_params or {})
            request_params["skill_packs"] = skill_packs
            existing.request_params = request_params
        if langfuse_trace_id is not None:
            existing.langfuse_trace_id = langfuse_trace_id
        await db.flush()
        return existing

    # 新建
    record = GenerationRecord(
        project_id=project_id,
        chapter_id=chapter_id,
        document_id=document_id,
        mode=mode,
        expert_id=expert_id or ((req.expert_id if req else None) or None),
        direction=_record_direction(req) if req else None,
        user_note=req.user_note if req else None,
        target_words=req.target_words if req else None,
        content=clean_content,
        word_count=_count_non_space_chars(clean_content),
        status="candidate",
        review_results=review_results,
        request_params=_request_snapshot(req, skill_packs=skill_packs) if req else ({"skill_packs": skill_packs} if skill_packs else None),
        langfuse_trace_id=langfuse_trace_id,
        run_id=run_uuid,
    )
    db.add(record)
    try:
        await db.flush()
        return record
    except IntegrityError:
        # 并发双写命中 run_id 唯一约束 → rollback 后重查对手已 insert 的行
        await db.rollback()
        win = await get_candidate_for_run(db, run_id=run_uuid, statuses=("candidate",))
        if win is not None:
            return win
        raise


async def mark_candidate_discarded_for_run(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    reason: str = "rejected",
) -> int:
    """把该 run_id 所有 candidate 记录标 discarded。

    严格只动 candidate；applied 记录不受影响（不能降级）。
    返回受影响行数。
    """
    run_uuid = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    result = await db.execute(
        select(GenerationRecord).where(
            GenerationRecord.run_id == run_uuid,
            GenerationRecord.status == "candidate",
        )
    )
    records = list(result.scalars().all())
    count = 0
    for rec in records:
        rec.status = "discarded"
        count += 1
    if count:
        await db.flush()
    logger.info("mark_candidate_discarded_for_run run_id=%s discarded=%d reason=%s", run_uuid, count, reason)
    return count


async def mark_applied_for_run(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    accepted_version_id: str | uuid.UUID | None,
    content: str | None = None,
) -> bool:
    """把该 run_id 最新 candidate 标 applied 并关联版本。

    严格要求 candidate 存在；不存在返回 False（兼容无候选记录的 approve 场景）。
    """
    run_uuid = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    rec = await get_candidate_for_run(db, run_id=run_uuid, statuses=("candidate",))
    if rec is None:
        logger.warning("mark_applied_for_run: no candidate for run %s", run_uuid)
        return False
    rec.status = "applied"
    if accepted_version_id is not None:
        rec.accepted_version_id = accepted_version_id
    if content is not None:
        clean = sanitize_chapter_content(content)
        rec.content = clean
        rec.word_count = _count_non_space_chars(clean)
    await db.flush()
    return True
