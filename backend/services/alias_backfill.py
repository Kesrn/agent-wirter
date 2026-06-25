"""人物 alias 历史回填：把已配置 alias 簇的旧脏数据合并到规范名。

dry-run：只读，返回影响行数，不改库。
apply：实际合并 4 张表（character_profile / ability_profile /
       character_appearance / event_timeline），不动原文/staging/chunks/facts。

合并语义复用 extraction_service 的 merge 规则（first_seen 取早、evidence 去重上限、
confidence 取高），保证回填后数据与后续抽取口径一致。
"""

from __future__ import annotations

import logging
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.structured_knowledge import (
    CharacterAliasCluster, CharacterProfile, AbilityProfile,
    EventTimeline, CharacterAppearance,
)
from services.extraction_service import _append_limited_evidence

logger = logging.getLogger(__name__)

# ability status 优先级（与 _merge_ability 一致）
_STATUS_PRIORITY = {"new": 5, "upgraded": 4, "used": 3, "mentioned": 2, "lost": 1, "unknown": 0}


async def _load_clusters(db: AsyncSession, project_id: str, cluster_id: str | None = None):
    stmt = select(CharacterAliasCluster).where(CharacterAliasCluster.project_id == str(project_id))
    if cluster_id:
        stmt = stmt.where(CharacterAliasCluster.id == cluster_id)
    return list((await db.execute(stmt)).scalars().all())


# ── dry-run（只读） ───────────────────────────────────────

async def backfill_preview(db: AsyncSession, project_id: str, cluster_id: str | None = None) -> dict:
    """只读：对每个 alias 簇统计将影响多少行 + 是否会撞 UNIQUE（需合并）。"""
    pid = str(project_id)
    clusters = await _load_clusters(db, pid, cluster_id)
    result = {"clusters": []}
    for c in clusters:
        canonical = c.canonical_name
        aliases = list(c.aliases or [])
        impact = {"character_profile": {}, "ability_profile": {}, "character_appearance": {}, "event_timeline": {}}
        for alias in aliases:
            # character_profile
            src_profile = (await db.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == pid)
                .where(CharacterProfile.name == alias)
            )).scalar_one_or_none()
            if src_profile:
                tgt = (await db.execute(
                    select(CharacterProfile)
                    .where(CharacterProfile.project_id == pid)
                    .where(CharacterProfile.name == canonical)
                )).scalar_one_or_none()
                impact["character_profile"]["source"] = impact["character_profile"].get("source", 0) + 1
                impact["character_profile"]["target_exists"] = tgt is not None
            # ability_profile
            src_abilities = list((await db.execute(
                select(AbilityProfile)
                .where(AbilityProfile.project_id == pid)
                .where(AbilityProfile.character_name == alias)
            )).scalars().all())
            if src_abilities:
                conflicts = 0
                for a in src_abilities:
                    tgt = (await db.execute(
                        select(AbilityProfile)
                        .where(AbilityProfile.project_id == pid)
                        .where(AbilityProfile.character_name == canonical)
                        .where(AbilityProfile.ability_type == a.ability_type)
                        .where(AbilityProfile.ability_name == a.ability_name)
                    )).scalar_one_or_none()
                    if tgt:
                        conflicts += 1
                impact["ability_profile"]["source"] = impact["ability_profile"].get("source", 0) + len(src_abilities)
                impact["ability_profile"]["merge_conflicts"] = impact["ability_profile"].get("merge_conflicts", 0) + conflicts
            # character_appearance
            src_apps = list((await db.execute(
                select(CharacterAppearance)
                .where(CharacterAppearance.project_id == pid)
                .where(CharacterAppearance.canonical_name == alias)
            )).scalars().all())
            if src_apps:
                ch_conflicts = 0
                for ap in src_apps:
                    tgt = (await db.execute(
                        select(CharacterAppearance)
                        .where(CharacterAppearance.project_id == pid)
                        .where(CharacterAppearance.canonical_name == canonical)
                        .where(CharacterAppearance.chapter_no == ap.chapter_no)
                    )).scalar_one_or_none()
                    if tgt:
                        ch_conflicts += 1
                impact["character_appearance"]["source"] = impact["character_appearance"].get("source", 0) + len(src_apps)
                impact["character_appearance"]["chapter_conflicts"] = impact["character_appearance"].get("chapter_conflicts", 0) + ch_conflicts
            # event_timeline（characters JSON 数组包含该异体名）
            # 用 Python 判断成员关系：JSON 存的是 unicode 转义，SQL like 匹配不可靠
            events = list((await db.execute(
                select(EventTimeline.characters)
                .where(EventTimeline.project_id == pid)
            )).scalars().all())
            ev_n = sum(1 for chars in events if chars and alias in chars)
            if ev_n:
                impact["event_timeline"]["source"] = impact["event_timeline"].get("source", 0) + ev_n
        result["clusters"].append({"canonical": canonical, "aliases": aliases, "impact": impact})
    return result


