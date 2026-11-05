"""Read-time human edit metrics derived from immutable chapter versions.

The definitions live in ``backend/docs/edit_metrics.md``. ``updated_at`` doubles as
the applied timestamp, so metric refreshes explicitly preserve it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from models.chapter_version import ChapterVersion
from models.generation_record import GenerationRecord
from services.chapter_quality import calculate_edit_change_metrics, count_non_space_chars, normalize_for_comparison

logger = logging.getLogger(__name__)


def _paragraphs(text: str) -> list[str]:
    # Keep paragraph boundaries as alignment hints, but strip all whitespace from
    # each paragraph because whitespace is not part of the published metric.
    return [normalize_for_comparison(part) for part in (text or "").splitlines() if normalize_for_comparison(part)]


def compute_edit_metrics(
    original: str,
    final: str,
    *,
    anchor_version_id: str,
    human_version_id: str | None,
) -> dict:
    """Compute a deterministic ``difflib-v1`` metric without database access."""
    original = original or ""
    final = final or ""
    normalized_original = normalize_for_comparison(original)
    normalized_final = normalize_for_comparison(final)
    original_chars = count_non_space_chars(original)
    final_chars = count_non_space_chars(final)

    if normalized_original == normalized_final:
        changed_chars = changed_original_chars = inserted_chars = 0
        similarity = 1.0
    else:
        source_paragraphs = _paragraphs(original)
        final_paragraphs = _paragraphs(final)
        paragraph_matcher = SequenceMatcher(None, source_paragraphs, final_paragraphs, autojunk=False)
        changed_chars = changed_original_chars = inserted_chars = 0
        matching_chars = 0.0
        for tag, i1, i2, j1, j2 in paragraph_matcher.get_opcodes():
            old_block = "".join(source_paragraphs[i1:i2])
            new_block = "".join(final_paragraphs[j1:j2])
            if tag == "equal":
                matching_chars += len(old_block)
                continue
            block_similarity, block_changed, block_original, block_inserted = calculate_edit_change_metrics(
                old_block, new_block,
            )
            changed_chars += block_changed
            changed_original_chars += block_original
            inserted_chars += block_inserted
            matching_chars += block_similarity * (len(old_block) + len(new_block)) / 2
        denominator = len(normalized_original) + len(normalized_final)
        similarity = (2 * matching_chars / denominator) if denominator else 1.0

    edit_ratio = min(1.0, changed_chars / max(original_chars, 1))
    return {
        "method": "difflib-v1",
        "edited": changed_chars > 0,
        "anchor_version_id": str(anchor_version_id),
        "human_version_id": str(human_version_id) if human_version_id is not None else None,
        "original_chars": original_chars,
        "final_chars": final_chars,
        "changed_chars": changed_chars,
        "changed_original_chars": changed_original_chars,
        "inserted_chars": inserted_chars,
        "similarity": round(max(0.0, min(1.0, similarity)), 4),
        "edit_ratio": round(edit_ratio, 4),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


@dataclass(frozen=True)
class _VersionSummary:
    id: str
    chapter_id: str
    version_number: int
    source: str
    run_id: str | None
    rollback_from_version_id: str | None
    is_applied_anchor: bool


async def _load_version_summaries(
    db: AsyncSession,
    chapter_ids: Iterable[str],
) -> dict[str, list[_VersionSummary]]:
    ids = list(dict.fromkeys(str(value) for value in chapter_ids if value is not None))
    if not ids:
        return {}
    is_applied_anchor = exists().where(
        GenerationRecord.status == "applied",
        GenerationRecord.accepted_version_id == ChapterVersion.id,
    )
    result = await db.execute(
        select(
            ChapterVersion.id,
            ChapterVersion.chapter_id,
            ChapterVersion.version_number,
            ChapterVersion.source,
            ChapterVersion.run_id,
            ChapterVersion.rollback_from_version_id,
            is_applied_anchor.label("is_applied_anchor"),
        )
        .where(ChapterVersion.chapter_id.in_(ids))
        .order_by(ChapterVersion.chapter_id, ChapterVersion.version_number)
    )
    grouped: dict[str, list[_VersionSummary]] = {}
    for row in result:
        summary = _VersionSummary(
            id=str(row.id),
            chapter_id=str(row.chapter_id),
            version_number=row.version_number,
            source=row.source,
            run_id=str(row.run_id) if row.run_id is not None else None,
            rollback_from_version_id=(
                str(row.rollback_from_version_id) if row.rollback_from_version_id is not None else None
            ),
            is_applied_anchor=bool(row.is_applied_anchor),
        )
        grouped.setdefault(summary.chapter_id, []).append(summary)
    return grouped


def _resolve_summary_pair(
    record: GenerationRecord,
    summaries: list[_VersionSummary],
) -> tuple[_VersionSummary | None, _VersionSummary | None]:
    if record.status != "applied" or record.accepted_version_id is None or record.chapter_id is None:
        return None, None
    anchor_id = str(record.accepted_version_id)
    anchor = next((item for item in summaries if item.id == anchor_id), None)
    if anchor is None:
        return None, None

    human: _VersionSummary | None = None
    for item in summaries:
        if item.version_number <= anchor.version_number:
            continue
        if (
            item.source not in {"manual", "finalize"}
            or item.run_id is not None
            or item.rollback_from_version_id is not None
            or item.is_applied_anchor
        ):
            break
        human = item
    return anchor, human


async def resolve_human_version(
    db: AsyncSession,
    record: GenerationRecord,
) -> tuple[ChapterVersion | None, ChapterVersion | None]:
    """Resolve an applied record's immutable anchor and latest manual version."""
    if record.chapter_id is None or record.accepted_version_id is None or record.status != "applied":
        return None, None
    grouped = await _load_version_summaries(db, [record.chapter_id])
    anchor_summary, human_summary = _resolve_summary_pair(record, grouped.get(str(record.chapter_id), []))
    if anchor_summary is None:
        return None, None
    version_ids = [anchor_summary.id]
    if human_summary is not None:
        version_ids.append(human_summary.id)
    result = await db.execute(select(ChapterVersion).where(ChapterVersion.id.in_(version_ids)))
    by_id = {str(version.id): version for version in result.scalars().all()}
    return by_id.get(anchor_summary.id), by_id.get(human_summary.id) if human_summary else None


