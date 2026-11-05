"""检索索引 outbox Worker 测试（阶段二，P0 闭环）。

SQLite 内存库覆盖 worker 全逻辑（同源合并策略 / 原子领取 / 超时回收 / 重试 /
stale 校验 / 运行入口），其中成功路径走真实索引构建（ProjectSourceChunk →
build_source_index → 双写 retrieval_chunks + FTS 表）。

显式设置 ``RUN_PG_TESTS=1`` 时，真实执行 PG 验收用例；默认始终跳过 PG 路径：
  1. 定稿 → outbox → Worker → active → 检索命中
  2. 连续定稿两次只命中新版（指针切换 + 旧块删除）
  3. v1 processing 时产生 v2（合并策略 → 新增 pending → v2 生效）
  4. 两个 Worker 并发不重复消费（无重复 generation）
  5. 崩溃后超时回收（processing + 超时 claimed_at → 重置 pending → 重新消费）
  6. 构建失败不留半成品（failed + attempts=1，无 active 指针、无可见块）
  7. stale 校验（job.source_version_id ≠ chapter.final_version_id → 不构建不切指针）

隔离：每用例独立 project_id，finally 清理本项目在 retrieval 四表 + job 表 +
业务种子表上的全部行，PG 库其他业务数据零接触。
"""

import asyncio
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.retrieval_index import (
    RetrievalIndexGeneration,
    RetrievalIndexSourceState,
)
from models.retrieval_index_job import RetrievalIndexJob
from models.structured_knowledge import (
    AbilityProfile,
    CharacterAliasCluster,
    CharacterProfile,
)
from models.world_entry import WorldEntry
from services.retrieval_indexer import (
    clear_indexer_cache,
    ensure_fts_table,
    request_chapter_index,
    request_source_index,
    request_structured_index,
)
from services.retrieval_tokenizer import clear_tokenizer_cache
import services.retrieval_index_worker as worker_mod
from services.retrieval_index_worker import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    enqueue_index_job,
    main as worker_main,
    poll_and_process_jobs,
    run_worker_loop,
)

TEST_PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
TEST_PG_PORT = os.environ.get("TEST_PG_PORT", "5432")
DB_NAME = f"ai_creative_retrieval_test_{uuid.uuid4().hex[:8]}"
PG_URL = f"postgresql+asyncpg://{TEST_PG_HOST}:{TEST_PG_PORT}/{DB_NAME}"

# 检索命中词（与 test_retrieval_retriever 同款种子，确保分词稳定）
_VOCAB_WORDS = ("莫凡", "雷系", "玫炎")

# 定稿章节正文（v1 含雷系 / v2 含火系，其余一致）
CONTENT_V1 = "第一章 觉醒\n莫凡觉醒了雷系，玫炎燃烧如炬，这是全书核心设定。"
CONTENT_V2 = "第一章 觉醒\n莫凡觉醒了火系，玫炎燃烧如炬，这是全书核心设定。"


pg_required = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1",
    reason="PG 集成测试需 RUN_PG_TESTS=1",
)


@pytest_asyncio.fixture
async def db():
    """独立的 SQLite 内存库会话（含 FTS5 虚拟表，业务表齐全）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_fts_table(conn)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例前后清空分词器/索引器缓存，避免测试间串扰。"""
    clear_tokenizer_cache()
    clear_indexer_cache()
    yield
    clear_tokenizer_cache()
    clear_indexer_cache()


