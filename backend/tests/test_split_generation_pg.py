"""切分代次 PG 集成测试（真实 PostgreSQL，RUN_PG_TESTS=1 才执行）。

覆盖用户阻塞点 4/5 的验证：
  1. alembic 033/034 在真实 PG 上执行（UUID 类型列、FK、级联、唯一约束）；
  2. async 双后端服务全链路（legacy 回填 → 构建 → 激活 → 回滚）在 PG 上通过；
  3. FK 级联：删除 source → split_generations / source_split_state /
     split_exclusions 级联清理。

用法：
  RUN_PG_TESTS=1 venv/bin/python -m pytest tests/test_split_generation_pg.py -q
"""

import asyncio
import hashlib
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.extraction_pipeline import ExtractionJob, ProjectSourceChapter
from models.project_source import ProjectSource
from models.split_generation import (
    SourceSplitState, SplitExclusion, SplitGeneration,
)
from services.split_generation_service import (
    activate_generation,
    build_new_generation,
    ensure_legacy_generation,
    get_active_generation,
    get_chapters_for_generation,
    rollback_generation,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1",
    reason="PG 集成测试需 RUN_PG_TESTS=1（真实 PostgreSQL）",
)

PG_HOST = "localhost"
PG_PORT = "5432"
DB_NAME = f"ai_creative_split_test_{uuid.uuid4().hex[:8]}"
PG_URL = f"postgresql+asyncpg://{PG_HOST}:{PG_PORT}/{DB_NAME}"

# asyncpg 连接池绑定创建时的 event loop：模块级单 loop，避免跨 loop 使用。
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PREFACE = "这是站点开头的说明文字，占位内容填充至四十一个字符为止。"
CHAPTERS = [
    ("第613章 铁笼囚牢", "正文：主角被困铁笼之中，生死一线。"),
    ("第614章 破笼而出", "正文：雷印绽放，笼门碎裂。"),
    ("第3195章 梵葵陷阱", "正文：梵葵花海之下暗藏杀机。"),
    ("第3232章 风暴前夜", "正文：大战前的最后宁静。"),
    ("第3233章 人间，惹不起了", "正文：全书终章，尘埃落定。"),
]


def make_source() -> str:
    body = "".join(f"{label}\n{body}\n" for label, body in CHAPTERS)
    return PREFACE + "\n" + body


def make_ledger(source: str) -> dict:
    from services.split_generation_service import _recompute_decisions_hash
    ledger = {
        "schema_version": 1,
        "source_content_hash": _sha256(source),
        "decisions": {"drop": [], "renumber": [], "keep_duplicate": [], "unresolved": []},
        "gap_resolution": {"author_numbering_skip": [], "format_recovered": [],
                           "accepted_missing_in_supplied_source": {"count": 0}},
        "ready_for_write": True,
        "resolved_verdict": "PASS",
    }
    ledger["decisions_hash"] = _recompute_decisions_hash(ledger)
    return ledger


@pytest.fixture(scope="module")
def pg_engine():
    """创建测试库 → 真实跑 alembic 033/034 → 提供 async engine（模块级单 loop）。"""
    # 建库（已存在则忽略）
    subprocess.run(["createdb", "-h", PG_HOST, "-p", PG_PORT, DB_NAME],
                   check=False, capture_output=True)

    # alembic upgrade head（PG 上真实执行 033 + 034）。
    # env.py 会覆盖 sqlalchemy.url 为 settings.DATABASE_URL → 先改 settings 实例
    # （frozen dataclass，用 object.__setattr__ 绕过）。
    from config.settings import settings as app_settings
    object.__setattr__(app_settings, "DATABASE_URL", PG_URL)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_async_engine(PG_URL)
    yield engine

    _run(engine.dispose())
    subprocess.run(["dropdb", "--if-exists", "-h", PG_HOST, "-p", PG_PORT, DB_NAME],
                   check=False, capture_output=True)
    _LOOP.close()


@pytest.fixture(scope="module")
def factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="module")
def ids():
    return {"project_id": uuid.uuid4(), "source_id": uuid.uuid4()}


def _run_factory(factory, fn, *args, **kwargs):
    async def _go():
        async with factory() as session:
            result = await fn(session, *args, **kwargs)
            await session.commit()
            return result
    return _run(_go())


def _seed(factory, ids):
    source = make_source()
    async def _go():
        async with factory() as session:
            await session.execute(
                text("INSERT INTO project_sources (id, project_id, title, source_type, "
                     "content, always_inject, chunk_count, token_count) "
                     "VALUES (:id, :pid, 'PG测试源', 'upload', :content, false, 0, 0)"),
                {"id": str(ids["source_id"]), "pid": str(ids["project_id"]),
                 "content": source},
            )
            await session.commit()
    _run(_go())
    return source


# ── 1. 迁移与表结构 ─────────────────────────────────────