# ── apply（实际合并） ────────────────────────────────────

async def apply_backfill(db: AsyncSession, project_id: str, cluster_id: str | None = None) -> dict:
    """对每个 alias 簇，把异体名数据合并到规范名。"""
    pid = str(project_id)
    clusters = await _load_clusters(db, pid, cluster_id)
    stats = {"merged_profiles": 0, "moved_abilities": 0, "merged_abilities": 0,
             "moved_appearances": 0, "merged_appearances": 0, "updated_events": 0}

    for c in clusters:
        canonical = c.canonical_name
        for alias in (c.aliases or []):
            await _backfill_one(db, pid, alias, canonical, stats)
        # 回填后重算规范 profile.appearance_count
        await _refresh_appearance_count(db, pid, canonical)

    await db.flush()
    logger.info("alias backfill: project=%s stats=%s", pid, stats)
    return stats


async def _backfill_one(db: AsyncSession, pid: str, alias: str, canonical: str, stats: dict) -> None:
    """把单个异体名 alias 的数据合并到 canonical。"""
    # 1. character_profile
    await _backfill_profile(db, pid, alias, canonical, stats)
    # 2. ability_profile
    await _backfill_ability(db, pid, alias, canonical, stats)
    # 3. character_appearance
    await _backfill_appearance(db, pid, alias, canonical, stats)
    # 4. event_timeline
    await _backfill_events(db, pid, alias, canonical, stats)


async def _backfill_profile(db, pid, alias, canonical, stats):
    src = (await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
        .where(CharacterProfile.name == alias)
    )).scalar_one_or_none()
    if not src:
        return
    tgt = (await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
        .where(CharacterProfile.name == canonical)
    )).scalar_one_or_none()
    if tgt is None:
        # 目标不存在：直接重命名
        src.name = canonical
        src.aliases = list(dict.fromkeys(list(src.aliases or []) + [alias]))
        stats["merged_profiles"] += 1
        return
    # 合并
    tgt.aliases = list(dict.fromkeys(list(tgt.aliases or []) + list(src.aliases or []) + [alias]))
    for ev in (src.evidence or []):
        tgt.evidence = _append_limited_evidence(tgt.evidence, ev)
    if src.first_seen_chapter is not None:
        tgt.first_seen_chapter = min(tgt.first_seen_chapter or src.first_seen_chapter, src.first_seen_chapter)
    if src.last_seen_chapter is not None:
        tgt.last_seen_chapter = max(tgt.last_seen_chapter or src.last_seen_chapter, src.last_seen_chapter)
    if not tgt.identity_desc and src.identity_desc:
        tgt.identity_desc = src.identity_desc
    if not tgt.status_desc and src.status_desc:
        tgt.status_desc = src.status_desc
    tgt.confidence = max(tgt.confidence, src.confidence)
    await db.delete(src)
    stats["merged_profiles"] += 1