@pytest_asyncio.fixture
async def pg():
    """只在随机测试库中运行 PG 集成用例。"""
    subprocess.run(
        ["createdb", "-h", TEST_PG_HOST, "-p", TEST_PG_PORT, DB_NAME],
        check=True,
        capture_output=True,
        text=True,
    )
    engine = create_async_engine(PG_URL)
    try:
        async with engine.connect() as conn:
            connected_db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            if not re.fullmatch(r"ai_creative_retrieval_test_[0-9a-f]{8}", connected_db):
                pytest.fail(f"拒绝在非测试数据库运行 PG 用例: {connected_db}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await ensure_fts_table(conn)
            # 与 alembic 031 / setup_pg.py 同契约的 FTS DDL（幂等）
            for table in ("retrieval_chunks_fts", "retrieval_chunks"):
                await conn.execute(text(
                    "ALTER TABLE %s ADD COLUMN IF NOT EXISTS search_tsv tsvector "
                    "GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED" % table
                ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_tsv "
                "ON retrieval_chunks_fts USING GIN (search_tsv)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_search_tsv "
                "ON retrieval_chunks USING GIN (search_tsv)"
            ))
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield engine, factory, session
    finally:
        await engine.dispose()
        subprocess.run(
            ["dropdb", "--if-exists", "-h", TEST_PG_HOST, "-p", TEST_PG_PORT, DB_NAME],
            check=False,
            capture_output=True,
            text=True,
        )


def _pid() -> str:
    return str(uuid.uuid4())


# ── 种子辅助 ──────────────────────────────────────────────

async def _seed_project_vocab(db: AsyncSession, pid: str) -> None:
    """种子项目词汇（角色/能力/别名簇/世界观专名），保证检索词稳定命中。"""
    db.add_all([
        CharacterProfile(project_id=pid, name="莫凡"),
        AbilityProfile(project_id=pid, character_name="莫凡",
                       ability_type="magic_element", ability_name="雷系"),
        CharacterAliasCluster(project_id=pid, canonical_name="莫凡", aliases=["小莫"]),
        WorldEntry(project_id=pid, title="玫炎", category="magic", content="玫炎是一种火焰魔法。"),
    ])
    await db.commit()


async def _seed_source_chunks(db: AsyncSession, pid: str, source_id: str) -> None:
    """种子资料源 + 切片，供 source_chunk 索引路径使用。"""
    content = "莫凡在灵域觉醒雷系魔法，玫炎燃烧如炬，这是全书核心设定。"
    db.add(ProjectSource(project_id=pid, title="灵域设定", source_type="upload",
                         content=content))
    db.add(ProjectSourceChunk(
        project_id=pid, source_id=source_id, chunk_index=0,
        content=content,
    ))
    await db.commit()


async def _seed_chapter_with_versions(
    db: AsyncSession, pid: str, *, contents: list[str], final_index: int
) -> tuple[Chapter, list[ChapterVersion]]:
    """种子章节 + 定稿版本，final_version_id 指向 final_index 对应的版本。"""
    chapter = Chapter(project_id=pid, title="第一章 觉醒", sequence_number=1,
                      content="草稿")
    db.add(chapter)
    await db.flush()
    versions: list[ChapterVersion] = []
    for i, content in enumerate(contents, start=1):
        version = ChapterVersion(
            project_id=pid, chapter_id=str(chapter.id), version_number=i,
            content=content, word_count=len(content),
        )
        db.add(version)
        await db.flush()
        versions.append(version)
    chapter.final_version_id = str(versions[final_index].id)
    await db.commit()
    return chapter, versions


async def _pending_job_count(db: AsyncSession) -> int:
    return len(
        (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.status == STATUS_PENDING)
        )).scalars().all()
    )


async def _cleanup_pg_project(engine, pid: str) -> None:
    """删除测试项目在 PG 上写入的全部行（只限本项目 id，不动业务数据）。"""
    async with engine.begin() as conn:
        for table in (
            "retrieval_chunks_fts", "retrieval_chunks", "retrieval_index_jobs",
            "retrieval_index_generation", "retrieval_index_source_state",
            "retrieval_vectors", "project_source_chunks", "project_sources",
            "character_profile", "ability_profile", "character_alias_clusters",
            "world_entries", "event_timeline", "chapter_versions", "chapters",
        ):
            try:
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE project_id = :pid"), {"pid": pid}
                )
            except Exception:  # noqa: BLE001 — 缺表/缺列时跳过单项清理
                pass


async def _pg_fts_hit(db: AsyncSession, pid: str, word: str) -> bool:
    return (
        await db.execute(
            text(
                "SELECT chunk_id FROM retrieval_chunks_fts "
                "WHERE project_id = :pid "
                "AND search_tsv @@ to_tsquery('simple', :q) LIMIT 1"
            ),
            {"pid": pid, "q": word},
        )
    ).scalar_one_or_none() is not None


# ── enqueue 同源合并策略（SQLite） ────────────────────────

