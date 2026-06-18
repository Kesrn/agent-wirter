"""规则事实索引服务 —— 写入、重建、查询。

reindex/upload 时扫描全量 chunk，预计算"人物 -> 法系"事实写入 project_knowledge_facts。
QA 时优先查事实表，命中则用确定性答案。
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.project_knowledge_fact import ProjectKnowledgeFact
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from services.knowledge_fact_rules import extract_character_system_facts_from_text

logger = logging.getLogger(__name__)

FACT_TYPE_CHARACTER_SYSTEM = "character_system"
PREDICATE_HAS_MAGIC_SYSTEM = "has_magic_system"
EXTRACTOR_CHARACTER_SYSTEM_V1 = "rule.character_system.v1"

# 分页读取 chunk，避免一次性载入 7000+ chunks
_CHUNK_BATCH_SIZE = 500


async def rebuild_source_facts(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
    *,
    fact_types: list[str] | None = None,
) -> dict:
    """重建单个 source 的事实索引。

    1. 删除该 source 旧 facts
    2. 分页读取 chunks
    3. 规则扫描抽取事实
    4. 去重写入
    返回 {"source_id": ..., "chunk_count": N, "fact_count": M}
    """
    pid = str(project_id)
    sid = str(source_id)
    types = fact_types or [FACT_TYPE_CHARACTER_SYSTEM]

    # 1. 删除旧 facts
    await db.execute(
        delete(ProjectKnowledgeFact)
        .where(ProjectKnowledgeFact.project_id == pid)
        .where(ProjectKnowledgeFact.source_id == sid)
    )

    # 2. 查 source 标题（用于 metadata）
    src_result = await db.execute(
        select(ProjectSource.title).where(ProjectSource.id == sid)
    )
    source_title = src_result.scalar() or ""

    # 3. 分页读取 chunks 并抽取
    fact_count = 0
    chunk_count = 0
    seen_keys: set[tuple] = set()  # (fact_type, subject, predicate, object, evidence_text[:200])
    facts_to_add: list[ProjectKnowledgeFact] = []

    offset = 0
    while True:
        result = await db.execute(
            select(ProjectSourceChunk)
            .where(ProjectSourceChunk.source_id == sid)
            .order_by(ProjectSourceChunk.chunk_index)
            .offset(offset)
            .limit(_CHUNK_BATCH_SIZE)
        )
        chunks = result.scalars().all()
        if not chunks:
            break
        chunk_count += len(chunks)

        for chunk in chunks:
            candidates = extract_character_system_facts_from_text(
                chunk.content or "",
                source_title=source_title,
            )
            for cand in candidates:
                if FACT_TYPE_CHARACTER_SYSTEM not in types:
                    continue
                dedup_key = (
                    FACT_TYPE_CHARACTER_SYSTEM,
                    cand.subject,
                    PREDICATE_HAS_MAGIC_SYSTEM,
                    cand.object,
                    cand.evidence_text[:200],
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                facts_to_add.append(ProjectKnowledgeFact(
                    project_id=pid,
                    source_id=sid,
                    chunk_id=str(chunk.id) if chunk.id else None,
                    fact_type=FACT_TYPE_CHARACTER_SYSTEM,
                    subject=cand.subject,
                    predicate=PREDICATE_HAS_MAGIC_SYSTEM,
                    object=cand.object,
                    confidence=cand.confidence,
                    evidence_text=cand.evidence_text,
                    evidence_start=cand.evidence_start,
                    evidence_end=cand.evidence_end,
                    extractor=EXTRACTOR_CHARACTER_SYSTEM_V1,
                    metadata_=cand.metadata or None,
                ))
                fact_count += 1

        offset += _CHUNK_BATCH_SIZE
        if len(chunks) < _CHUNK_BATCH_SIZE:
            break

    if facts_to_add:
        db.add_all(facts_to_add)
    await db.commit()

    logger.info(
        "rebuild_source_facts: project=%s source=%s chunks=%d facts=%d",
        pid, sid, chunk_count, fact_count,
    )
    return {"source_id": sid, "chunk_count": chunk_count, "fact_count": fact_count}


async def rebuild_project_facts(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    *,
    fact_types: list[str] | None = None,
) -> dict:
    """重建整个项目的事实索引。返回 {source_count, chunk_count, fact_count}。"""
    pid = str(project_id)
    result = await db.execute(
        select(ProjectSource.id).where(ProjectSource.project_id == pid)
    )
    source_ids = [str(r) for r in result.scalars().all()]

    total_chunks = 0
    total_facts = 0
    for sid in source_ids:
        r = await rebuild_source_facts(db, pid, sid, fact_types=fact_types)
        total_chunks += r["chunk_count"]
        total_facts += r["fact_count"]

    logger.info(
        "rebuild_project_facts: project=%s sources=%d chunks=%d facts=%d",
        pid, len(source_ids), total_chunks, total_facts,
    )
    return {
        "source_count": len(source_ids),
        "chunk_count": total_chunks,
        "fact_count": total_facts,
    }


def _dedupe_character_system_facts(
    facts: Iterable[ProjectKnowledgeFact],
    *,
    limit: int,
) -> list[ProjectKnowledgeFact]:
    """Collapse repeated evidence rows for the same character-system binding.

    The fact table may intentionally keep multiple evidence rows from different
    chunks. QA citations should not show all of them as duplicate-looking rows.
    """
    deduped: list[ProjectKnowledgeFact] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        key = (fact.fact_type, fact.subject, fact.predicate, fact.object)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
        if len(deduped) >= limit:
            break
    return deduped


async def query_character_system_facts(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    subject: str | None = None,
    system: str | None = None,
    limit: int = 50,
) -> list[ProjectKnowledgeFact]:
    """查询人物-法系事实。

    - subject 非空：查某人物的所有法系（正向查询）
    - system 非空：查某法系的所有人物（反向查询）
    默认只返回 explicit 置信度。
    """
    pid = str(project_id)
    stmt = (
        select(ProjectKnowledgeFact)
        .where(ProjectKnowledgeFact.project_id == pid)
        .where(ProjectKnowledgeFact.fact_type == FACT_TYPE_CHARACTER_SYSTEM)
        .where(ProjectKnowledgeFact.predicate == PREDICATE_HAS_MAGIC_SYSTEM)
        .where(ProjectKnowledgeFact.confidence == "explicit")
    )
    if subject:
        stmt = stmt.where(ProjectKnowledgeFact.subject == subject)
    if system:
        # 法系名归一化后匹配
        from services.knowledge_source import _normalize_system_name
        stmt = stmt.where(ProjectKnowledgeFact.object == _normalize_system_name(system))
    # Fetch extra rows because repeated evidence for the same binding may be
    # collapsed below. This keeps the public limit as "unique bindings".
    fetch_limit = max(limit * 20, 200)
    stmt = stmt.order_by(
        ProjectKnowledgeFact.subject,
        ProjectKnowledgeFact.object,
        ProjectKnowledgeFact.created_at,
    ).limit(fetch_limit)
    result = await db.execute(stmt)
    return _dedupe_character_system_facts(result.scalars().all(), limit=limit)


async def list_facts(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    *,
    fact_type: str | None = None,
    subject: str | None = None,
    object_: str | None = None,
    source_id: str | None = None,
    confidence: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ProjectKnowledgeFact], int]:
    """列出事实（带过滤与分页）。返回 (items, total)。"""
    pid = str(project_id)
    base = select(ProjectKnowledgeFact).where(ProjectKnowledgeFact.project_id == pid)
    count_base = select(func.count(ProjectKnowledgeFact.id)).where(ProjectKnowledgeFact.project_id == pid)

    if fact_type:
        base = base.where(ProjectKnowledgeFact.fact_type == fact_type)
        count_base = count_base.where(ProjectKnowledgeFact.fact_type == fact_type)
    if subject:
        base = base.where(ProjectKnowledgeFact.subject == subject)
        count_base = count_base.where(ProjectKnowledgeFact.subject == subject)
    if object_:
        base = base.where(ProjectKnowledgeFact.object == object_)
        count_base = count_base.where(ProjectKnowledgeFact.object == object_)
    if source_id:
        base = base.where(ProjectKnowledgeFact.source_id == source_id)
        count_base = count_base.where(ProjectKnowledgeFact.source_id == source_id)
    if confidence:
        base = base.where(ProjectKnowledgeFact.confidence == confidence)
        count_base = count_base.where(ProjectKnowledgeFact.confidence == confidence)

    total = (await db.execute(count_base)).scalar() or 0
    base = base.order_by(ProjectKnowledgeFact.created_at.desc()).offset(offset).limit(limit)
    items = list((await db.execute(base)).scalars().all())
    return items, total