async def ensure_fresh_metrics(
    db: AsyncSession,
    records: list[GenerationRecord],
    *,
    max_recompute: int = 20,
    force: bool = False,
) -> None:
    """Refresh stale caches using one batched version-summary query.

    Failures are best-effort: log metadata only and preserve the existing cache.
    Callers own the transaction and commit when appropriate.
    """
    applicable = [record for record in records if record.status == "applied"]
    for record in applicable:
        if record.accepted_version_id is None and record.human_edit_metrics is not None:
            applied_at = record.updated_at
            record.human_edit_metrics = None
            if applied_at is not None:
                record.updated_at = applied_at

    with_anchor = [
        record for record in applicable
        if record.chapter_id is not None and record.accepted_version_id is not None
    ]
    if not with_anchor:
        return

    try:
        grouped = await _load_version_summaries(db, [record.chapter_id for record in with_anchor])
    except Exception:
        logger.warning("无法读取人工改动指标版本摘要；保留现有缓存", exc_info=True)
        return

    pending: list[tuple[GenerationRecord, _VersionSummary, _VersionSummary | None]] = []
    max_recompute = max(0, int(max_recompute))
    for record in with_anchor:
        summaries = grouped.get(str(record.chapter_id), [])
        anchor, human = _resolve_summary_pair(record, summaries)
        if anchor is None:
            logger.warning(
                "无法解析人工改动指标锚点：record_id=%s chapter_id=%s",
                record.id, record.chapter_id,
            )
            continue
        human_id = human.id if human is not None else None
        cache = record.human_edit_metrics
        current = (
            isinstance(cache, dict)
            and cache.get("anchor_version_id") == anchor.id
            and cache.get("human_version_id") == human_id
        )
        if current and not force:
            continue
        if len(pending) >= max_recompute:
            continue
        pending.append((record, anchor, human))

    if not pending:
        return
    version_ids = list(dict.fromkeys(
        summary.id
        for _, anchor, human in pending
        for summary in (anchor, human)
        if summary is not None
    ))
    try:
        result = await db.execute(
            select(ChapterVersion.id, ChapterVersion.content).where(ChapterVersion.id.in_(version_ids))
        )
        content_by_id = {str(row.id): row.content or "" for row in result}
    except Exception:
        logger.warning("无法读取人工改动指标版本正文；保留现有缓存", exc_info=True)
        return

    for record, anchor, human in pending:
        try:
            if anchor.id not in content_by_id or (human is not None and human.id not in content_by_id):
                raise LookupError("锚点或人工版本正文已不可用")
            original = content_by_id[anchor.id]
            final = content_by_id[human.id] if human is not None else original
            applied_at = record.updated_at
            record.human_edit_metrics = compute_edit_metrics(
                original,
                final,
                anchor_version_id=anchor.id,
                human_version_id=human.id if human is not None else None,
            )
            # SQLAlchemy's onupdate default would otherwise rewrite the applied time
            # every time this read-time cache is committed.
            if applied_at is not None:
                record.updated_at = applied_at
                flag_modified(record, "updated_at")
        except Exception:
            logger.warning(
                "计算人工改动指标失败；保留该记录原缓存：record_id=%s",
                record.id,
                exc_info=True,
            )


async def refresh_edit_metrics_for_record(db: AsyncSession, record: GenerationRecord) -> None:
    """Force refresh a single owner-authorized record while preserving old data on failure."""
    await ensure_fresh_metrics(db, [record], max_recompute=1, force=True)
