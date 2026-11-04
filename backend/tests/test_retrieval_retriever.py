"""词法检索器 + 索引写入器（阶段一核心）单元测试。

每个测试独立使用 SQLite 内存库（StaticPool 共享同一连接）：
ensure_fts_table 显式建 FTS5 虚拟表 → 种子项目词汇（角色/能力/别名簇）→
写入测试块（含中文「莫凡/雷系/玫炎」等）→ 验证 LexicalRetriever.search：
词法命中、实体优先排序、source_kinds 过滤、chapter_num 过滤、无结果返回 []。
另覆盖索引写入器：generation 生命周期（指针切换）、content_hash 幂等、
失败清理、定稿章节索引、rebuild_project 全量重建。
"""

import logging
import uuid

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
from models.structured_knowledge import (
    AbilityProfile,
    CharacterAliasCluster,
    CharacterProfile,
    EventTimeline,
)
from models.world_entry import WorldEntry
from services.lexical_retriever import LexicalRetriever, RetrievalHit
from services.retrieval_indexer import (
    clear_indexer_cache,
    ensure_fts_table,
    fail_generation,
    finish_generation_active,
    index_chapter_version,
    index_source_chunks,
    rebuild_project,
    start_generation,
)
from services.retrieval_tokenizer import (
    build_project_vocabulary,
    clear_tokenizer_cache,
    get_project_tokenizer,
)


@pytest_asyncio.fixture
async def db():
    """独立的 SQLite 内存库会话（含 FTS5 虚拟表）。"""
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


def _pid() -> str:
    return str(uuid.uuid4())


# ── 测试辅助 ─────────────────────────────────────────────

async def _seed_project_vocab(db: AsyncSession, pid: str) -> None:
    """种子项目词汇（角色/能力/别名簇/世界观专名），来源与生产一致。"""
    db.add_all([
        CharacterProfile(project_id=pid, name="莫凡",
                         identity_desc="法师 觉醒", origin="manual", source_priority=100),
        AbilityProfile(project_id=pid, character_name="莫凡",
                       ability_type="magic_element", ability_name="雷系",
                       level_desc="初阶", origin="manual", source_priority=100),
        CharacterAliasCluster(project_id=pid, canonical_name="莫凡",
                              aliases=["小莫"], source="manual"),
        WorldEntry(project_id=pid, title="玫炎", category="magic",
                   content="玫炎是一种火焰魔法。"),
    ])
    await db.commit()


async def _build_vocab(db: AsyncSession, pid: str):
    """构建项目分词器（等价索引侧/检索侧首次调用）。"""
    data = await build_project_vocabulary(db, pid)
    get_project_tokenizer(pid, data["version"])
    return get_project_tokenizer(pid)


def _search_text(tok, content: str) -> str:
    return " ".join(tok.tokenize(content))


async def _insert_test_chunk(
    db: AsyncSession,
    pid: str,
    *,
    chunk_id: str,
    source_kind: str = "source_chunk",
    source_id: str,
    chapter_no=None,
    title: str = "",
    content: str,
    search_text: str,
    source_version_id: str | None = None,
    generation_id: str = "test-gen",
) -> None:
    """直接双写一条测试块（retrieval_chunks + fts 同一事务）。"""
    await db.execute(
        text(
            "INSERT INTO retrieval_chunks (id, project_id, source_kind, source_id, "
            "source_version_id, index_generation_id, chunk_index, content, search_text, "
            "content_hash, metadata) VALUES (:id, :pid, :sk, :sid, :svid, :gid, 0, "
            ":content, :st, :h, NULL)"
        ),
        {
            "id": chunk_id, "pid": pid, "sk": source_kind, "sid": source_id,
            "svid": source_version_id, "gid": generation_id, "content": content,
            "st": search_text, "h": "test-hash",
        },
    )
    await db.execute(
        text(
            "INSERT INTO retrieval_chunks_fts (chunk_id, project_id, source_kind, "
            "source_id, chapter_no, title, content, search_text) VALUES "
            "(:id, :pid, :sk, :sid, :cn, :title, :content, :st)"
        ),
        {
            "id": chunk_id, "pid": pid, "sk": source_kind, "sid": source_id,
            "cn": chapter_no, "title": title, "content": content, "st": search_text,
        },
    )
    await db.commit()


