"""切分代服务（P-1 阶段四：legacy 迁移 / 新代构建 / 完整性验收 / CAS 激活 / 回滚）。

方案 v3 §3.5 的写库实现。**async SQLAlchemy 双后端**（PostgreSQL / SQLite 一致），
与项目其他服务层同一约定：函数只接收 AsyncSession，不自己 commit/rollback，
由调用方决定事务边界（FastAPI 路由 / CLI 脚本 / 测试）。

规则 7（所有章节读取统一按活动代过滤）的落点：
- ``get_active_generation_id`` / ``get_active_chapters`` / ``get_chapters_for_generation``
  是业务层的唯一章节查询入口；禁止各业务 SQL 自行拼接代过滤。
- legacy 代语义：display_chapter_no 为 NULL（旧数据无可靠展示号），按
  source_ordinal（= 旧 chapter_no）读取；新代 display / 偏移 / 哈希齐全。

激活门槛（构建时 + 激活时双校验）：
- 构建：ledger.ready_for_write == True、decisions_hash **重新计算验证**、
  source_content_hash 与当前原文一致、账本 ordinal 与解析结果一致；
- 激活：目标代必须 ready，且**重新读取原文复核 source_content_hash** 未漂移；
  CAS 条件更新必须传旧代（不能只用 IS NULL），并发下先到者生效。

安全边界：本模块只在传入的 session 上写库；legacy 占位代不参与 active 切换竞态，
任何代切换都不会删除数据（旧代保留供回滚）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.extraction_pipeline import ProjectSourceChapter
from models.project_source import ProjectSource
from models.split_generation import (
    SourceSplitState,
    SplitExclusion,
    SplitGeneration,
)
from services.chapter_parser import (
    ChapterHeading,
    PARSER_VERSION,
    build_validation_report,
    parse_headings,
)

# 状态枚举（与方案 v3 §3.5 一致）
STATUS_BUILDING = "building"
STATUS_VALIDATING = "validating"
STATUS_READY = "ready"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"
STATUS_LEGACY = "legacy"


class SplitGenerationError(RuntimeError):
    """切换/构建失败（门槛不满足 / 并发冲突）。"""


# ── 工具 ─────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _recompute_decisions_hash(ledger: dict) -> str:
    """按第 0 步同一规范化重新计算 decisions_hash（防篡改验证）。

    规范与 chapter_decision_ledger.build_resolved_snapshot 完全一致：
    decisions 各动作组按 (ordinal, display_no) 排序，顶层 sort_keys 序列化。
    """
    norm = {
        "schema_version": 1,
        "source_content_hash": ledger.get("source_content_hash"),
        "decisions": {
            k: sorted(v or [], key=lambda x: (x.get("ordinal") or 0,
                                              x.get("display_no") or 0))
            for k, v in ledger.get("decisions", {}).items()
        },
        "gap_resolution": ledger.get("gap_resolution", {}),
    }
    return hashlib.sha256(
        json.dumps(norm, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


def _as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ── 统一查询仓库（规则 7：业务层唯一章节读取入口） ──────────

async def get_active_generation(
    db: AsyncSession, project_id, source_id
) -> SplitGeneration | None:
    """当前活动代（无指针返回 None）。"""
    row = (
        await db.execute(
            select(SplitGeneration)
            .join(SourceSplitState, SourceSplitState.active_split_generation_id == SplitGeneration.id)
            .where(SourceSplitState.project_id == _as_uuid(project_id))
            .where(SourceSplitState.source_id == _as_uuid(source_id))
        )
    ).scalar_one_or_none()
    return row


async def get_active_generation_id(
    db: AsyncSession, project_id, source_id
) -> uuid.UUID | None:
    """当前活动代 id（抽取任务锁定 / 章节查询过滤用）。"""
    active = await get_active_generation(db, project_id, source_id)
    return active.id if active else None


async def list_generations(
    db: AsyncSession, project_id, source_id
) -> list[SplitGeneration]:
    rows = (
        await db.execute(
            select(SplitGeneration)
            .where(SplitGeneration.project_id == _as_uuid(project_id))
            .where(SplitGeneration.source_id == _as_uuid(source_id))
            .order_by(SplitGeneration.created_at)
        )
    ).scalars().all()
    return list(rows)


async def _chapter_stmt(project_id, source_id, generation_id):
    """章节查询语句（规则 7 统一过滤）。

    generation_id 为 None（未迁移库，无任何代）→ 读 split_generation_id IS NULL
    的行（= 旧行为全量）；否则限定到指定代，杜绝 legacy + 新代同时被读到。
    """
    stmt = (
        select(ProjectSourceChapter)
        .where(ProjectSourceChapter.project_id == _as_uuid(project_id))
        .where(ProjectSourceChapter.source_id == _as_uuid(source_id))
    )
    if generation_id is None:
        stmt = stmt.where(ProjectSourceChapter.split_generation_id.is_(None))
    else:
        stmt = stmt.where(
            ProjectSourceChapter.split_generation_id == _as_uuid(generation_id)
        )
    return stmt


async def get_chapters_for_generation(
    db: AsyncSession,
    project_id,
    source_id,
    generation_id,
    chapter_no_start: int | None = None,
    chapter_no_end: int | None = None,
) -> list[ProjectSourceChapter]:
    """某代内的章节行（按 source_ordinal 排序；可加 chapter_no 范围）。

    chapter_no 兼容列 = source_ordinal（新代），legacy 行 = 旧顺序号。
    抽取任务的范围内章节查询统一走这里。
    """
    stmt = await _chapter_stmt(project_id, source_id, generation_id)
    if chapter_no_start:
        stmt = stmt.where(ProjectSourceChapter.chapter_no >= chapter_no_start)
    if chapter_no_end:
        stmt = stmt.where(ProjectSourceChapter.chapter_no <= chapter_no_end)
    stmt = stmt.order_by(ProjectSourceChapter.chapter_no)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def get_active_chapters(
    db: AsyncSession,
    project_id,
    source_id,
    chapter_no_start: int | None = None,
    chapter_no_end: int | None = None,
) -> tuple[SplitGeneration | None, list[ProjectSourceChapter]]:
    """活动代章节行（统一入口，业务读取/计数/重试都走这里）。

    返回 (active_generation, chapters)。无活动代（未迁移库）时返回
    (None, NULL 行)——旧行为全量，兼容迁移前的库。
    """
    active = await get_active_generation(db, project_id, source_id)
    generation_id = active.id if active else None
    chapters = await get_chapters_for_generation(
        db, project_id, source_id, generation_id,
        chapter_no_start=chapter_no_start, chapter_no_end=chapter_no_end,
    )
    return active, chapters


async def count_active_chapters(
    db: AsyncSession, project_id, source_id,
    chapter_no_start: int | None = None, chapter_no_end: int | None = None,
) -> int:
    """活动代章节计数（不加载行，抽取任务统计用）。"""
    active = await get_active_generation(db, project_id, source_id)
    generation_id = active.id if active else None
    return await count_chapters_for_generation(
        db, project_id, source_id, generation_id,
        chapter_no_start=chapter_no_start, chapter_no_end=chapter_no_end,
    )


async def count_chapters_for_generation(
    db: AsyncSession, project_id, source_id, generation_id,
    chapter_no_start: int | None = None, chapter_no_end: int | None = None,
) -> int:
    """指定代内的章节计数（抽取任务统计用，job 锁定代优先）。"""
    stmt = await _chapter_stmt(project_id, source_id, generation_id)
    if chapter_no_start:
        stmt = stmt.where(ProjectSourceChapter.chapter_no >= chapter_no_start)
    if chapter_no_end:
        stmt = stmt.where(ProjectSourceChapter.chapter_no <= chapter_no_end)
    sub = stmt.order_by(None).subquery()
    return (await db.execute(select(func.count()).select_from(sub))).scalar() or 0


# ── legacy 迁移（幂等） ──────────────────────────────────

async def ensure_legacy_generation(
    db: AsyncSession, project_id, source_id
) -> uuid.UUID:
    """为 (project_id, source_id) 建立 legacy 占位代并回填现有章节行（幂等）。

    迁移方案（方案 v3 §3.5 旧数据迁移）：
      1. 建 legacy generation（status='legacy'，parser_version='legacy'，
         decisions_hash=NULL，source_content_hash=当前原文哈希）；
      2. 现有章节回填 split_generation_id=legacy + source_ordinal=旧 chapter_no
         + display_chapter_no=NULL（旧数据无可靠展示号，标记待重切）；
      3. source_split_state 插入活动指针 → legacy（仅当该 source 尚无指针）。
    返回 legacy generation id。调用方负责 commit。
    """
    pid, sid = _as_uuid(project_id), _as_uuid(source_id)

    legacy = (
        await db.execute(
            select(SplitGeneration)
            .where(SplitGeneration.project_id == pid)
            .where(SplitGeneration.source_id == sid)
            .where(SplitGeneration.status == STATUS_LEGACY)
            .order_by(SplitGeneration.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    if legacy is None:
        source = (
            await db.execute(
                select(ProjectSource.content).where(ProjectSource.id == sid)
            )
        ).scalar_one_or_none()
        if source is None:
            raise SplitGenerationError(f"source not found: {sid}")
        legacy = SplitGeneration(
            project_id=pid,
            source_id=sid,
            source_content_hash=_sha256(source or ""),
            parser_version="legacy",
            status=STATUS_LEGACY,
        )
        db.add(legacy)
        await db.flush()

    rows = (
        await db.execute(
            select(ProjectSourceChapter)
            .where(ProjectSourceChapter.project_id == pid)
            .where(ProjectSourceChapter.source_id == sid)
            .where(ProjectSourceChapter.split_generation_id.is_(None))
        )
    ).scalars().all()
    for row in rows:
        row.split_generation_id = legacy.id
        row.source_ordinal = row.chapter_no
        row.display_chapter_no = None
        row.original_label = row.chapter_title

    state = (
        await db.execute(
            select(SourceSplitState)
            .where(SourceSplitState.project_id == pid)
            .where(SourceSplitState.source_id == sid)
        )
    ).scalar_one_or_none()
    if state is None:
        db.add(SourceSplitState(
            project_id=pid,
            source_id=sid,
            active_split_generation_id=legacy.id,
        ))

    return legacy.id


# ── 新代构建 ─────────────────────────────────────────────

def _apply_ledger_decisions(
    headings: list[ChapterHeading], ledger: dict
) -> tuple[dict[int, dict], list[str]]:
    """把决策账本的决议映射到每个章节（keep/renumber/keep_duplicate/drop）。

    return (decision_map, errors)：decision_map ordinal → {action, display_no}，
    未在账本中的章节 = 默认 keep（保留原 display_no）。
    """
    errors: list[str] = []
    decision_map: dict[int, dict] = {}
    for item in ledger.get("decisions", {}).get("drop", []):
        decision_map[item["ordinal"]] = {
            "action": "drop",
            "display_no": item.get("display_no"),
        }
    for item in ledger.get("decisions", {}).get("renumber", []):
        decision_map[item["ordinal"]] = {
            "action": "renumber",
            "target": item.get("to"),
            "display_no": item.get("display_no"),
        }
    for item in ledger.get("decisions", {}).get("keep_duplicate", []):
        decision_map[item["ordinal"]] = {
            "action": "keep_duplicate",
            "display_no": item.get("display_no"),
        }
    for item in ledger.get("decisions", {}).get("unresolved", []):
        errors.append(f"账本含 unresolved：ordinal={item.get('ordinal')}（不得写库）")

    known = {h.ordinal for h in headings}
    for ordinal in decision_map:
        if ordinal not in known:
            errors.append(f"账本引用不存在的 ordinal={ordinal}（与解析结果不一致）")

    for h in headings:
        if h.ordinal not in decision_map:
            decision_map[h.ordinal] = {"action": "keep", "display_no": h.display_no}
    return decision_map, errors


async def build_new_generation(
    db: AsyncSession,
    project_id,
    source_id,
    ledger: dict,
) -> dict:
    """按决策账本构建新切分代（building → ready/failed）。

    写库门槛（任一不满足直接失败，不写任何行）：
      1. ledger.ready_for_write == True（第 0 步六条件硬门槛）；
      2. decisions_hash **重新计算验证**（防篡改：与规范化重算值一致）；
      3. ledger.source_content_hash == 当前原文 SHA-256（原文未变更）。
    构建内容（单事务，调用方 commit）：
      - split_generations 一行（构建后置 ready/failed，验收报告写入）；
      - 保留章节写 project_source_chapters（source_ordinal=ordinal，
        display_chapter_no=renumber 目标或原号，偏移/哈希齐全）；
      - drop 章节写 split_exclusions(kind='noise')；首部非空写 kind='preface'。
    返回 generation 记录 dict（含 validation_report）。调用方负责 commit。
    """
    pid, sid = _as_uuid(project_id), _as_uuid(source_id)

    source = (
        await db.execute(select(ProjectSource.content).where(ProjectSource.id == sid))
    ).scalar_one_or_none()
    if source is None:
        raise SplitGenerationError(f"source not found: {sid}")
    source = source or ""
    source_hash = _sha256(source)

    # ── 写库门槛 ──
    if not ledger.get("ready_for_write"):
        raise SplitGenerationError(
            "决策账本未通过 ready_for_write 硬门槛，禁止写库："
            f"{ledger.get('resolved_verdict')}"
        )
    recomputed = _recompute_decisions_hash(ledger)
    if recomputed != ledger.get("decisions_hash"):
        raise SplitGenerationError(
            "decisions_hash 重算不一致：账本声明 "
            f"{str(ledger.get('decisions_hash'))[:16]}…，重算 {recomputed[:16]}…"
            "（账本被篡改或生成工具与第 0 步不一致，需重新生成）"
        )
    if ledger.get("source_content_hash") != source_hash:
        raise SplitGenerationError(
            "原文哈希不一致：账本基线 "
            f"{str(ledger.get('source_content_hash'))[:16]}… vs 当前 {source_hash[:16]}…"
            "（原文已变更，需重新跑第 0 步）"
        )

    headings = parse_headings(source)
    decision_map, errors = _apply_ledger_decisions(headings, ledger)
    if errors:
        raise SplitGenerationError(
            "账本与解析结果不一致：\n  " + "\n  ".join(errors)
        )

    generation = SplitGeneration(
        project_id=pid,
        source_id=sid,
        source_content_hash=source_hash,
        parser_version=PARSER_VERSION,
        decisions_hash=ledger["decisions_hash"],
        decision_ledger=ledger,
        status=STATUS_BUILDING,
    )
    db.add(generation)
    await db.flush()

    kept = 0
    for h in headings:
        dec = decision_map.get(h.ordinal, {"action": "keep"})
        if dec["action"] == "drop":
            db.add(SplitExclusion(
                split_generation_id=generation.id,
                kind="noise",
                start_offset=h.start,
                end_offset=h.end,
                content_hash=h.content_hash,
                reason="drop_entire_span",
                ordinal_ref=h.ordinal,
            ))
            continue
        display_no = dec.get("target") if dec["action"] == "renumber" else dec.get("display_no")
        db.add(ProjectSourceChapter(
            project_id=pid,
            source_id=sid,
            chapter_no=h.ordinal,          # 兼容列 = source_ordinal
            chapter_title=h.title,
            content=h.content(source),
            split_type="chapter_regex",
            warning=h.warning,
            split_generation_id=generation.id,
            source_ordinal=h.ordinal,
            display_chapter_no=display_no,
            original_label=h.label,
            start_offset=h.start,
            end_offset=h.end,
            content_hash=h.content_hash,
        ))
        kept += 1

    if headings and headings[0].start > 0:
        db.add(SplitExclusion(
            split_generation_id=generation.id,
            kind="preface",
            start_offset=0,
            end_offset=headings[0].start,
            content_hash=_sha256(source[:headings[0].start]),
            reason="preface",
        ))

    await db.flush()

    # ── 完整性验收（写库后独立验证，任何失败 → failed 状态保留数据） ──
    report = await validate_generation(db, generation.id, source)
    generation.status = STATUS_READY if report["verdict"] == "PASS" else STATUS_FAILED
    generation.validation_report = report
    await db.flush()

    return {
        "id": generation.id,
        "project_id": pid,
        "source_id": sid,
        "status": generation.status,
        "parser_version": generation.parser_version,
        "decisions_hash": generation.decisions_hash,
        "source_content_hash": generation.source_content_hash,
        "kept_chapters": kept,
        "validation_report": report,
    }


# ── 完整性验收（方案 v3 §3.5 规则 6） ────────────────────

async def _chapter_intervals(db: AsyncSession, generation_id) -> list[tuple[int, int, str]]:
    rows = (
        await db.execute(
            select(ProjectSourceChapter.start_offset, ProjectSourceChapter.end_offset,
                   ProjectSourceChapter.content_hash)
            .where(ProjectSourceChapter.split_generation_id == _as_uuid(generation_id))
            .order_by(ProjectSourceChapter.source_ordinal)
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _exclusion_intervals(db: AsyncSession, generation_id) -> list[tuple[int, int, str]]:
    rows = (
        await db.execute(
            select(SplitExclusion.start_offset, SplitExclusion.end_offset,
                   SplitExclusion.content_hash)
            .where(SplitExclusion.split_generation_id == _as_uuid(generation_id))
            .order_by(SplitExclusion.start_offset)
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def validate_generation(
    db: AsyncSession, generation_id, source: str
) -> dict:
    """完整性验收：保留区间 + 排除区间（含首部）= 原始全文。

    检查项：interval_cover（零缺口）/ segment_hash / fulltext_hash /
    landmarks（613 铁笼 / 3195 梵葵 / 末章 3233）/ no_regression /
    duplicate_groups_match。verdict PASS/FAIL（含失败项明细）。
    """
    gid = _as_uuid(generation_id)
    report: dict = {
        "schema_version": 1,
        "generation_id": str(gid),
        "checks": {},
        "verdict": "PASS",
        "failed": [],
    }

    gen = (
        await db.execute(
            select(SplitGeneration.decisions_hash, SplitGeneration.source_content_hash)
            .where(SplitGeneration.id == gid)
        )
    ).one_or_none()
    if gen is None:
        raise SplitGenerationError(f"generation not found: {generation_id}")
    report["decisions_hash"] = gen[0]
    report["source_content_hash"] = gen[1]

    # 1. 区间覆盖
    intervals = await _chapter_intervals(db, gid)
    intervals += await _exclusion_intervals(db, gid)
    intervals.sort(key=lambda x: x[0])
    uncovered: list[list[int]] = []
    if not intervals:
        uncovered = [[0, len(source)]]
    else:
        cursor = 0
        for start, end, _k in intervals:
            if start > cursor:
                uncovered.append([cursor, start])
            cursor = max(cursor, end)
        if cursor < len(source):
            uncovered.append([cursor, len(source)])
    cover_ok = not uncovered
    report["checks"]["interval_cover"] = {
        "ok": cover_ok,
        "intervals": len(intervals),
        "uncovered": uncovered,
    }
    if not cover_ok:
        report["failed"].append(f"interval_cover: 未覆盖区间 {uncovered}")

    # 2. 段哈希与原文切片一致
    bad_segments: list[str] = []
    for start, end, stored_hash in intervals:
        if _sha256(source[start:end]) != stored_hash:
            bad_segments.append(f"[{start},{end})")
    segment_ok = not bad_segments
    report["checks"]["segment_hash"] = {
        "ok": segment_ok,
        "bad_segments": bad_segments,
    }
    if not segment_ok:
        report["failed"].append(f"segment_hash: {len(bad_segments)} 段内容与原文不一致")

    # 3. 全文重组哈希 == 原文哈希
    rebuilt = "".join(source[s:e] for s, e, _k in intervals)
    full_ok = _sha256(rebuilt) == _sha256(source) and _sha256(source) == gen[1]
    report["checks"]["fulltext_hash"] = {"ok": full_ok, "source_content_hash": gen[1]}
    if not full_ok:
        report["failed"].append("fulltext_hash: 区间重组哈希 != 原文哈希")

    # 4. 地标（读库中数据，不重新解析）
    landmark_checks = []
    row613 = (
        await db.execute(
            select(ProjectSourceChapter.content)
            .where(ProjectSourceChapter.split_generation_id == gid)
            .where(ProjectSourceChapter.display_chapter_no == 613)
            .limit(1)
        )
    ).scalars().first()
    ok613 = row613 is not None and "铁笼" in row613
    landmark_checks.append({"name": "铁笼→613", "pass": ok613,
                            "detail": "命中" if ok613 else "缺失/不含关键词"})
    row3195 = (
        await db.execute(
            select(ProjectSourceChapter.content)
            .where(ProjectSourceChapter.split_generation_id == gid)
            .where(ProjectSourceChapter.display_chapter_no == 3195)
            .limit(1)
        )
    ).scalars().first()
    ok3195 = row3195 is not None and "梵葵" in row3195
    landmark_checks.append({"name": "梵葵陷阱→3195", "pass": ok3195,
                            "detail": "命中" if ok3195 else "缺失/不含关键词"})
    last_row = (
        await db.execute(
            select(ProjectSourceChapter.display_chapter_no,
                   ProjectSourceChapter.original_label)
            .where(ProjectSourceChapter.split_generation_id == gid)
            .order_by(ProjectSourceChapter.source_ordinal.desc())
            .limit(1)
        )
    ).one_or_none()
    ok3233 = last_row is not None and last_row[0] == 3233
    landmark_checks.append({"name": "末章→3233(末位)", "pass": ok3233,
                            "detail": last_row[1] if last_row else "无章节"})
    report["checks"]["landmarks"] = landmark_checks
    for c in landmark_checks:
        if not c["pass"]:
            report["failed"].append(f"landmarks.{c['name']}")

    # 5. display 无回跳（keep_duplicate 组内相等允许）
    rows = (
        await db.execute(
            select(ProjectSourceChapter.source_ordinal,
                   ProjectSourceChapter.display_chapter_no)
            .where(ProjectSourceChapter.split_generation_id == gid)
            .where(ProjectSourceChapter.display_chapter_no.is_not(None))
            .order_by(ProjectSourceChapter.source_ordinal)
        )
    ).all()
    regressions = []
    for i in range(1, len(rows)):
        if rows[i][1] < rows[i - 1][1]:
            regressions.append(
                f"ord {rows[i][0]} display {rows[i][1]} < 前章 {rows[i - 1][1]}"
            )
    regression_ok = not regressions
    report["checks"]["no_regression"] = {"ok": regression_ok, "regressions": regressions}
    if not regression_ok:
        report["failed"].append(f"no_regression: {regressions}")

    # 6. 重复组数 == 账本 keep_duplicate 数
    dup_groups: dict[int, list] = {}
    for r in rows:
        dup_groups.setdefault(r[1], []).append(r[0])
    dup_groups = {k: v for k, v in dup_groups.items() if len(v) > 1}
    gen_dup_count = len(dup_groups)
    ledger_dup_count = 0
    gen_row = (
        await db.execute(
            select(SplitGeneration.decision_ledger).where(SplitGeneration.id == gid)
        )
    ).scalar_one_or_none()
    if isinstance(gen_row, dict):
        try:
            ledger_dup_count = len(gen_row["decisions"]["keep_duplicate"])
        except (KeyError, TypeError):
            ledger_dup_count = 0
    dup_ok = gen_dup_count == ledger_dup_count
    report["checks"]["duplicate_groups_match"] = {
        "ok": dup_ok,
        "in_db": gen_dup_count,
        "in_ledger": ledger_dup_count,
        "groups": {str(k): v for k, v in sorted(dup_groups.items())},
    }
    if not dup_ok:
        report["failed"].append(
            f"duplicate_groups_match: 库 {gen_dup_count} != 账本 {ledger_dup_count}"
        )

    if report["failed"]:
        report["verdict"] = "FAIL"
    return report


# ── CAS 切换（激活 / 回滚共用） ───────────────────────────

async def _cas_switch(
    db: AsyncSession,
    project_id,
    source_id,
    target_generation_id,
    expected_current: uuid.UUID | str | None,
    require_ready: bool,
    reason: str,
) -> dict:
    """单事务条件更新切换活动指针。

    UPDATE source_split_state SET active=:new
    WHERE project_id/source_id 匹配 AND active = :expected —— 必须传旧代，
    不能只用 IS NULL（两个 ready 代并发时只有先到者生效，后者 rowcount=0 重试）。
    调用方负责 commit；rowcount != 1 时抛 SplitGenerationError（未修改任何行）。
    """
    pid, sid = _as_uuid(project_id), _as_uuid(source_id)
    target_id = _as_uuid(target_generation_id)

    target = (
        await db.execute(
            select(SplitGeneration)
            .where(SplitGeneration.id == target_id)
            .where(SplitGeneration.project_id == pid)
            .where(SplitGeneration.source_id == sid)
        )
    ).scalar_one_or_none()
    if target is None:
        raise SplitGenerationError(f"目标代不存在：{target_generation_id}")
    if require_ready and target.status != STATUS_READY:
        raise SplitGenerationError(
            f"目标代状态 {target.status} 不是 ready，禁止激活"
        )
    if not require_ready and target.status not in (
        STATUS_LEGACY, STATUS_SUPERSEDED, STATUS_READY,
    ):
        raise SplitGenerationError(
            f"回滚目标状态 {target.status} 不可用（legacy/superseded/ready）"
        )

    state = (
        await db.execute(
            select(SourceSplitState)
            .where(SourceSplitState.project_id == pid)
            .where(SourceSplitState.source_id == sid)
        )
    ).scalar_one_or_none()
    if state is None:
        raise SplitGenerationError(
            f"该 source 尚无活动指针（先执行 ensure_legacy_generation）"
        )
    current = state.active_split_generation_id
    if expected_current is not None:
        try:
            expected_uuid = _as_uuid(expected_current)
        except (ValueError, AttributeError, TypeError):
            raise SplitGenerationError(
                f"并发冲突：期望旧代 {expected_current} 非法，实际 {current}（重试）"
            )
        if current != expected_uuid:
            raise SplitGenerationError(
                f"并发冲突：期望旧代 {expected_uuid}，实际 {current}（重试）"
            )

    # ── 激活门槛补强：重新读取原文，复核 target 的 source_content_hash 未漂移 ──
    source = (
        await db.execute(select(ProjectSource.content).where(ProjectSource.id == sid))
    ).scalar_one_or_none()
    current_hash = _sha256(source or "")
    if current_hash != target.source_content_hash:
        raise SplitGenerationError(
            "激活前原文哈希复核失败：构建时 "
            f"{target.source_content_hash[:16]}… vs 当前 {current_hash[:16]}…"
            "（原文在构建后被修改，禁止激活；需重新构建该代）"
        )

    # CAS 条件更新（expected 为 None 时匹配 IS NULL）
    if expected_current is None:
        result = await db.execute(
            update(SourceSplitState)
            .where(SourceSplitState.project_id == pid)
            .where(SourceSplitState.source_id == sid)
            .where(SourceSplitState.active_split_generation_id.is_(None))
            .values(active_split_generation_id=target_id)
        )
    else:
        result = await db.execute(
            update(SourceSplitState)
            .where(SourceSplitState.project_id == pid)
            .where(SourceSplitState.source_id == sid)
            .where(SourceSplitState.active_split_generation_id == _as_uuid(expected_current))
            .values(active_split_generation_id=target_id)
        )
    if result.rowcount != 1:
        raise SplitGenerationError(
            f"CAS 切换失败（rowcount={result.rowcount}）：活动指针已被其他代占用"
        )

    # 旧活动代标记 superseded（legacy 保持 legacy，不参与竞态）
    if current and current != target_id:
        await db.execute(
            update(SplitGeneration)
            .where(SplitGeneration.id == current)
            .where(SplitGeneration.status == STATUS_ACTIVE)
            .values(status=STATUS_SUPERSEDED, updated_at=_now())
        )
    # 新代激活（legacy 占位代保持 legacy 状态）
    if target.status != STATUS_LEGACY:
        await db.execute(
            update(SplitGeneration)
            .where(SplitGeneration.id == target_id)
            .values(status=STATUS_ACTIVE, activated_at=_now(), updated_at=_now())
        )
    await db.flush()

    return {
        "generation_id": target_id,
        "reason": reason,
        "switched_from": current,
        "switched_to": target_id,
    }


async def activate_generation(
    db: AsyncSession,
    project_id,
    source_id,
    generation_id,
    expected_current=None,
) -> dict:
    """激活 ready 代（CAS 单事务）。expected_current 必须传旧代 id 或 None。"""
    return await _cas_switch(db, project_id, source_id, generation_id,
                             expected_current, require_ready=True, reason="activate")


async def rollback_generation(
    db: AsyncSession,
    project_id,
    source_id,
    target_generation_id,
    expected_current=None,
) -> dict:
    """回滚到目标代（legacy/superseded/ready，CAS 单事务）。"""
    return await _cas_switch(db, project_id, source_id, target_generation_id,
                             expected_current, require_ready=False, reason="rollback")