async def _backfill_ability(db, pid, alias, canonical, stats):
    src_abilities = list((await db.execute(
        select(AbilityProfile)
        .where(AbilityProfile.project_id == pid)
        .where(AbilityProfile.character_name == alias)
    )).scalars().all())
    for a in src_abilities:
        tgt = (await db.execute(
            select(AbilityProfile)
            .where(AbilityProfile.project_id == pid)
            .where(AbilityProfile.character_name == canonical)
            .where(AbilityProfile.ability_type == a.ability_type)
            .where(AbilityProfile.ability_name == a.ability_name)
        )).scalar_one_or_none()
        if tgt is None:
            a.character_name = canonical
            stats["moved_abilities"] += 1
        else:
            # 撞 UNIQUE：合并
            if tgt.first_seen_chapter is not None or a.first_seen_chapter is not None:
                tgt.first_seen_chapter = min(
                    x for x in (tgt.first_seen_chapter, a.first_seen_chapter) if x is not None)
            for ev in (a.evidence or []):
                if ev not in (tgt.evidence or []):
                    tgt.evidence = (tgt.evidence or []) + [ev]
            tgt.confidence = max(tgt.confidence, a.confidence)
            if _STATUS_PRIORITY.get(a.status or "", 0) > _STATUS_PRIORITY.get(tgt.status or "", 0):
                tgt.status = a.status
            await db.delete(a)
            stats["merged_abilities"] += 1


async def _backfill_appearance(db, pid, alias, canonical, stats):
    src_apps = list((await db.execute(
        select(CharacterAppearance)
        .where(CharacterAppearance.project_id == pid)
        .where(CharacterAppearance.canonical_name == alias)
    )).scalars().all())
    # 规范 profile id（用于 character_id）
    tgt_profile = (await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
        .where(CharacterProfile.name == canonical)
    )).scalar_one_or_none()
    tgt_pid = str(tgt_profile.id) if tgt_profile else None
    for ap in src_apps:
        tgt = (await db.execute(
            select(CharacterAppearance)
            .where(CharacterAppearance.project_id == pid)
            .where(CharacterAppearance.canonical_name == canonical)
            .where(CharacterAppearance.chapter_no == ap.chapter_no)
        )).scalar_one_or_none()
        if tgt is None:
            ap.canonical_name = canonical
            ap.character_name = canonical
            ap.character_id = tgt_pid
            stats["moved_appearances"] += 1
        else:
            # 同章冲突：合并
            if not tgt.character_id and tgt_pid:
                tgt.character_id = tgt_pid
            if not tgt.evidence_text and ap.evidence_text:
                tgt.evidence_text = ap.evidence_text
            tgt.importance = max(tgt.importance, ap.importance)
            tgt.confidence = max(tgt.confidence, ap.confidence)
            await db.delete(ap)
            stats["merged_appearances"] += 1


async def _backfill_events(db, pid, alias, canonical, stats):
    events = list((await db.execute(
        select(EventTimeline)
        .where(EventTimeline.project_id == pid)
    )).scalars().all())
    for e in events:
        chars = e.characters or []
        if alias not in chars:
            continue
        new_chars: list[str] = []
        seen: set[str] = set()
        changed = False
        for c in chars:
            nc = canonical if c == alias else c
            if nc not in seen:
                seen.add(nc)
                new_chars.append(nc)
            if c != nc:
                changed = True
        if changed or len(new_chars) != len(chars):
            e.characters = new_chars
            stats["updated_events"] += 1


async def _refresh_appearance_count(db, pid, canonical):
    prof = (await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
        .where(CharacterProfile.name == canonical)
    )).scalar_one_or_none()
    if not prof:
        return
    cnt = (await db.execute(
        select(func.count(CharacterAppearance.id))
        .where(CharacterAppearance.project_id == pid)
        .where(CharacterAppearance.canonical_name == canonical)
    )).scalar() or 0
    prof.appearance_count = cnt
