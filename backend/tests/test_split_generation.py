"""切分代次服务测试（P-1 阶段四：legacy 迁移 / 新代构建 / 完整性验收 / CAS 激活 / 回滚）。

async SQLAlchemy 双后端版本。覆盖方案 v3 §3.5 的写库链路：
  1. legacy 回填幂等 + source_ordinal=旧 chapter_no 语义；
  2. 新代构建：keep/renumber/keep_duplicate/drop 决议落库，preface 排除；
  3. 写库门槛：ready_for_write / decisions_hash 重算验证 / 原文哈希 / ordinal 合法性；
  4. 完整性验收：区间覆盖 + 段哈希 + 全文哈希 + 地标 + 无回跳 + 重复组匹配；
  5. CAS 激活：并发冲突（旧代不匹配）拒绝、非 ready 拒绝、原文哈希复核；
  6. 回滚：激活后回滚到 legacy，读取恢复旧语义。
"""

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.base import Base
import models  # noqa: F401  — ensure all models registered
from models.extraction_pipeline import ProjectSourceChapter
from models.project_source import ProjectSource
from models.split_generation import SplitGeneration
from services.split_generation_service import (
    SplitGenerationError,
    activate_generation,
    build_new_generation,
    count_active_chapters,
    ensure_legacy_generation,
    get_active_chapters,
    get_active_generation,
    get_active_generation_id,
    get_chapters_for_generation,
    list_generations,
    rollback_generation,
    validate_generation,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_ledger(source: str, decisions: dict | None = None, **overrides) -> dict:
    """构造满足硬门槛的决策账本（第 0 步产物形状，decisions_hash 按规范重算）。"""
    from services.split_generation_service import _recompute_decisions_hash
    ledger = {
        "schema_version": 1,
        "source_content_hash": _sha256(source),
        "decisions": decisions or {
            "drop": [], "renumber": [], "keep_duplicate": [], "unresolved": [],
        },
        "gap_resolution": {"author_numbering_skip": [], "format_recovered": [],
                           "accepted_missing_in_supplied_source": {"count": 0}},
        "ready_for_write": True,
        "resolved_verdict": "PASS",
    }
    ledger["decisions_hash"] = _recompute_decisions_hash(ledger)
    ledger.update(overrides)
    if "decisions_hash" not in overrides:
        # overrides 可能改了 decisions → 重算；显式传入的 decisions_hash 除外
        ledger["decisions_hash"] = _recompute_decisions_hash(ledger)
    return ledger


PREFACE = "这是站点开头的说明文字，占位内容填充至四十一个字符为止。"
CHAPTERS = [
    ("第613章 铁笼囚牢", "正文：主角被困铁笼之中，生死一线。"),
    ("第614章 破笼而出", "正文：雷印绽放，笼门碎裂。"),
    ("第3195章 梵葵陷阱", "正文：梵葵花海之下暗藏杀机。"),
    ("第3232章 风暴前夜", "正文：大战前的最后宁静。"),
    ("第3233章 人间，惹不起了", "正文：全书终章，尘埃落定。"),
]


def make_source(chapters=CHAPTERS, preface=PREFACE) -> str:
    body = "".join(f"{label}\n{body}\n" for label, body in chapters)
    return (preface + "\n" if preface else "") + body


@pytest.fixture
def db(tmp_path):
    """临时文件 SQLite + AsyncSession（每测试独立库）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/split_test.sqlite3")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source = make_source()

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(ProjectSource(
                id=source_id, project_id=project_id, title="测试源",
                source_type="upload", content=source,
            ))
            await session.commit()

    asyncio.run(_seed())

    yield {
        "factory": factory,
        "project_id": project_id,
        "source_id": source_id,
        "source": source,
        "tmp": tmp_path,
    }
    asyncio.run(engine.dispose())


def seed_legacy_rows(factory, project_id, source_id, count=2):
    """模拟旧 splitter 切分的历史章节行（chapter_no = 顺序号）。"""
    async def _seed():
        async with factory() as session:
            for i in range(1, count + 1):
                session.add(ProjectSourceChapter(
                    id=uuid.uuid4(), project_id=project_id, source_id=source_id,
                    chapter_no=i, chapter_title=f"旧标题{i}", content=f"旧内容{i}",
                    split_type="chapter_regex",
                ))
            await session.commit()
    asyncio.run(_seed())


def _build(factory, project_id, source_id, source=None, ledger=None, new_source=None):
    """构建新代（同步包装；new_source 传入时先同步更新库内原文）。"""
    async def _go():
        async with factory() as session:
            if new_source is not None:
                src = (await session.execute(
                    select(ProjectSource).where(ProjectSource.id == source_id)
                )).scalar_one()
                src.content = new_source
                await session.commit()
            src = (await session.execute(
                select(ProjectSource.content).where(ProjectSource.id == source_id)
            )).scalar_one()
            effective_ledger = ledger or make_ledger(src)
            result = await build_new_generation(
                session, project_id, source_id, effective_ledger)
            await session.commit()
            return result
    return asyncio.run(_go())


def _run_with(factory, fn, *args, commit=True, **kwargs):
    """在独立 session 中执行服务函数（同步包装，默认提交）。"""
    async def _go():
        async with factory() as session:
            result = await fn(session, *args, **kwargs)
            if commit:
                await session.commit()
            return result
    return asyncio.run(_go())


def _activate(factory, project_id, source_id, generation_id, expected=None):
    async def _go():
        async with factory() as session:
            result = await activate_generation(
                session, project_id, source_id, generation_id, expected)
            await session.commit()
            return result
    return asyncio.run(_go())


def _rollback(factory, project_id, source_id, target, expected=None):
    async def _go():
        async with factory() as session:
            result = await rollback_generation(
                session, project_id, source_id, target, expected)
            await session.commit()
            return result
    return asyncio.run(_go())


# ── 1. legacy 迁移 ──────────────────────────────────────

def test_legacy_backfill_idempotent(db):
    f = db["factory"]
    pid, sid = db["project_id"], db["source_id"]
    seed_legacy_rows(f, pid, sid)
    g1 = _run_with(f, ensure_legacy_generation, pid, sid)
    g2 = _run_with(f, ensure_legacy_generation, pid, sid)
    assert g1 == g2
    async def _check():
        async with f() as session:
            null_rows = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id.is_(None))
            )).scalars().all()
            assert len(null_rows) == 0
            active = await get_active_generation(session, pid, sid)
            assert active.id == g1
    asyncio.run(_check())


def test_legacy_backfill_maps_chapter_no(db):
    f = db["factory"]
    pid, sid = db["project_id"], db["source_id"]
    seed_legacy_rows(f, pid, sid, count=3)
    gid = _run_with(f, ensure_legacy_generation, pid, sid)
    async def _check():
        async with f() as session:
            rows = await get_chapters_for_generation(session, pid, sid, gid)
            assert [r.chapter_no for r in rows] == [1, 2, 3]
            assert all(r.display_chapter_no is None for r in rows)
            assert [r.original_label for r in rows] == ["旧标题1", "旧标题2", "旧标题3"]
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == gid)
            )).scalar_one()
            assert gen.parser_version == "legacy"
            assert gen.status == "legacy"
            assert gen.decisions_hash is None
    asyncio.run(_check())


# ── 2. 新代构建 ─────────────────────────────────────────

def test_build_new_generation_ready(db):
    f, pid, sid, source = db["factory"], db["project_id"], db["source_id"], db["source"]
    result = _build(f, pid, sid)
    assert result["status"] == "ready"
    assert result["kept_chapters"] == len(CHAPTERS)
    report = result["validation_report"]
    assert report["verdict"] == "PASS"
    assert report["checks"]["interval_cover"]["ok"]
    assert report["checks"]["fulltext_hash"]["ok"]

    async def _check():
        async with f() as session:
            rows = await get_chapters_for_generation(session, pid, sid, result["id"])
            assert [r.display_chapter_no for r in rows] == [613, 614, 3195, 3232, 3233]
            assert [r.source_ordinal for r in rows] == [1, 2, 3, 4, 5]
            assert all(r.start_offset is not None and r.end_offset is not None
                       for r in rows)
            assert all(r.content_hash == _sha256(r.content) for r in rows)
            assert all(r.chapter_no == r.source_ordinal for r in rows)
            excls = (await session.execute(
                select(models.split_generation.SplitExclusion)
                .where(models.split_generation.SplitExclusion.split_generation_id == result["id"])
            )).scalars().all()
            assert any(e.kind == "preface" and e.start_offset == 0
                       and e.end_offset == len(PREFACE) + 1 for e in excls)
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == result["id"])
            )).scalar_one()
            assert gen.source_content_hash == _sha256(source)
            assert gen.decisions_hash == result["decisions_hash"]
            assert gen.parser_version
    asyncio.run(_check())


def test_build_rejects_unready_ledger(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    ledger = make_ledger(db["source"], ready_for_write=False,
                         resolved_verdict="FAIL：门槛未满足 ['unresolved_zero']")
    with pytest.raises(SplitGenerationError, match="ready_for_write"):
        _build(f, pid, sid, ledger=ledger)
    async def _check():
        async with f() as session:
            assert (await session.execute(select(SplitGeneration))).scalars().all() == []
    asyncio.run(_check())


def test_build_rejects_tampered_decisions_hash(db):
    """decisions_hash 被篡改 → 重算验证拒绝（不写任何行）。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    ledger = make_ledger(db["source"])
    ledger["decisions_hash"] = "f" * 64  # 伪造
    with pytest.raises(SplitGenerationError, match="重算不一致"):
        _build(f, pid, sid, ledger=ledger)


def test_build_rejects_hash_mismatch(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    ledger = make_ledger(db["source"] + "改过的原文")
    with pytest.raises(SplitGenerationError, match="原文哈希不一致"):
        _build(f, pid, sid, ledger=ledger)


def test_build_rejects_unknown_ordinal(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    ledger = make_ledger(db["source"], decisions={
        "drop": [{"ordinal": 999, "display_no": 1, "label": "x", "reason": "y"}],
        "renumber": [], "keep_duplicate": [], "unresolved": [],
    })
    with pytest.raises(SplitGenerationError, match="不存在的 ordinal"):
        _build(f, pid, sid, ledger=ledger)


def test_build_applies_renumber(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    ledger = make_ledger(db["source"], decisions={
        "drop": [], "keep_duplicate": [],
        "renumber": [{"ordinal": 2, "display_no": 614, "label": "第614章",
                      "to": 615, "reason": "测试"}],
        "unresolved": [],
    })
    result = _build(f, pid, sid, ledger=ledger)
    async def _check():
        async with f() as session:
            rows = await get_chapters_for_generation(session, pid, sid, result["id"])
            assert [r.display_chapter_no for r in rows] == [613, 615, 3195, 3232, 3233]
            assert result["validation_report"]["verdict"] == "PASS"
    asyncio.run(_check())


def test_build_keep_duplicate_and_drop(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    dup_source = make_source(chapters=[
        ("第1章 起点", "内容1\n"),
        ("第613章 铁笼囚牢", "正文：铁笼之中，生死一线。\n"),
        ("第614章 破笼而出", "内容3\n"),
        ("第3195章 梵葵陷阱", "正文：梵葵花海之下。\n"),
        ("第3195章 梵葵再临", "内容5\n"),  # keep_duplicate（display 3195 重复）
        ("第3233章 人间，惹不起了", "内容6\n"),
        ("第一章：中午12点", "更新说明：晚上更新\n"),  # update_note → drop
    ], preface="")
    ledger = make_ledger(dup_source, decisions={
        "drop": [{"ordinal": 7, "display_no": 1, "label": "第一章：中午12点",
                  "reason": "伪标题/噪音"}],
        "keep_duplicate": [{"ordinal": 5, "display_no": 3195, "label": "第3195章 梵葵再临",
                            "reason": "重复号待裁决"}],
        "renumber": [], "unresolved": [],
    })
    result = _build(f, pid, sid, new_source=dup_source, ledger=ledger)
    assert result["status"] == "ready"
    assert result["kept_chapters"] == 6
    async def _check():
        async with f() as session:
            rows = await get_chapters_for_generation(session, pid, sid, result["id"])
            assert [r.display_chapter_no for r in rows] == [1, 613, 614, 3195, 3195, 3233]
            ordinals = [r.source_ordinal for r in rows]
            assert len(set(ordinals)) == len(ordinals)
            excls = (await session.execute(
                select(models.split_generation.SplitExclusion)
                .where(models.split_generation.SplitExclusion.split_generation_id == result["id"])
            )).scalars().all()
            assert any(e.kind == "noise" and e.ordinal_ref == 7 for e in excls)
            report = result["validation_report"]
            assert report["checks"]["duplicate_groups_match"]["ok"]
            assert report["checks"]["interval_cover"]["ok"]
    asyncio.run(_check())


def test_build_failed_generation_kept(db):
    """地标缺失 → 验收 FAIL → status=failed（数据保留，不激活）。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    bad_source = make_source(chapters=[
        ("第1章 开局", "正文A\n"),
        ("第2章 发展", "正文B\n"),
        ("第3章 终局", "正文C\n"),
    ])
    result = _build(f, pid, sid, new_source=bad_source)
    assert result["status"] == "failed"
    assert result["validation_report"]["verdict"] == "FAIL"
    assert any("landmarks" in x for x in result["validation_report"]["failed"])
    async def _check():
        async with f() as session:
            gens = await list_generations(session, pid, sid)
            assert len(gens) == 1
            with pytest.raises(SplitGenerationError, match="不是 ready"):
                await activate_generation(session, pid, sid, result["id"])
    asyncio.run(_check())


# ── 3. 完整性验收 ───────────────────────────────────────

def test_validate_fulltext_integrity(db):
    f, pid, sid, source = db["factory"], db["project_id"], db["source_id"], db["source"]
    result = _build(f, pid, sid)
    async def _check():
        async with f() as session:
            report = await validate_generation(session, result["id"], source)
            assert report["verdict"] == "PASS"
            assert report["checks"]["interval_cover"]["uncovered"] == []
            assert report["checks"]["landmarks"][0]["pass"]
            assert report["checks"]["landmarks"][1]["pass"]
            assert report["checks"]["landmarks"][2]["pass"]
            assert report["checks"]["no_regression"]["ok"]
            assert report["checks"]["duplicate_groups_match"]["ok"]
    asyncio.run(_check())


# ── 4. CAS 激活 ─────────────────────────────────────────

def test_activate_cas_success(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    result = _activate(f, pid, sid, new["id"], expected=legacy_id)
    assert result["switched_from"] == legacy_id
    assert result["switched_to"] == new["id"]
    async def _check():
        async with f() as session:
            active = await get_active_generation(session, pid, sid)
            assert active.id == new["id"]
            assert active.status == "active"
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == legacy_id)
            )).scalar_one()
            assert gen.status == "legacy"  # 占位代保持 legacy
    asyncio.run(_check())


def test_activate_requires_ready(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    async def _check():
        async with f() as session:
            new_gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == new["id"])
            )).scalar_one()
            new_gen.status = "building"
            await session.commit()
            with pytest.raises(SplitGenerationError, match="不是 ready"):
                await activate_generation(session, pid, sid, new["id"])
    asyncio.run(_check())


def test_activate_rejects_source_hash_drift(db):
    """构建后原文被修改 → 激活时原文哈希复核拒绝。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    async def _check():
        async with f() as session:
            src = (await session.execute(
                select(ProjectSource).where(ProjectSource.id == sid)
            )).scalar_one()
            src.content = src.content + "构建后追加的原文"
            await session.commit()
            with pytest.raises(SplitGenerationError, match="原文哈希复核失败"):
                await activate_generation(session, pid, sid, new["id"],
                                          expected_current=legacy_id)
    asyncio.run(_check())


def test_activate_concurrent_conflict(db):
    """两个 ready 代并发激活：先到者生效，后者（expect 旧代）被拒。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    a = _build(f, pid, sid)
    b = _build(f, pid, sid)
    ok = _activate(f, pid, sid, a["id"], expected=legacy_id)
    assert ok["switched_to"] == a["id"]
    with pytest.raises(SplitGenerationError, match="并发冲突|CAS 切换失败"):
        _activate(f, pid, sid, b["id"], expected=legacy_id)
    async def _check():
        async with f() as session:
            active = await get_active_generation(session, pid, sid)
            assert active.id == a["id"]
    asyncio.run(_check())


def test_activate_wrong_expect(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    with pytest.raises(SplitGenerationError, match="并发冲突"):
        _activate(f, pid, sid, new["id"], expected="some-other-generation")


def test_activate_without_pointer(db):
    """该 source 无活动指针（未执行 legacy 迁移）→ 拒绝。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    new = _build(f, pid, sid)
    with pytest.raises(SplitGenerationError, match="尚无活动指针"):
        _activate(f, pid, sid, new["id"])


# ── 5. 回滚 ─────────────────────────────────────────────

def test_rollback_to_legacy(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    _activate(f, pid, sid, new["id"], expected=legacy_id)
    result = _rollback(f, pid, sid, legacy_id, expected=new["id"])
    assert result["switched_to"] == legacy_id
    async def _check():
        async with f() as session:
            active = await get_active_generation(session, pid, sid)
            assert active.id == legacy_id
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == legacy_id)
            )).scalar_one()
            assert gen.status == "legacy"
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == new["id"])
            )).scalar_one()
            assert gen.status == "superseded"
            active_now, chapters = await get_active_chapters(session, pid, sid)
            assert active_now.id == legacy_id
            assert all(c.display_chapter_no is None for c in chapters)
            # 幂等：legacy 状态保持后，再次回填返回同一 legacy
            again = await ensure_legacy_generation(session, pid, sid)
            assert again == legacy_id
            gens = await list_generations(session, pid, sid)
            assert len([g for g in gens if g.status == "legacy"]) == 1
    asyncio.run(_check())


