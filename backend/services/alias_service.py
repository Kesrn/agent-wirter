"""人物别名归一簇：CRUD + 候选探测。

项目级 alias 表替代硬编码的 _CHARACTER_ALIAS_CLUSTERS。
候选探测只读、不自动写库——必须人工确认后才入表，才影响抽取/查询。
"""

from __future__ import annotations

import logging
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.structured_knowledge import (
    CharacterAliasCluster, CharacterProfile, CharacterAppearance,
)

logger = logging.getLogger(__name__)


# ── CRUD ─────────────────────────────────────────────────

class AliasClusterConflictError(ValueError):
    """Raised when an alias name is already owned by another cluster."""


def _clean_name_list(names: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


async def _ensure_alias_names_available(
    db: AsyncSession,
    project_id: str,
    *,
    canonical_name: str,
    aliases: list[str],
    exclude_cluster_id: str | None = None,
) -> None:
    """Ensure every name in this cluster is not claimed by another cluster.

    The DB unique constraint only protects (project_id, canonical_name). It cannot
    protect JSON aliases, so we enforce the full "one name belongs to one cluster"
    rule in service code before writing.
    """
    names = set(_clean_name_list([canonical_name] + aliases))
    if not canonical_name.strip():
        raise ValueError("canonical_name 不能为空")
    if not names:
        raise ValueError("alias 簇至少需要一个有效名称")

    rows = (await db.execute(
        select(CharacterAliasCluster)
        .where(CharacterAliasCluster.project_id == str(project_id))
    )).scalars().all()

    for cluster in rows:
        if exclude_cluster_id and str(cluster.id) == str(exclude_cluster_id):
            continue
        owned = set(_clean_name_list([cluster.canonical_name] + list(cluster.aliases or [])))
        overlap = names & owned
        if overlap:
            conflict = sorted(overlap)[0]
            raise AliasClusterConflictError(
                f"名称「{conflict}」已属于「{cluster.canonical_name}」alias 簇"
            )

def _cluster_to_dict(c: CharacterAliasCluster) -> dict:
    return {
        "id": str(c.id),
        "project_id": str(c.project_id),
        "canonical_name": c.canonical_name,
        "aliases": c.aliases or [],
        "source": c.source,
        "confidence": c.confidence,
        "note": c.note,
    }


async def list_alias_clusters(db: AsyncSession, project_id: str) -> list[dict]:
    rows = (await db.execute(
        select(CharacterAliasCluster)
        .where(CharacterAliasCluster.project_id == str(project_id))
        .order_by(CharacterAliasCluster.canonical_name)
    )).scalars().all()
    return [_cluster_to_dict(c) for c in rows]


async def create_alias_cluster(
    db: AsyncSession, project_id: str, *,
    canonical_name: str, aliases: list[str], source: str = "manual",
    confidence: float = 1.0, note: str | None = None,
) -> dict:
    canonical_name = str(canonical_name or "").strip()
    clean_aliases = [a for a in _clean_name_list(aliases) if a != canonical_name]
    await _ensure_alias_names_available(
        db, project_id, canonical_name=canonical_name, aliases=clean_aliases,
    )
    cluster = CharacterAliasCluster(
        project_id=str(project_id),
        canonical_name=canonical_name,
        aliases=clean_aliases,
        source=source, confidence=confidence, note=note,
    )
    db.add(cluster)
    await db.flush()
    logger.info("create alias cluster: project=%s canonical=%s aliases=%s",
                project_id, canonical_name, aliases)
    return _cluster_to_dict(cluster)


async def update_alias_cluster(
    db: AsyncSession, project_id: str, cluster_id: str, *,
    aliases: list[str] | None = None, note: str | None = None,
    confidence: float | None = None,
) -> dict | None:
    cluster = (await db.execute(
        select(CharacterAliasCluster)
        .where(CharacterAliasCluster.id == cluster_id)
        .where(CharacterAliasCluster.project_id == str(project_id))
    )).scalar_one_or_none()
    if not cluster:
        return None
    if aliases is not None:
        clean_aliases = [a for a in _clean_name_list(aliases) if a != cluster.canonical_name]
        await _ensure_alias_names_available(
            db, project_id,
            canonical_name=cluster.canonical_name,
            aliases=clean_aliases,
            exclude_cluster_id=str(cluster.id),
        )
        cluster.aliases = clean_aliases
    if note is not None:
        cluster.note = note
    if confidence is not None:
        cluster.confidence = confidence
    await db.flush()
    return _cluster_to_dict(cluster)


async def delete_alias_cluster(db: AsyncSession, project_id: str, cluster_id: str) -> bool:
    cluster = (await db.execute(
        select(CharacterAliasCluster)
        .where(CharacterAliasCluster.id == cluster_id)
        .where(CharacterAliasCluster.project_id == str(project_id))
    )).scalar_one_or_none()
    if not cluster:
        return False
    await db.delete(cluster)
    return True


# ── 候选探测（只读，不写库） ───────────────────────────────

async def detect_alias_candidates(db: AsyncSession, project_id: str) -> list[dict]:
    """扫描人物名/别名，输出疑似同人的候选簇。

    规则（保守，第一版）：
      1. 子串：A⊂B（如 心夏⊂叶心夏）且非完全等名 → 候选
      2. aliases 交叉：A.aliases 含 B.name 或反之 → 候选
    不做同音/错字/编辑距离。
    不写库——只返回建议，需人工 CRUD 确认后才影响抽取。
    """
    pid = str(project_id)

    # 收集人物名 + aliases（来自 character_profile）+ appearance 名
    profiles = (await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
    )).scalars().all()
    names_from_profile = {p.name for p in profiles}
    name_to_aliases: dict[str, list[str]] = {
        p.name: list(p.aliases or []) for p in profiles
    }

    app_rows = (await db.execute(
        select(CharacterAppearance.character_name, CharacterAppearance.canonical_name)
        .where(CharacterAppearance.project_id == pid)
        .distinct()
    )).all()
    for r in app_rows:
        cn = r[1] if r[1] else r[0]
        if cn:
            names_from_profile.add(cn)
        if r[0] and r[0] != cn:
            names_from_profile.add(r[0])

    all_names = sorted(n for n in names_from_profile if n)

    # 出场章数（用于 evidence_count）
    if all_names:
        # 简化：统计每个名字的出场章数，候选对的 evidence_count 取两者较小
        app_counts = (await db.execute(
            select(CharacterAppearance.canonical_name,
                   func.count(func.distinct(CharacterAppearance.chapter_no)))
            .where(CharacterAppearance.project_id == pid)
            .group_by(CharacterAppearance.canonical_name)
        )).all()
        count_map = {r[0] or "": r[1] for r in app_counts}

    candidates: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()

    for i, a in enumerate(all_names):
        for b in all_names[i + 1:]:
            if a == b:
                continue
            reason = None
            confidence = 0.0
            # 规则1 子串
            if a in b or b in a:
                reason = "substring"
                confidence = 0.6
            # 规则2 aliases 交叉
            elif (a in name_to_aliases.get(b, [])) or (b in name_to_aliases.get(a, [])):
                reason = "alias_cross"
                confidence = 0.7
            if not reason:
                continue
            pair = frozenset({a, b})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # 规范名取较长者（如 叶心夏 vs 心夏 → 叶心夏）
            canonical = a if len(a) >= len(b) else b
            alias = b if canonical == a else a
            # 共现章数取两者较小
            ec = min(count_map.get(a, 0), count_map.get(b, 0)) if 'count_map' in dir() else 0
            candidates.append({
                "canonical_name": canonical,
                "aliases": [alias],
                "reason": reason,
                "confidence": confidence,
                "evidence_count": ec,
            })

    # 标记已配置
    configured_names: set[str] = set()
    configured_clusters = (await db.execute(
        select(CharacterAliasCluster)
        .where(CharacterAliasCluster.project_id == pid)
    )).scalars().all()
    for cluster in configured_clusters:
        configured_names.update(_clean_name_list([cluster.canonical_name] + list(cluster.aliases or [])))
    for cand in candidates:
        cand_names = set(_clean_name_list([cand["canonical_name"]] + list(cand.get("aliases") or [])))
        cand["already_configured"] = bool(cand_names & configured_names)

    return candidates