@pytest.mark.asyncio
async def test_enqueue_merges_same_source_pending(db):
    """同源 pending job：再次入队合并（更新 source_version_id，不新增），返回 True。"""
    pid, sid = _pid(), "chapter-0001"
    assert await enqueue_index_job(
        db, pid, "chapter_version", sid, source_version_id="version-0001",
    ) is True
    # 同源 pending → 合并为最新请求
    assert await enqueue_index_job(
        db, pid, "chapter_version", sid, source_version_id="version-0002",
    ) is True
    await db.commit()

    jobs = (
        await db.execute(select(RetrievalIndexJob).where(
            RetrievalIndexJob.project_id == pid,
            RetrievalIndexJob.source_kind == "chapter_version",
            RetrievalIndexJob.source_id == sid,
        ))
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == STATUS_PENDING
    assert jobs[0].source_version_id == "version-0002"  # 已升级为最新版本
    assert jobs[0].attempts == 0

    # 不同 source_id 不受影响
    assert await enqueue_index_job(db, pid, "chapter_version", "chapter-0002") is True
    await db.commit()
    assert await _pending_job_count(db) == 2


@pytest.mark.asyncio
async def test_enqueue_processing_allows_new_pending_and_reenqueue_after_done(db):
    """processing 中的 job 不锁死：允许新增 pending（最新版本）；done 后也可再入队。"""
    pid, sid = _pid(), "src-0001"
    assert await enqueue_index_job(db, pid, "source_chunk", sid) is True
    await db.commit()

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    job.status = STATUS_PROCESSING
    job.claimed_at = datetime.now(timezone.utc)
    await db.commit()

    # 同源 processing → 允许新增一条 pending（最新版本）
    assert await enqueue_index_job(db, pid, "source_chunk", sid) is True
    await db.commit()
    jobs = (
        await db.execute(select(RetrievalIndexJob).where(
            RetrievalIndexJob.project_id == pid,
            RetrievalIndexJob.source_id == sid,
        ).order_by(RetrievalIndexJob.created_at))
    ).scalars().all()
    assert len(jobs) == 2
    assert {j.status for j in jobs} == {STATUS_PROCESSING, STATUS_PENDING}

    # 模拟 processing 完成（done），同源可再次入队
    for j in jobs:
        if j.status == STATUS_PROCESSING:
            j.status = STATUS_DONE
    await db.commit()
    assert await enqueue_index_job(db, pid, "source_chunk", sid) is True
    await db.commit()
    assert await _pending_job_count(db) == 1  # 仅新增的那条 pending


@pytest.mark.asyncio
async def test_enqueue_persists_fields(db):
    """source_version_id / payload / 默认状态与 attempts 正确落库。"""
    pid, sid = _pid(), "chapter-0001"
    assert await enqueue_index_job(
        db, pid, "chapter_version", sid,
        source_version_id="version-0009",
        payload={"chapter_no": 3, "title": "第三章"},
    ) is True
    await db.commit()

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.source_version_id == "version-0009"
    assert job.payload == {"chapter_no": 3, "title": "第三章"}
    assert job.status == STATUS_PENDING
    assert job.attempts == 0
    assert job.error is None
    assert job.claimed_at is None


# ── poll 状态机（SQLite） ─────────────────────────────────

@pytest.mark.asyncio
async def test_poll_success_job_to_done_with_real_index(db):
    """成功路径：source_chunk job 处理为 done，真实写入检索块并切换指针。"""
    pid, src_id = _pid(), str(uuid.uuid4())
    await _seed_project_vocab(db, pid)
    await _seed_source_chunks(db, pid, src_id)

    assert await enqueue_index_job(db, pid, "source_chunk", src_id) is True
    await db.commit()

    result = await poll_and_process_jobs(db, batch_size=10)
    assert result == {"reclaimed": 0, "processed": 1, "failed": 0, "skipped": 0}

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.status == STATUS_DONE
    assert job.error is None
    assert job.attempts == 0

    # 索引块真实落库（双写 retrieval_chunks + FTS5）
    chunk_rows = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar()
    assert chunk_rows >= 1
    fts_rows = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks_fts WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar()
    assert fts_rows == chunk_rows

    # SQLite 同步路径完成指针切换：source_state 指向 active generation
    state_rows = (
        await db.execute(
            text(
                "SELECT active_generation_id FROM retrieval_index_source_state "
                "WHERE project_id = :pid AND source_kind = 'source_chunk'"
            ),
            {"pid": pid},
        )
    ).scalars().all()
    assert len(state_rows) == 1


@pytest.mark.asyncio
async def test_poll_chapter_version_builds_and_activates(db):
    """成功路径：chapter_version job 构建并激活（Worker 侧不再重复切指针）。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    chapter, versions = await _seed_chapter_with_versions(
        db, pid, contents=[CONTENT_V1], final_index=0,
    )
    # SQLite 下 request_chapter_index 是同步构建（不写 job 表），这里直接构造
    # outbox job 行，覆盖 Worker 对 chapter_version 的消费路径
    assert await enqueue_index_job(
        db, pid, "chapter_version", str(chapter.id),
        source_version_id=str(versions[0].id),
    ) is True
    await db.commit()

    result = await poll_and_process_jobs(db)
    assert result == {"reclaimed": 0, "processed": 1, "failed": 0, "skipped": 0}

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.status == STATUS_DONE
    assert job.attempts == 0

    gen = (await db.execute(
        select(RetrievalIndexGeneration).where(RetrievalIndexGeneration.project_id == pid)
    )).scalars().one()
    assert gen.status == "active"
    state = (await db.execute(
        select(RetrievalIndexSourceState).where(
            RetrievalIndexSourceState.project_id == pid,
            RetrievalIndexSourceState.source_kind == "chapter_version",
            RetrievalIndexSourceState.source_id == str(chapter.id),
        )
    )).scalar_one()
    assert state.active_generation_id == gen.generation_id

    chunk_rows = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar()
    assert chunk_rows >= 1


@pytest.mark.asyncio
async def test_poll_marks_failed_with_attempts(db):
    """失败路径：未知 source_kind → job failed + attempts+1 + error 落库。"""
    pid, sid = _pid(), "mystery-0001"
    assert await enqueue_index_job(db, pid, "not_a_source_kind", sid) is True
    await db.commit()

    result = await poll_and_process_jobs(db)
    assert result["processed"] == 0
    assert result["failed"] == 1

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.status == STATUS_FAILED
    assert job.attempts == 1
    assert "not_a_source_kind" in (job.error or "")

    # failed job 不重复处理
    result2 = await poll_and_process_jobs(db)
    assert result2 == {"reclaimed": 0, "processed": 0, "failed": 0, "skipped": 0}
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_poll_reclaims_stale_processing(db):
    """超时回收：processing 且 claimed_at 过期的 job 重置回 pending 并重试。"""
    pid, src_id = _pid(), str(uuid.uuid4())
    await _seed_source_chunks(db, pid, src_id)

    assert await enqueue_index_job(db, pid, "source_chunk", src_id) is True
    await db.commit()
    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    job.status = STATUS_PROCESSING
    job.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    job.error = "上一次崩溃前的残留错误"
    await db.commit()

    # 第一次 poll（batch_size=0 只回收不消费）：超时 job 重置回 pending
    result = await poll_and_process_jobs(db, timeout_seconds=300, batch_size=0)
    assert result == {"reclaimed": 1, "processed": 0, "failed": 0, "skipped": 0}
    assert job.status == STATUS_PENDING
    assert job.claimed_at is None
    assert job.error is None

    # 第二次 poll：重新消费成功
    result2 = await poll_and_process_jobs(db)
    assert result2 == {"reclaimed": 0, "processed": 1, "failed": 0, "skipped": 0}
    assert job.status == STATUS_DONE


@pytest.mark.asyncio
async def test_poll_reclaim_blocked_by_newer_pending_marks_failed(db):
    """超时回收 + 同源已有 pending：不重置（避免唯一索引冲突），直接记 failed。"""
    pid, src_id = _pid(), str(uuid.uuid4())
    await _seed_project_vocab(db, pid)
    await _seed_source_chunks(db, pid, src_id)

    # v1 job → processing 且超时（模拟崩溃残留）
    assert await enqueue_index_job(db, pid, "source_chunk", src_id) is True
    await db.commit()
    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    job.status = STATUS_PROCESSING
    job.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    await db.commit()

    # v2（同源）→ 新增 pending（合并策略：processing 不锁死）
    assert await enqueue_index_job(db, pid, "source_chunk", src_id) is True
    await db.commit()

    result = await poll_and_process_jobs(db)
    assert result["reclaimed"] == 0  # 被 pending 挡住，不计数 reclaimed
    assert result["processed"] == 1  # 最新 pending 被消费
    assert result["failed"] == 1     # 超时残留收尾 failed

    jobs = (await db.execute(select(RetrievalIndexJob).where(
        RetrievalIndexJob.project_id == pid,
    ).order_by(RetrievalIndexJob.created_at))).scalars().all()
    assert len(jobs) == 2
    by_status = {j.status: j for j in jobs}
    assert by_status[STATUS_DONE].error is None
    assert by_status[STATUS_FAILED].attempts == 1
    assert "superseded_by_newer_pending" in (by_status[STATUS_FAILED].error or "")


@pytest.mark.asyncio
async def test_poll_keeps_fresh_processing(db):
    """未超时的 processing job 不被回收。"""
    pid, sid = _pid(), "src-0001"
    assert await enqueue_index_job(db, pid, "source_chunk", sid) is True
    await db.commit()
    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    job.status = STATUS_PROCESSING
    job.claimed_at = datetime.now(timezone.utc)
    await db.commit()

    result = await poll_and_process_jobs(db, timeout_seconds=300)
    assert result == {"reclaimed": 0, "processed": 0, "failed": 0, "skipped": 0}
    assert job.status == STATUS_PROCESSING


@pytest.mark.asyncio
async def test_poll_does_not_reprocess_done_jobs(db):
    """重复 poll 不重复处理 done job，索引数据不重复写入。"""
    pid, src_id = _pid(), str(uuid.uuid4())
    await _seed_source_chunks(db, pid, src_id)
    assert await enqueue_index_job(db, pid, "source_chunk", src_id) is True
    await db.commit()

    result1 = await poll_and_process_jobs(db)
    assert result1["processed"] == 1
    chunks_before = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar()

    result2 = await poll_and_process_jobs(db)
    assert result2 == {"reclaimed": 0, "processed": 0, "failed": 0, "skipped": 0}
    chunks_after = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar()
    assert chunks_after == chunks_before
    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.status == STATUS_DONE
    assert job.attempts == 0


@pytest.mark.asyncio
async def test_poll_stale_chapter_version_marked_failed_without_build(db):
    """stale 校验（SQLite）：job 指向非当前定稿版本 → failed，不构建不切指针。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    chapter, versions = await _seed_chapter_with_versions(
        db, pid, contents=[CONTENT_V1, CONTENT_V2], final_index=1,  # 当前定稿 = v2
    )
    # 手工构造一条指向旧版本 v1 的 pending job（v1 未消费时已再次定稿 v2）
    assert await enqueue_index_job(
        db, pid, "chapter_version", str(chapter.id),
        source_version_id=str(versions[0].id),
    ) is True
    await db.commit()

    result = await poll_and_process_jobs(db)
    assert result["failed"] == 1
    assert result["processed"] == 0

    job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
    assert job.status == STATUS_FAILED
    assert job.attempts == 1
    assert "stale_version" in (job.error or "")

    # 不构建、不切指针：无块、无 generation、无 source_state
    assert (await db.execute(
        text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
        {"pid": pid},
    )).scalar() == 0
    assert (await db.execute(
        select(RetrievalIndexGeneration).where(RetrievalIndexGeneration.project_id == pid)
    )).scalars().all() == []
    assert (await db.execute(
        select(RetrievalIndexSourceState).where(RetrievalIndexSourceState.project_id == pid)
    )).scalars().all() == []


@pytest.mark.asyncio
async def test_request_source_index_sync_builds_on_sqlite(db):
    """request_source_index 在 SQLite 同步构建（桌面端语义，无 job 表写入）。"""
    pid, src_id = _pid(), str(uuid.uuid4())
    await _seed_project_vocab(db, pid)
    await _seed_source_chunks(db, pid, src_id)

    await request_source_index(db, pid, src_id)
    await db.commit()

    # 不写 outbox job，直接同步构建完成
    assert (await db.execute(select(RetrievalIndexJob))).scalars().all() == []
    assert (await db.execute(
        text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
        {"pid": pid},
    )).scalar() >= 1
    state = (await db.execute(
        select(RetrievalIndexSourceState).where(
            RetrievalIndexSourceState.project_id == pid,
            RetrievalIndexSourceState.source_kind == "source_chunk",
            RetrievalIndexSourceState.source_id == src_id,
        )
    )).scalar_one()
    assert state.active_generation_id is not None


# ── 运行入口 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_worker_loop_once_consumes_pending():
    """run_worker_loop(once=True)：单轮处理一批 pending 后退出。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_fts_table(conn)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    pid, src_id = _pid(), str(uuid.uuid4())
    try:
        async with factory() as s:
            await _seed_project_vocab(s, pid)
            await _seed_source_chunks(s, pid, src_id)
            assert await enqueue_index_job(s, pid, "source_chunk", src_id) is True
            await s.commit()

        stats = await run_worker_loop(
            interval_sec=0.1, batch_size=10, once=True, session_factory=factory,
        )
        assert stats is not None
        assert stats["processed"] == 1

        async with factory() as s:
            jobs = (await s.execute(select(RetrievalIndexJob))).scalars().all()
            assert len(jobs) == 1 and jobs[0].status == STATUS_DONE
            assert (await s.execute(
                text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
                {"pid": pid},
            )).scalar() >= 1
    finally:
        await engine.dispose()


def test_worker_main_rejects_sqlite(monkeypatch, capsys):
    """SQLite 方言下 Worker 入口打印提示并退出（桌面端不启动 Worker）。"""
    monkeypatch.setattr("os.environ", dict(os.environ))  # 隔离 main 对 env 的写入
    rc = worker_main(["--db", "sqlite+aiosqlite:///:memory:", "--once"])
    assert rc == 1
    assert "SQLite" in capsys.readouterr().err


# ── PG 验收用例（仅 RUN_PG_TESTS=1 时执行） ────────────────

@pg_required
@pytest.mark.asyncio
async def test_pg_1_finalize_outbox_worker_active_hit(pg):
    """验收 1：定稿 → outbox → Worker → active → 检索命中。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, versions = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1], final_index=0,
        )

        # 模拟 finalize：定稿版本已建、final_version_id 已指向，request 写 outbox
        await request_chapter_index(db, pid, str(versions[0].id))
        await db.commit()

        jobs = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].status == STATUS_PENDING
        assert jobs[0].source_id == str(chapter.id)
        assert jobs[0].source_version_id == str(versions[0].id)

        result = await poll_and_process_jobs(db)
        assert result == {"reclaimed": 0, "processed": 1, "failed": 0, "skipped": 0}

        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        assert job.status == STATUS_DONE
        assert job.attempts == 0

        # generation active + source_state 指针指向它
        gen = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().one()
        assert gen.status == "active"
        state = (await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "chapter_version",
                RetrievalIndexSourceState.source_id == str(chapter.id),
            )
        )).scalar_one()
        assert state.active_generation_id == gen.generation_id

        # 块落库 + search_tsv 检索命中
        chunk_rows = (await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )).scalar()
        assert chunk_rows >= 1
        for word in _VOCAB_WORDS:
            assert await _pg_fts_hit(db, pid, word), f"PG FTS 检索未命中词: {word}"
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_2_refinalize_only_new_version_recallable(pg):
    """验收 2：连续定稿两次只命中新版（指针切换 + 旧块删除）。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, [v1, v2] = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1, CONTENT_V2], final_index=0,
        )

        # 第一次定稿：v1 可检索
        await request_chapter_index(db, pid, str(v1.id))
        await db.commit()
        r1 = await poll_and_process_jobs(db)
        assert r1["processed"] == 1
        gen1 = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().one()
        assert gen1.status == "active"
        assert await _pg_fts_hit(db, pid, "雷系")

        # 再次定稿：v2 生效，指针切换到新 generation
        chapter.final_version_id = str(v2.id)
        await db.commit()
        await request_chapter_index(db, pid, str(v2.id))
        await db.commit()
        r2 = await poll_and_process_jobs(db)
        assert r2["processed"] == 1

        state = (await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "chapter_version",
                RetrievalIndexSourceState.source_id == str(chapter.id),
            )
        )).scalar_one()
        gen2 = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid,
                RetrievalIndexGeneration.status == "active",
            )
        )).scalars().one()
        assert state.active_generation_id == gen2.generation_id
        assert gen2.generation_id != gen1.generation_id
        # 旧 generation superseded
        old = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gen1.generation_id
            )
        )).scalar_one()
        assert old.status == "superseded"
        # 该 source_id 只剩新 generation 的块（旧块已双删）
        remaining_gids = (await db.execute(
            text(
                "SELECT DISTINCT index_generation_id FROM retrieval_chunks "
                "WHERE project_id = :pid AND source_kind = 'chapter_version' "
                "AND source_id = :sid"
            ),
            {"pid": pid, "sid": str(chapter.id)},
        )).scalars().all()
        assert [str(g) for g in remaining_gids] == [gen2.generation_id]

        # 检索只召回新版：雷系（旧版专属词）不可命中，火系命中
        assert not await _pg_fts_hit(db, pid, "雷系")
        assert await _pg_fts_hit(db, pid, "莫凡")
        hit_content = (await db.execute(
            text(
                "SELECT content FROM retrieval_chunks_fts "
                "WHERE project_id = :pid "
                "AND search_tsv @@ to_tsquery('simple', '莫凡') LIMIT 1"
            ),
            {"pid": pid},
        )).scalar()
        assert "火系" in (hit_content or "")
        assert "雷系" not in (hit_content or "")
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_3_v1_processing_then_v2_enqueued(pg):
    """验收 3：v1 processing 时产生 v2 → 合并策略新增 pending → v2 生效。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, [v1, v2] = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1, CONTENT_V2], final_index=0,
        )
        await request_chapter_index(db, pid, str(v1.id))
        await db.commit()

        # 手工构造 v1 处于 processing（模拟 Worker 正在消费）
        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        job.status = STATUS_PROCESSING
        job.claimed_at = datetime.now(timezone.utc)
        await db.commit()

        # 再次定稿 v2（v1 仍 processing）→ 合并策略：允许新增一条 pending
        chapter.final_version_id = str(v2.id)
        await db.commit()
        await request_chapter_index(db, pid, str(v2.id))
        await db.commit()

        jobs = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
            .order_by(RetrievalIndexJob.created_at)
        )).scalars().all()
        assert len(jobs) == 2
        statuses = {j.status for j in jobs}
        assert statuses == {STATUS_PROCESSING, STATUS_PENDING}
        pending = next(j for j in jobs if j.status == STATUS_PENDING)
        assert pending.source_version_id == str(v2.id)

        # poll：新 pending 被消费，v2 生效；v1 processing job 原样保留
        result = await poll_and_process_jobs(db)
        assert result["processed"] == 1
        assert result["failed"] == 0

        active_gen = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid,
                RetrievalIndexGeneration.status == "active",
            )
        )).scalars().one()
        assert await _pg_fts_hit(db, pid, "火系")
        assert not await _pg_fts_hit(db, pid, "雷系")

        jobs_after = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
            .order_by(RetrievalIndexJob.created_at)
        )).scalars().all()
        v1_job = next(j for j in jobs_after if j.status == STATUS_PROCESSING)
        assert v1_job.source_version_id == str(v1.id)
        done_job = next(j for j in jobs_after if j.status == STATUS_DONE)
        assert done_job.source_version_id == str(v2.id)
        _ = active_gen
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_4_two_workers_no_duplicate_consumption(pg):
    """验收 4：两个 Worker 并发各跑一轮 → 每 job 恰好一次，无重复 generation。"""
    engine, factory, db = pg
    pid = _pid()
    n_jobs = 3
    try:
        await _seed_project_vocab(db, pid)
        for i in range(n_jobs):
            chapter, versions = await _seed_chapter_with_versions(
                db, pid,
                contents=[f"第{i}章 觉醒\n莫凡觉醒了雷系。{i}"],
                final_index=0,
            )
            await request_chapter_index(db, pid, str(versions[0].id))
            await db.commit()
        assert await _pending_job_count(db) == n_jobs

        async with factory() as s1, factory() as s2:
            r1, r2 = await asyncio.gather(
                poll_and_process_jobs(s1, batch_size=10),
                poll_and_process_jobs(s2, batch_size=10),
            )
        assert r1["failed"] == 0 and r2["failed"] == 0
        assert r1["processed"] + r2["processed"] == n_jobs

        # 每个 job 最终 done 唯一、attempts==0（未被重复消费）
        jobs = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().all()
        assert len(jobs) == n_jobs
        assert all(j.status == STATUS_DONE and j.attempts == 0 for j in jobs)

        # 无重复 generation：每源一个 active generation，块 generation 无重复
        gens = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().all()
        assert len(gens) == n_jobs
        assert all(g.status == "active" for g in gens)
        gids = {g.generation_id for g in gens}
        states = (await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid
            )
        )).scalars().all()
        assert len(states) == n_jobs
        assert all(s.active_generation_id in gids for s in states)
        chunk_gids = (await db.execute(
            text("SELECT DISTINCT index_generation_id FROM retrieval_chunks "
                 "WHERE project_id = :pid"),
            {"pid": pid},
        )).scalars().all()
        assert set(str(g) for g in chunk_gids) == gids
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_5_crash_recovery_timeout_reclaim(pg):
    """验收 5：崩溃后超时回收：processing + 超时 claimed_at → 重置 pending → 再消费。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, versions = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1], final_index=0,
        )
        await request_chapter_index(db, pid, str(versions[0].id))
        await db.commit()

        # 模拟 Worker 崩溃：job 停在 processing，claimed_at 已超时（3600s 前）
        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        job.status = STATUS_PROCESSING
        job.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
        await db.commit()

        # 第一轮（batch_size=0 只回收）：超时 job 重置回 pending
        r1 = await poll_and_process_jobs(db, batch_size=0)
        assert r1["reclaimed"] == 1
        assert r1["processed"] == 0
        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        assert job.status == STATUS_PENDING

        # 第二轮：成功消费
        r2 = await poll_and_process_jobs(db)
        assert r2["processed"] == 1
        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        assert job.status == STATUS_DONE
        gen = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().one()
        assert gen.status == "active"
        assert await _pg_fts_hit(db, pid, "莫凡")
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_6_build_failure_leaves_no_partial_state(pg, monkeypatch):
    """验收 6：构建失败不留半成品：failed + attempts=1，无 active 指针、无可见块。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, versions = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1], final_index=0,
        )
        await request_chapter_index(db, pid, str(versions[0].id))
        await db.commit()

        async def _boom(*args, **kwargs):
            raise RuntimeError("indexer boom")

        monkeypatch.setattr(worker_mod, "build_chapter_index", _boom)

        result = await poll_and_process_jobs(db)
        assert result["processed"] == 0
        assert result["failed"] == 1

        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        assert job.status == STATUS_FAILED
        assert job.attempts == 1
        assert "indexer boom" in (job.error or "")

        # 无可见半成品：无块、无 generation、无 source_state（回滚了领取与半成品）
        assert (await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )).scalar() == 0
        assert (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().all() == []
        assert (await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid
            )
        )).scalars().all() == []

        # failed job 不重复消费
        r2 = await poll_and_process_jobs(db)
        assert r2["processed"] == 0 and r2["failed"] == 0
        _ = chapter
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_7_stale_version_not_built_or_switched(pg):
    """验收 7：stale 校验：job.source_version_id ≠ chapter.final_version_id →
    不构建、不切指针（旧版本不得把指针切回去）。"""
    engine, _factory, db = pg
    pid = _pid()
    try:
        await _seed_project_vocab(db, pid)
        chapter, [v1, v2] = await _seed_chapter_with_versions(
            db, pid, contents=[CONTENT_V1, CONTENT_V2], final_index=1,  # 当前定稿 = v2
        )
        # 手工构造指向旧版本 v1 的 pending job（v1 未消费时已再次定稿 v2）
        assert await enqueue_index_job(
            db, pid, "chapter_version", str(chapter.id),
            source_version_id=str(v1.id),
        ) is True
        await db.commit()

        result = await poll_and_process_jobs(db)
        assert result["failed"] == 1
        assert result["processed"] == 0

        job = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().one()
        assert job.status == STATUS_FAILED
        assert job.attempts == 1
        assert "stale_version" in (job.error or "")

        # 不构建不切指针：无块、无 generation、无 source_state
        assert (await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )).scalar() == 0
        assert (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().all() == []
        assert (await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid
            )
        )).scalars().all() == []
        assert not await _pg_fts_hit(db, pid, "雷系")
    finally:
        await _cleanup_pg_project(engine, pid)