def _retriever() -> LexicalRetriever:
    return LexicalRetriever()


# ── 检索：词法命中 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_lexical_search_finds_relevant_chunk(db):
    """中文查询分词后 FTS 命中：含「莫凡/雷系」的块排在最前，rank 从 1 开始。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)

    await _insert_test_chunk(
        db, pid, chunk_id="c-a", source_id="src-a",
        content="莫凡觉醒了雷系。他是主角。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。他是主角。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id="c-b", source_id="src-b",
        content="张小侯使用了风之力。",
        search_text=_search_text(tok, "张小侯使用了风之力。"),
    )

    hits = await _retriever().search(db, pid, "莫凡的雷系是什么？", limit=5)
    assert hits, "应当有命中"
    assert hits[0].chunk_id == "c-a"
    assert hits[0].rank == 1
    assert hits[0].source_id == "src-a"
    assert hits[0].source_kind == "source_chunk"
    assert hits[0].project_id == pid
    assert hits[0].score > 0
    assert "雷系" in hits[0].snippet
    assert hits[0].content == "莫凡觉醒了雷系。他是主角。"
    # c-b 不含任何 query token，不应被召回
    assert all(h.chunk_id != "c-b" for h in hits)


@pytest.mark.asyncio
async def test_search_returns_empty_on_no_match(db):
    """查询 token 全部无命中时返回 []。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    await _insert_test_chunk(
        db, pid, chunk_id="c-a", source_id="src-a",
        content="莫凡觉醒了雷系。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。"),
    )
    assert await _retriever().search(db, pid, "量子猫薄荷") == []
    assert await _retriever().search(db, pid, "") == []
    assert await _retriever().search(db, pid, "莫凡", limit=0) == []


# ── 检索：实体优先通道 ───────────────────────────────────