def test_rollback_rejects_unknown_target(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    _run_with(f, ensure_legacy_generation, pid, sid)
    with pytest.raises(SplitGenerationError, match="目标代不存在"):
        _rollback(f, pid, sid, uuid.uuid4())


def test_rollback_rejects_failed_target(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    _run_with(f, ensure_legacy_generation, pid, sid)
    bad_source = make_source(chapters=[("第1章 A", "x\n"), ("第2章 B", "y\n")])
    failed = _build(f, pid, sid, new_source=bad_source)
    assert failed["status"] == "failed"
    with pytest.raises(SplitGenerationError, match="不可用"):
        _rollback(f, pid, sid, failed["id"])


# ── 6. 读取（规则 7 统一入口） ───────────────────────────

def test_active_chapters_legacy_semantics(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    seed_legacy_rows(f, pid, sid, count=2)
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    async def _check():
        async with f() as session:
            active, chapters = await get_active_chapters(session, pid, sid)
            assert active.id == legacy_id
            assert [c.chapter_no for c in chapters] == [1, 2]
            assert [c.chapter_title for c in chapters] == ["旧标题1", "旧标题2"]
            assert await count_active_chapters(session, pid, sid) == 2
            assert await get_active_generation_id(session, pid, sid) == legacy_id
    asyncio.run(_check())


def test_active_chapters_after_activation(db):
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    seed_legacy_rows(f, pid, sid, count=2)
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    _activate(f, pid, sid, new["id"], expected=legacy_id)
    async def _check():
        async with f() as session:
            active, chapters = await get_active_chapters(session, pid, sid)
            assert active.id == new["id"]
            assert [c.display_chapter_no for c in chapters] == [613, 614, 3195, 3232, 3233]
            assert await count_active_chapters(session, pid, sid) == len(CHAPTERS)
            # 范围过滤
            _, ranged = await get_active_chapters(session, pid, sid,
                                                  chapter_no_start=2, chapter_no_end=3)
            assert [c.source_ordinal for c in ranged] == [2, 3]
            # legacy 行保留（旧代不删除）
            legacy_rows = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id == legacy_id)
            )).scalars().all()
            assert len(legacy_rows) == 2
    asyncio.run(_check())