# ── PG 补充用例（request_* 分流 / 并发入队竞态 / source_chunk 端到端） ──

@pg_required
@pytest.mark.asyncio
async def test_pg_request_source_index_outbox_end_to_end_fts_hit(pg):
    """PG 端到端（source_chunk）：request_source_index 写 outbox → Worker → FTS 命中。"""
    engine, _factory, db = pg
    pid, src_id = _pid(), str(uuid.uuid4())
    try:
        await _seed_project_vocab(db, pid)
        await _seed_source_chunks(db, pid, src_id)

        await request_source_index(db, pid, src_id)
        await db.commit()
        assert await _pending_job_count(db) == 1

        result = await poll_and_process_jobs(db)
        assert result == {"reclaimed": 0, "processed": 1, "failed": 0, "skipped": 0}

        job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
        assert job.status == STATUS_DONE
        assert job.attempts == 0
        assert (await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )).scalar() >= 1
        for word in _VOCAB_WORDS:
            assert await _pg_fts_hit(db, pid, word), f"PG FTS 检索未命中词: {word}"
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_request_structured_index_via_outbox_payload(pg):
    """PG 端到端（structured）：request_structured_index payload → Worker 构建命中。"""
    engine, _factory, db = pg
    pid, sid = _pid(), str(uuid.uuid4())
    try:
        await _seed_project_vocab(db, pid)
        await request_structured_index(
            db, pid, "world_entry", sid,
            title="圣城", content="圣城是世界的中心，莫凡曾在此觉醒雷系。", chapter_no=3,
        )
        await db.commit()

        job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
        assert job.payload["title"] == "圣城"
        assert "莫凡" in (job.payload["content"] or "")
        assert job.payload["chapter_no"] == 3

        result = await poll_and_process_jobs(db)
        assert result["processed"] == 1

        job = (await db.execute(select(RetrievalIndexJob))).scalars().one()
        assert job.status == STATUS_DONE
        gen = (await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )).scalars().one()
        assert gen.status == "active"
        assert gen.source_kind == "world_entry"
        assert await _pg_fts_hit(db, pid, "莫凡")
    finally:
        await _cleanup_pg_project(engine, pid)