@pytest.mark.asyncio
async def test_entity_first_ordering(db):
    """别名「小莫」归一为「莫凡」：含莫凡的块加权排最前，再按词法分排序。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)

    await _insert_test_chunk(
        db, pid, chunk_id="c-e1", source_id="src-e",
        content="莫凡在魔法高中参加觉醒仪式。",
        search_text=_search_text(tok, "莫凡在魔法高中参加觉醒仪式。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id="c-e2", source_id="src-e",
        content="雷系魔法威力强大。",
        search_text=_search_text(tok, "雷系魔法威力强大。"),
    )

    hits = await _retriever().search(db, pid, "小莫的雷系", limit=5)
    assert [h.chunk_id for h in hits] == ["c-e1", "c-e2"], \
        "实体命中（莫凡）应排在词法命中（雷系）之前"
    assert hits[0].rank == 1
    assert hits[0].score > hits[1].score


@pytest.mark.asyncio
async def test_entity_first_disabled(db):
    """entity_first=False 时不做实体加权，仅词法通道。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    await _insert_test_chunk(
        db, pid, chunk_id="c-e1", source_id="src-e",
        content="莫凡在魔法高中参加觉醒仪式。",
        search_text=_search_text(tok, "莫凡在魔法高中参加觉醒仪式。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id="c-e2", source_id="src-e",
        content="雷系魔法威力强大。",
        search_text=_search_text(tok, "雷系魔法威力强大。"),
    )
    hits = await _retriever().search(db, pid, "小莫的雷系", limit=5, entity_first=False)
    # 词法 token 为 小莫/的/雷系：只有 c-e2 含「雷系」，c-e1 不含小莫
    assert [h.chunk_id for h in hits] == ["c-e2"]


# ── 检索：过滤 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_source_kinds_filter(db):
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    content = "莫凡觉醒了雷系。"
    await _insert_test_chunk(
        db, pid, chunk_id="c-f1", source_kind="chapter_version", source_id="src-f",
        content=content, search_text=_search_text(tok, content),
    )
    await _insert_test_chunk(
        db, pid, chunk_id="c-f2", source_kind="source_chunk", source_id="src-f",
        content=content, search_text=_search_text(tok, content),
    )

    retriever = _retriever()
    only_chapter = await retriever.search(db, pid, "莫凡", source_kinds=["chapter_version"])
    assert [h.chunk_id for h in only_chapter] == ["c-f1"]

    both = await retriever.search(
        db, pid, "莫凡", source_kinds=["chapter_version", "source_chunk"]
    )
    assert {h.chunk_id for h in both} == {"c-f1", "c-f2"}


@pytest.mark.asyncio
async def test_chapter_num_filter(db):
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    await _insert_test_chunk(
        db, pid, chunk_id="c-g2", source_kind="chapter_version", source_id="src-g",
        chapter_no=2, title="第二章", content="莫凡觉醒了雷系。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id="c-g3", source_kind="chapter_version", source_id="src-g",
        chapter_no=3, title="第三章", content="莫凡觉醒了火系。",
        search_text=_search_text(tok, "莫凡觉醒了火系。"),
    )

    retriever = _retriever()
    hits = await retriever.search(db, pid, "莫凡", chapter_num=3)
    assert [h.chunk_id for h in hits] == ["c-g3"]
    assert hits[0].chapter_no == 3
    assert await retriever.search(db, pid, "莫凡", chapter_num=99) == []
    # 不限章节时两条都出
    assert {h.chunk_id for h in await retriever.search(db, pid, "莫凡")} == {"c-g2", "c-g3"}


# ── 索引：generation 生命周期（D1 指针切换） ─────────────

@pytest.mark.asyncio
async def test_generation_lifecycle(db):
    pid = _pid()
    gid = await start_generation(db, pid, "source_chunk", "src-1")
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gid
            )
        )
    ).scalar_one()
    assert gen.status == "building"
    assert gen.started_at is not None

    await finish_generation_active(db, gid)
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gid
            )
        )
    ).scalar_one()
    assert gen.status == "active"
    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "source_chunk",
                RetrievalIndexSourceState.source_id == "src-1",
            )
        )
    ).scalar_one()
    assert state.active_generation_id == gid

    # 非 building 状态不能再次切 active
    with pytest.raises(ValueError):
        await finish_generation_active(db, gid)


@pytest.mark.asyncio
async def test_generation_switch_replaces_pointer(db):
    """切换指针时：旧 generation 标记 superseded，且其块被物理删除。"""
    pid = _pid()
    gid1 = await start_generation(db, pid, "source_chunk", "src-1")
    await _insert_test_chunk(
        db, pid, chunk_id="c-old", source_id="src-1",
        content="旧内容", search_text="旧内容", generation_id=gid1,
    )
    # 手动把第一代置 active（等价索引流程收尾）
    await finish_generation_active(db, gid1)

    gid2 = await start_generation(db, pid, "source_chunk", "src-1")
    await _insert_test_chunk(
        db, pid, chunk_id="c-new", source_id="src-1",
        content="新内容", search_text="新内容", generation_id=gid2,
    )
    await finish_generation_active(db, gid2)

    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "source_chunk",
                RetrievalIndexSourceState.source_id == "src-1",
            )
        )
    ).scalar_one()
    assert state.active_generation_id == gid2

    old_gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gid1
            )
        )
    ).scalar_one()
    assert old_gen.status == "superseded"

    # 旧块（含 fts 行）已被同步双删，只剩新块
    chunks = (
        await db.execute(text("SELECT id FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid2})
    ).scalars().all()
    assert [str(c) for c in chunks] == ["c-new"]
    old_fts = (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks_fts WHERE chunk_id = :id"), {"id": "c-old"}
        )
    ).scalar()
    assert old_fts == 0


@pytest.mark.asyncio
async def test_fail_generation_cleans_chunks(db):
    pid = _pid()
    gid = await start_generation(db, pid, "source_chunk", "src-1")
    await _insert_test_chunk(
        db, pid, chunk_id="c-x", source_id="src-1",
        content="坏数据", search_text="坏数据", generation_id=gid,
    )
    await fail_generation(db, gid, "模拟写入失败")

    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gid
            )
        )
    ).scalar_one()
    assert gen.status == "failed"
    assert gen.error == "模拟写入失败"
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid}
        )
    ).scalar() == 0
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks_fts WHERE chunk_id = :id"), {"id": "c-x"}
        )
    ).scalar() == 0