# ── 7. 跨代回归（生产级问题修复验证） ────────────────────

from models.extraction_pipeline import ExtractionJob, ExtractionStaging  # noqa: E402
from services.chapter_splitter import split_source_chapters  # noqa: E402
from services.extraction_service import (  # noqa: E402
    _staging_keys_for_source,
    retry_extraction_chapter,
)


def _make_raw_json(chapter_no: int, character: str) -> dict:
    return {
        "chapter_no": chapter_no,
        "chapter_title": f"第{chapter_no}章",
        "characters": [{
            "name": character, "aliases": [], "identity": "测试",
            "status": "存活", "importance": 3, "confidence": 0.9,
            "evidence": f"第{chapter_no}章正文证据",
        }],
    }


def _seed_two_generations_with_staging(db, chapter_no=1):
    """构造 legacy + 新代同号章节、各带一条 job + staging。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    assert new["status"] == "ready"

    async def _seed():
        async with f() as session:
            # legacy 代章节（chapter_no=1 的旧行）
            session.add(ProjectSourceChapter(
                id=uuid.uuid4(), project_id=pid, source_id=sid,
                chapter_no=chapter_no, chapter_title="旧标题", content="旧内容",
                split_type="chapter_regex",
                split_generation_id=legacy_id, source_ordinal=chapter_no,
            ))
            legacy_job = ExtractionJob(
                project_id=pid, source_id=sid, genre="magic_fantasy",
                status="FAILED", provider="mock",
                split_generation_id=legacy_id,
            )
            new_job = ExtractionJob(
                project_id=pid, source_id=sid, genre="magic_fantasy",
                status="FAILED", provider="mock",
                split_generation_id=new["id"],
            )
            session.add_all([legacy_job, new_job])
            await session.flush()
            # 同 chapter_no 各一条 staging（raw_json 不同人物，updated_at 区分新旧）
            session.add_all([
                ExtractionStaging(
                    job_id=legacy_job.id, project_id=pid, source_id=sid,
                    chapter_no=chapter_no, chapter_title="旧",
                    genre="magic_fantasy", template_name="t", schema_version="1",
                    raw_output="", raw_json=_make_raw_json(chapter_no, "旧代人物"),
                    status="MERGED", provider="mock",
                    updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                ),
                ExtractionStaging(
                    job_id=new_job.id, project_id=pid, source_id=sid,
                    chapter_no=chapter_no, chapter_title="新",
                    genre="magic_fantasy", template_name="t", schema_version="1",
                    raw_output="", raw_json=_make_raw_json(chapter_no, "新代人物"),
                    status="MERGED", provider="mock",
                    updated_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
                ),
            ])
            await session.commit()
            return legacy_id, new["id"]
    return asyncio.run(_seed())


def test_staging_keys_isolated_by_generation(db):
    """跨代回归 1：新代任务的 staging 证据不得混入旧代 job 的记录。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id, new_id = _seed_two_generations_with_staging(db)

    async def _check():
        async with f() as session:
            new_keys = await _staging_keys_for_source(
                session, source_id=str(sid), genre="magic_fantasy",
                generation_id=new_id)
            assert "新代人物" in new_keys["characters"]
            assert "旧代人物" not in new_keys["characters"], \
                "新代证据混入了旧代 job 的 staging"

            legacy_keys = await _staging_keys_for_source(
                session, source_id=str(sid), genre="magic_fantasy",
                generation_id=legacy_id)
            assert "旧代人物" in legacy_keys["characters"]
            assert "新代人物" not in legacy_keys["characters"]

            # 未迁移语义（gen=None）：旧行为，全量可见
            all_keys = await _staging_keys_for_source(
                session, source_id=str(sid), genre="magic_fantasy",
                generation_id=None)
            assert "旧代人物" in all_keys["characters"]
            assert "新代人物" in all_keys["characters"]
    asyncio.run(_check())