@pg_required
@pytest.mark.asyncio
async def test_pg_enqueue_concurrent_race_merges(pg):
    """并发入队竞态：两事务同时 enqueue 同源 → 唯一索引兜底，读回合并为一条。"""
    engine, factory, db = pg
    pid = _pid()
    try:
        async with factory() as s1, factory() as s2:
            # s1 插入 pending（不提交）；s2 同源 enqueue 会在唯一索引上等待，
            # s1 提交后 s2 收到 IntegrityError → 读回合并为最新版本
            assert await enqueue_index_job(s1, pid, "source_chunk", "src-race-1") is True

            async def _commit_s1():
                await asyncio.sleep(0.05)  # 确保 s2 的 INSERT 已开始等待
                await s1.commit()

            committer = asyncio.create_task(_commit_s1())
            try:
                assert await enqueue_index_job(
                    s2, pid, "source_chunk", "src-race-1", source_version_id="v2",
                ) is True
            finally:
                await committer
            await s2.commit()

        jobs = (await db.execute(
            select(RetrievalIndexJob).where(RetrievalIndexJob.project_id == pid)
        )).scalars().all()
        assert len(jobs) == 1  # 合并：没有重复 pending
        assert jobs[0].status == STATUS_PENDING
        assert jobs[0].source_version_id == "v2"  # 已合并为最新请求
    finally:
        await _cleanup_pg_project(engine, pid)