# ── 索引：素材切片投影 + 幂等 ────────────────────────────

@pytest.mark.asyncio
async def test_index_source_chunks_and_idempotency(db):
    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)
    source = ProjectSource(project_id=pid, title="测试资料", content="莫凡觉醒了雷系。张小侯使用了风之力。")
    db.add(source)
    await db.flush()
    sid = str(source.id)
    db.add_all([
        ProjectSourceChunk(project_id=pid, source_id=sid, chunk_index=0,
                           content="莫凡觉醒了雷系。", token_count=10),
        ProjectSourceChunk(project_id=pid, source_id=sid, chunk_index=1,
                           content="张小侯使用了风之力。", token_count=10),
    ])
    await db.commit()

    gid1 = await index_source_chunks(db, pid, sid)
    assert gid1 is not None
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid1}
        )
    ).scalar() == 2
    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "source_chunk",
                RetrievalIndexSourceState.source_id == sid,
            )
        )
    ).scalar_one()
    assert state.active_generation_id == gid1

    # 幂等：内容未变直接跳过
    assert await index_source_chunks(db, pid, sid) is None
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid1}
        )
    ).scalar() == 2

    # 内容变化 → 重建：新 generation 生效，旧块（含 fts）被删
    chunk0 = (
        await db.execute(
            select(ProjectSourceChunk).where(
                ProjectSourceChunk.source_id == sid, ProjectSourceChunk.chunk_index == 0
            )
        )
    ).scalar_one()
    chunk0.content = "莫凡觉醒了火系。"
    await db.commit()

    gid2 = await index_source_chunks(db, pid, sid)
    assert gid2 is not None and gid2 != gid1
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid1}
        )
    ).scalar() == 0
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE index_generation_id = :g"), {"g": gid2}
        )
    ).scalar() == 2

    # 检索能看到新内容（火系），旧内容（雷系只留在另一块）
    tok = get_project_tokenizer(pid)
    hits = await _retriever().search(db, pid, "莫凡", limit=5)
    assert hits
    assert any("火系" in h.content for h in hits)


@pytest.mark.asyncio
async def test_index_source_chunks_empty_returns_none(db):
    pid = _pid()
    assert await index_source_chunks(db, pid, "no-such-source") is None


# ── 索引：定稿章节版本 ───────────────────────────────────