def test_pg_migration_schema_and_fks(pg_engine, ids):
    async def _check():
        async with pg_engine.connect() as conn:
            # 三张表 + 列
            for tbl in ("split_generations", "source_split_state", "split_exclusions"):
                exists = (await conn.execute(text(
                    "SELECT to_regclass('public." + tbl + "')"))).scalar()
                assert exists is not None, f"表 {tbl} 不存在"
            chapter_cols = {
                r[0] for r in (await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='project_source_chapters'"
                ))).fetchall()
            }
            for col in ("split_generation_id", "source_ordinal",
                        "display_chapter_no", "original_label",
                        "start_offset", "end_offset", "content_hash"):
                assert col in chapter_cols, f"章节表缺列 {col}"
            job_cols = {
                r[0] for r in (await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='extraction_jobs'"
                ))).fetchall()
            }
            assert "split_generation_id" in job_cols
            # 列类型：PG 上代键为 UUID
            gen_type = (await conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='split_generations' AND column_name='id'"
            ))).scalar()
            assert gen_type == "uuid"
            # FK 存在
            fks = {
                r[0] for r in (await conn.execute(text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type='FOREIGN KEY' AND table_name IN "
                    "('split_generations','source_split_state','split_exclusions',"
                    "'project_source_chapters','extraction_jobs')"
                ))).fetchall()
            }
            for fk in ("fk_split_generations_source", "fk_source_split_state_source",
                       "fk_source_split_state_active", "fk_split_exclusions_generation",
                       "fk_source_chapters_generation", "fk_extraction_jobs_split_generation"):
                assert fk in fks, f"缺外键 {fk}"
            # 唯一约束
            uqs = {
                r[0] for r in (await conn.execute(text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type='UNIQUE' AND table_name='project_source_chapters'"
                ))).fetchall()
            }
            assert "uq_source_chapters_generation_ordinal" in uqs
    _run(_check())


# ── 2. 全链路（PG 双后端） ──────────────────────────────

def test_pg_full_cycle(factory, ids):
    source = _seed(factory, ids)

    # legacy 回填（含幂等）
    legacy_id = _run_factory(factory, ensure_legacy_generation,
                     ids["project_id"], ids["source_id"])
    again = _run_factory(factory, ensure_legacy_generation,
                 ids["project_id"], ids["source_id"])
    assert legacy_id == again

    # 构建新代
    ledger = make_ledger(source)
    result = _run_factory(factory, build_new_generation,
                  ids["project_id"], ids["source_id"], ledger)
    assert result["status"] == "ready"
    assert result["kept_chapters"] == len(CHAPTERS)
    assert result["validation_report"]["verdict"] == "PASS"
    assert result["validation_report"]["checks"]["landmarks"][0]["pass"]
    assert result["validation_report"]["checks"]["landmarks"][2]["pass"]

    # 激活（CAS）
    act = _run_factory(factory, activate_generation,
               ids["project_id"], ids["source_id"], result["id"],
               expected_current=legacy_id)
    assert act["switched_to"] == result["id"]

    async def _active_check():
        async with factory() as session:
            active = await get_active_generation(session, ids["project_id"], ids["source_id"])
            assert active.id == result["id"]
            rows = await get_chapters_for_generation(
                session, ids["project_id"], ids["source_id"], result["id"])
            assert [r.display_chapter_no for r in rows] == [613, 614, 3195, 3232, 3233]
    _run(_active_check())

    # 回滚
    rb = _run_factory(factory, rollback_generation,
              ids["project_id"], ids["source_id"], legacy_id,
              expected_current=result["id"])
    assert rb["switched_to"] == legacy_id
    async def _rollback_check():
        async with factory() as session:
            active = await get_active_generation(session, ids["project_id"], ids["source_id"])
            assert active.id == legacy_id
            gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == result["id"])
            )).scalar_one()
            assert gen.status == "superseded"
            legacy_gen = (await session.execute(
                select(SplitGeneration).where(SplitGeneration.id == legacy_id)
            )).scalar_one()
            assert legacy_gen.status == "legacy"
    _run(_rollback_check())


# ── 3. FK 级联删除（source 删除） ───────────────────────

def test_pg_cascade_delete_source(factory, ids):
    """删除 source → 代 / 指针 / exclusions 级联清理（FK CASCADE 生效）。"""
    async def _delete_source():
        async with factory() as session:
            # 手动清章节（routes 同款逻辑；FK 之外的第二道保险）
            await session.execute(delete(ProjectSourceChapter).where(
                ProjectSourceChapter.source_id == ids["source_id"]))
            await session.execute(delete(ExtractionJob).where(
                ExtractionJob.source_id == ids["source_id"]))
            # 删除 source：FK CASCADE 应清掉 split_generations / state / exclusions
            await session.execute(delete(ProjectSource).where(
                ProjectSource.id == ids["source_id"]))
            await session.commit()
    _run(_delete_source())

    async def _verify_empty():
        async with factory() as session:
            gens = (await session.execute(select(SplitGeneration).where(
                SplitGeneration.source_id == ids["source_id"]))).scalars().all()
            states = (await session.execute(select(SourceSplitState).where(
                SourceSplitState.source_id == ids["source_id"]))).scalars().all()
            excls = (await session.execute(select(SplitExclusion).where(
                SplitExclusion.split_generation_id.in_(
                    select(SplitGeneration.id).where(
                        SplitGeneration.source_id == ids["source_id"])
                )))).scalars().all()
            assert gens == []
            assert states == []
            assert excls == []
    _run(_verify_empty())