def test_retry_deletes_only_own_generation_staging(db):
    """跨代回归 2：单章重试只删失败 staging 所属代（job）的记录。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    _seed_two_generations_with_staging(db)

    # 新代 staging 最新（updated_at 2021）→ owner = 新代 job
    async def _retry():
        async with f() as session:
            return await retry_extraction_chapter(
                session, str(pid), str(sid), chapter_no=1,
                user_id="tester", force_reextract=True)
    result = asyncio.run(_retry())

    async def _verify():
        async with f() as session:
            rows = (await session.execute(
                select(ExtractionStaging)
                .where(ExtractionStaging.source_id == str(sid))
                .where(ExtractionStaging.chapter_no == 1)
            )).scalars().all()
            jobs = {str(r.job_id) for r in rows if r.job_id}
            # 断言 1：旧代（legacy job）的记录未被误删（跨代误删 = 修复目标）
            def _first_char(r):
                chars = (r.raw_json or {}).get("characters") or []
                return chars[0].get("name") if chars else None

            legacy_rows = [r for r in rows if _first_char(r) == "旧代人物"]
            assert legacy_rows, "旧代 job 的 staging 被重试误删（跨代误删）"
            # 断言 2：所属代（新代 job）的原 staging 被重试重置删除（删除范围限定正确）
            assert not [r for r in rows if _first_char(r) == "新代人物"], \
                "所属代 staging 未被重试重置（删除范围未限定 job）"
            # 断言 3：重试推进产生了新的 staging（mock 抽取，非旧代人物）
            fresh = [r for r in rows if r.job_id not in
                     {str(s.job_id) for s in legacy_rows}]
            assert fresh, "重试后应有新的 staging 产生"


def test_splitter_creates_new_generation_not_mutate(db):
    """跨代回归 3：已验收代不可变——splitter 重切创建新代，旧代行零改动。"""
    f, pid, sid = db["factory"], db["project_id"], db["source_id"]
    legacy_id = _run_with(f, ensure_legacy_generation, pid, sid)
    new = _build(f, pid, sid)
    _activate(f, pid, sid, new["id"], expected=legacy_id)

    async def _snapshot():
        async with f() as session:
            gen_rows = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id == new["id"])
                .order_by(ProjectSourceChapter.source_ordinal)
            )).scalars().all()
            legacy_rows = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id == legacy_id)
            )).scalars().all()
            return ([(r.source_ordinal, r.display_chapter_no, r.content_hash)
                     for r in gen_rows],
                    len(legacy_rows),
                    [(r.source_ordinal, r.content) for r in legacy_rows])
    before = asyncio.run(_snapshot())

    async def _resplit():
        async with f() as session:
            return await split_source_chapters(session, pid, sid)
    result = asyncio.run(_resplit())
    assert result.chapter_count > 0

    async def _verify():
        async with f() as session:
            after_gen = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id == new["id"])
                .order_by(ProjectSourceChapter.source_ordinal)
            )).scalars().all()
            after_legacy = (await session.execute(
                select(ProjectSourceChapter)
                .where(ProjectSourceChapter.split_generation_id == legacy_id)
            )).scalars().all()
            # 已验收新代行零改动（数量/偏移/哈希不变）
            assert [(r.source_ordinal, r.display_chapter_no, r.content_hash)
                    for r in after_gen] == before[0]
            assert len(after_legacy) == before[1]
            # legacy 行零改动
            assert [(r.source_ordinal, r.content) for r in after_legacy] == before[2]
            # 新 splitter 代存在（parser=splitter, ready，未自动激活）
            splitter_gens = (await session.execute(
                select(SplitGeneration)
                .where(SplitGeneration.project_id == pid)
                .where(SplitGeneration.source_id == sid)
                .where(SplitGeneration.parser_version == "splitter")
            )).scalars().all()
            assert len(splitter_gens) == 1
            assert splitter_gens[0].status == "ready"
            # 活动代仍指向原新代（splitter 不自动切换）
            active = await get_active_generation(session, pid, sid)
            assert active.id == new["id"]
            # splitter 代可显式 CAS 激活（活动代不可变语义下提供显式切换路径）
            act = await activate_generation(
                session, pid, sid, splitter_gens[0].id, expected_current=new["id"])
            await session.commit()
            assert act["switched_to"] == splitter_gens[0].id
            active_after = await get_active_generation(session, pid, sid)
            assert active_after.id == splitter_gens[0].id
    asyncio.run(_verify())