@pytest.mark.asyncio
async def test_index_chapter_version(db):
    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)

    chapter = Chapter(project_id=pid, title="第一章 觉醒", sequence_number=1,
                      content="莫凡觉醒了雷系。")
    db.add(chapter)
    await db.flush()
    version = ChapterVersion(
        project_id=pid, chapter_id=str(chapter.id), version_number=1,
        content="第一章 觉醒\n莫凡觉醒了雷系。", word_count=20,
    )
    db.add(version)
    await db.commit()

    gid = await index_chapter_version(db, pid, str(version.id))
    assert gid is not None

    # 稳定来源标识（P0）：source_id = chapter.id（章节稳定），
    # source_version_id = version.id（本次定稿版本）
    chunk_rows = (
        await db.execute(
            text(
                "SELECT source_id, source_version_id FROM retrieval_chunks "
                "WHERE index_generation_id = :g"
            ),
            {"g": gid},
        )
    ).all()
    assert chunk_rows
    assert all(r[0] == str(chapter.id) for r in chunk_rows)
    assert all(r[1] == str(version.id) for r in chunk_rows)

    # 直接查 fts：章节号/标题来自 Chapter（sequence_number=1）
    fts_rows = (
        await db.execute(
            text(
                "SELECT f.chapter_no, f.title, f.chunk_id "
                "FROM retrieval_chunks_fts f "
                "JOIN retrieval_chunks c ON c.id = f.chunk_id "
                "WHERE c.index_generation_id = :g"
            ),
            {"g": gid},
        )
    ).all()
    assert len(fts_rows) >= 1
    assert fts_rows[0][0] == 1  # chapter_no
    assert fts_rows[0][1] == "第一章 觉醒"

    retriever = _retriever()
    hits = await retriever.search(db, pid, "莫凡", chapter_num=1)
    assert hits and hits[0].chapter_no == 1
    assert await retriever.search(db, pid, "莫凡", chapter_num=2) == []

    # 幂等：同一版本再索引一次跳过
    assert await index_chapter_version(db, pid, str(version.id)) is None


# ── 索引：全量重建 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_rebuild_project_covers_all_kinds(db):
    pid = _pid()
    await _seed_project_vocab(db, pid)

    source = ProjectSource(project_id=pid, title="测试资料", content="莫凡觉醒了雷系。")
    db.add(source)
    await db.flush()
    sid = str(source.id)
    db.add_all([
        ProjectSourceChunk(project_id=pid, source_id=sid, chunk_index=0,
                           content="莫凡觉醒了雷系。", token_count=10),
        CharacterProfile(project_id=pid, name="张小侯", identity_desc="风系法师",
                         origin="manual", source_priority=100),
        AbilityProfile(project_id=pid, character_name="张小侯",
                       ability_type="magic_element", ability_name="风系",
                       level_desc="初阶", first_seen_chapter=16,
                       origin="manual", source_priority=100),
        EventTimeline(project_id=pid, chapter_no=1, event_title="第1章事件",
                      event_desc="本章主要事件"),
        WorldEntry(project_id=pid, title="圣城", category="location",
                   content="圣城是世界的中心。"),
    ])
    chapter = Chapter(project_id=pid, title="第一章", sequence_number=1, content="觉醒")
    db.add(chapter)
    await db.flush()
    db.add(ChapterVersion(project_id=pid, chapter_id=str(chapter.id), version_number=1,
                          content="第一章\n莫凡觉醒了雷系。", word_count=10))
    await db.commit()

    progress: list[tuple[int, int, str]] = []
    stats = await rebuild_project(db, pid, progress_cb=lambda d, t, s: progress.append((d, t, s)))

    # 六类来源全部落库
    kinds = (
        await db.execute(
            text("SELECT DISTINCT source_kind FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalars().all()
    assert sorted(kinds) == [
        "ability_profile", "chapter_version", "character_profile",
        "source_chunk", "timeline_event", "world_entry",
    ]
    assert stats["failed_rows"] == 0
    # 源行数：1 source + 1 version + 2 profiles(莫凡/张小侯) + 2 abilities + 1 event + 2 world entries
    assert stats["total_rows"] == len(progress) == 9
    assert progress[-1][0] == progress[-1][1] == 9
    assert {stage for _, _, stage in progress} == {
        "source_chunk", "chapter_version", "character_profile",
        "ability_profile", "timeline_event", "world_entry",
    }

    # 检索验证：限定 source_kind 只回对应来源；章节过滤生效
    retriever = _retriever()
    profile_hits = await retriever.search(db, pid, "张小侯", source_kinds=["character_profile"])
    assert profile_hits and profile_hits[0].source_kind == "character_profile"

    ability_hits = await retriever.search(db, pid, "风系", source_kinds=["ability_profile"])
    assert ability_hits and "张小侯" in ability_hits[0].content
    assert ability_hits[0].chapter_no == 16

    ch1 = await retriever.search(db, pid, "莫凡", chapter_num=1)
    assert ch1 and all(h.chapter_no == 1 for h in ch1)
    assert await retriever.search(db, pid, "莫凡", chapter_num=2) == []


@pytest.mark.asyncio
async def test_rebuild_project_progress_and_rebuild_idempotent(db):
    """重建幂等：再次 rebuild 全部跳过（无新 generation），进度回调仍走完。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    source = ProjectSource(project_id=pid, title="资料", content="莫凡觉醒了雷系。")
    db.add(source)
    await db.flush()
    db.add(ProjectSourceChunk(project_id=pid, source_id=str(source.id), chunk_index=0,
                              content="莫凡觉醒了雷系。", token_count=5))
    await db.commit()

    gen_ids_first: set[str] = set()
    progress1: list[tuple[int, int, str]] = []

    async def _collect(db_, pid_, gen_ids: set[str]):
        stats = await rebuild_project(
            db_, pid_, progress_cb=lambda d, t, s: progress1.append((d, t, s))
        )
        rows = (
            await db_.execute(
                text("SELECT index_generation_id FROM retrieval_chunks WHERE project_id = :pid"),
                {"pid": pid_},
            )
        ).scalars().all()
        gen_ids.update(str(g) for g in rows)
        return stats

    stats1 = await _collect(db, pid, gen_ids_first)
    assert stats1["failed_rows"] == 0

    # 第二次重建：全部幂等跳过，不产生新块/新 generation
    # 源行数 = 1 source + 1 profile(莫凡) + 1 ability + 1 world entry = 4
    progress2: list[tuple[int, int, str]] = []
    stats2 = await rebuild_project(db, pid, progress_cb=lambda d, t, s: progress2.append((d, t, s)))
    assert stats2["failed_rows"] == 0
    assert stats2["total_rows"] == len(progress2) == 4
    gen_ids_second: set[str] = set(
        str(g) for g in (
            await db.execute(
                text("SELECT index_generation_id FROM retrieval_chunks WHERE project_id = :pid"),
                {"pid": pid},
            )
        ).scalars().all()
    )
    assert gen_ids_second == gen_ids_first  # 没有新增 generation


# ── P0：同一章节连续定稿 → 稳定 source_id 替换（不新旧并存） ──

@pytest.mark.asyncio
async def test_refinalize_same_chapter_replaces_old_version(db):
    """同一章节连续定稿两次：同 source_id（chapter.id）下新 generation 替换旧块
    （指针切换 + 物理双删），检索只命中最新版正文，旧版专属词不再可召回。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)

    chapter = Chapter(project_id=pid, title="第一章 觉醒", sequence_number=1,
                      content="旧版")
    db.add(chapter)
    await db.flush()
    cid = str(chapter.id)
    v1 = ChapterVersion(project_id=pid, chapter_id=cid, version_number=1,
                        content="第一章 觉醒\n莫凡觉醒了雷系。", word_count=20)
    v2 = ChapterVersion(project_id=pid, chapter_id=cid, version_number=2,
                        content="第一章 觉醒\n莫凡觉醒了火系。", word_count=20)
    db.add_all([v1, v2])
    await db.commit()

    # 第一次定稿：v1 可检索
    gid1 = await index_chapter_version(db, pid, str(v1.id))
    assert gid1 is not None
    assert any("雷系" in h.content for h in await _retriever().search(db, pid, "雷系"))

    # 第二次定稿（新 version.id，内容变化）：同 source_id 下重建
    gid2 = await index_chapter_version(db, pid, str(v2.id))
    assert gid2 is not None and gid2 != gid1

    # 指针切到新 generation；旧 generation 标记 superseded
    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == pid,
                RetrievalIndexSourceState.source_kind == "chapter_version",
                RetrievalIndexSourceState.source_id == cid,
            )
        )
    ).scalar_one()
    assert state.active_generation_id == gid2
    old_gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == gid1
            )
        )
    ).scalar_one()
    assert old_gen.status == "superseded"

    # 该 source_id 只剩新 generation 的块（旧块已双删）
    remaining_gids = (
        await db.execute(
            text(
                "SELECT DISTINCT index_generation_id FROM retrieval_chunks "
                "WHERE project_id = :pid AND source_kind = 'chapter_version' "
                "AND source_id = :sid"
            ),
            {"pid": pid, "sid": cid},
        )
    ).scalars().all()
    assert [str(g) for g in remaining_gids] == [gid2]

    # 检索只命中最新版：旧版专属词「雷系」不再可召回，新版「火系」命中
    assert await _retriever().search(db, pid, "雷系") == []
    hits = await _retriever().search(db, pid, "莫凡", chapter_num=1)
    assert hits and all("火系" in h.content for h in hits)
    assert all("雷系" not in h.content for h in hits)


@pytest.mark.asyncio
async def test_index_chapter_version_skips_when_chapter_missing(db, caplog):
    """version 无对应 chapter（防御）：logger.warning 并跳过，返回 None。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)
    orphan = ChapterVersion(
        project_id=pid, chapter_id=str(uuid.uuid4()), version_number=1,
        content="无主版本内容", word_count=5,
    )
    db.add(orphan)
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="services.retrieval_indexer"):
        assert await index_chapter_version(db, pid, str(orphan.id)) is None
    assert any("无对应 Chapter" in rec.message for rec in caplog.records)
    assert (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM retrieval_chunks "
                "WHERE project_id = :pid AND source_kind = 'chapter_version'"
            ),
            {"pid": pid},
        )
    ).scalar() == 0


# ── P0：索引失败 savepoint 隔离，不提前提交事务 ───────────

@pytest.mark.asyncio
async def test_index_failure_does_not_commit_outer_transaction(db, monkeypatch):
    """索引写入失败：savepoint 回滚 + generation 标记 failed，索引器全程不 commit，
    外层事务仍可正常继续提交（定稿数据与 failed 审计记录一并落库）。"""
    import services.retrieval_indexer as ri

    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)
    chapter = Chapter(project_id=pid, title="第一章", sequence_number=1, content="草稿")
    db.add(chapter)
    await db.flush()
    version = ChapterVersion(
        project_id=pid, chapter_id=str(chapter.id), version_number=1,
        content="第一章\n莫凡觉醒了雷系。", word_count=12,
    )
    db.add(version)
    await db.commit()

    # spy：索引流程中 commit 不应被调用（事务边界完全由调用方控制）
    commits: list[str] = []
    orig_commit = db.commit

    async def _spy_commit(*args, **kwargs):
        commits.append("commit")
        return await orig_commit(*args, **kwargs)

    monkeypatch.setattr(db, "commit", _spy_commit)

    async def _boom(*args, **kwargs):
        raise RuntimeError("索引写入失败")

    monkeypatch.setattr(ri, "_insert_chunks", _boom)

    with pytest.raises(RuntimeError, match="索引写入失败"):
        await index_chapter_version(db, pid, str(version.id))

    # 1) 索引器从未 commit 外层事务
    assert commits == []
    # 2) generation 标记 failed（在外层事务内，尚未提交）
    gens = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )
    ).scalars().all()
    assert len(gens) == 1 and gens[0].status == "failed"
    # 3) 无任何块残留（savepoint 已回滚索引写入）
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar() == 0
    # 4) 外层事务仍可正常继续并提交
    extra = Chapter(project_id=pid, title="第二章", sequence_number=2, content="续写")
    db.add(extra)
    await db.commit()
    assert await db.get(Chapter, extra.id) is not None
    # 提交后：业务数据与 failed 审计记录一并落库
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )
    ).scalar_one()
    assert gen.status == "failed"


@pytest.mark.asyncio
async def test_index_failure_then_outer_rollback_stays_consistent(db, monkeypatch):
    """索引失败后外层事务回滚：未提交的业务数据与索引痕迹（含 failed 标记）
    一并撤销，会话恢复可用（等价真实定稿事务：版本 flush 未提交即触发索引）。"""
    import services.retrieval_indexer as ri

    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)
    chapter = Chapter(project_id=pid, title="第一章", sequence_number=1, content="草稿")
    db.add(chapter)
    await db.flush()
    version = ChapterVersion(
        project_id=pid, chapter_id=str(chapter.id), version_number=1,
        content="第一章\n莫凡觉醒了雷系。", word_count=12,
    )
    db.add(version)
    await db.flush()  # 不 commit：与 finalize 定稿流程一致，业务数据尚未提交

    async def _boom(*args, **kwargs):
        raise RuntimeError("索引写入失败")

    monkeypatch.setattr(ri, "_insert_chunks", _boom)

    with pytest.raises(RuntimeError):
        await index_chapter_version(db, pid, str(version.id))

    # 外层事务回滚：业务行、generation、块全部撤销（事务边界在调用方）
    await db.rollback()
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar() == 0
    assert (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )
    ).scalars().all() == []
    assert (
        await db.execute(select(Chapter).where(Chapter.project_id == pid))
    ).scalars().all() == []
    assert (
        await db.execute(select(ChapterVersion).where(ChapterVersion.project_id == pid))
    ).scalars().all() == []

    # 会话恢复可用：重新写入并提交
    ch2 = Chapter(project_id=pid, title="第一章", sequence_number=1, content="新草稿")
    db.add(ch2)
    await db.commit()
    assert (
        await db.execute(select(Chapter).where(Chapter.project_id == pid))
    ).scalars().one().id == ch2.id


@pytest.mark.asyncio
async def test_finalize_chapter_content_survives_index_failure(db, monkeypatch, caplog):
    """章节定稿流程（chapter_save.finalize_chapter_content）遇索引失败不抛异常：
    定稿状态照常生效，索引 generation 标记 failed，事务仍由调用方统一提交。"""
    from services.chapter_save import finalize_chapter_content
    import services.retrieval_indexer as ri

    pid = _pid()
    await _seed_project_vocab(db, pid)
    await _build_vocab(db, pid)
    chapter = Chapter(project_id=pid, title="第一章 觉醒", sequence_number=1,
                      content="草稿")
    db.add(chapter)
    await db.commit()

    commits: list[str] = []
    orig_commit = db.commit

    async def _spy_commit(*args, **kwargs):
        commits.append("commit")
        return await orig_commit(*args, **kwargs)

    monkeypatch.setattr(db, "commit", _spy_commit)

    async def _boom(*args, **kwargs):
        raise RuntimeError("索引写入失败")

    monkeypatch.setattr(ri, "_insert_chunks", _boom)

    with caplog.at_level(logging.WARNING, logger="services.chapter_save"):
        result = await finalize_chapter_content(
            db, chapter, "第一章 觉醒\n莫凡觉醒了雷系。"
        )

    # 定稿不受影响（D2：索引失败不回滚章节）
    assert result.status == "final"
    assert result.final_version_id is not None
    version = await db.get(ChapterVersion, result.final_version_id)
    assert version is not None and "雷系" in version.content
    assert any("检索索引触发失败" in rec.message for rec in caplog.records)

    # 定稿流程全程未 commit（事务边界在路由层）
    assert commits == []
    # 索引 generation 已标记 failed 且无块残留
    gens = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )
    ).scalars().all()
    assert len(gens) == 1 and gens[0].status == "failed"
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = :pid"),
            {"pid": pid},
        )
    ).scalar() == 0

    # 调用方提交：定稿 + failed 审计记录一并落库
    await db.commit()
    assert (
        await db.execute(select(Chapter).where(Chapter.id == chapter.id))
    ).scalar_one().status == "final"
    assert (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.project_id == pid
            )
        )
    ).scalar_one().status == "failed"
