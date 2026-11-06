"""知识问答检索链路接入 LexicalRetriever 的分发测试（阶段一启用）。

验证 search_project_knowledge 的检索分发（settings.RETRIEVAL_MODE flag）：
- ilike：完全走原 ILIKE 逻辑（分发函数不被调用，行为不变）；
- lexical：项目存在检索索引（retrieval_chunks）时走 LexicalRetriever，
  RetrievalHit 映射为 knowledge_source 同构结果格式，且
  source_kinds / entity_first 按 retrieval_policy.get_retrieval_policy(intent)
  传入（D5 集中映射）；
- hybrid：lexical + ILIKE 合并去重（lexical 优先，按 chunk_id 去重）；
- 降级：索引缺失 / LexicalRetriever 抛异常 / lexical 无命中且策略要求回退时，
  回退 ILIKE 原逻辑（记日志，不阻断问答）。

知识问答主流程依赖 LLM，这里只测「检索分发」层（即 search_project_knowledge
及其计划级入口 search_project_knowledge_with_plan），不测完整问答。
"""

import logging
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.retrieval_index import RetrievalChunk  # noqa: F401  注册 retrieval_chunks 表
from models.retrieval_vector import RetrievalVector  # noqa: F401  注册 retrieval_vectors 表
from models.structured_knowledge import CharacterAliasCluster, CharacterProfile
from services.lexical_retriever import LexicalRetriever, RetrievalHit
from services.retrieval_indexer import clear_indexer_cache, ensure_fts_table, index_source_chunks
from services.retrieval_tokenizer import clear_tokenizer_cache


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


@pytest.fixture
def retrieval_mode():
    """切换 settings.RETRIEVAL_MODE（frozen dataclass 需用 object.__setattr__）。"""
    from config.settings import settings

    def _set(mode: str) -> None:
        object.__setattr__(settings, "RETRIEVAL_MODE", mode)

    yield _set
    object.__setattr__(settings, "RETRIEVAL_MODE", "ilike")


# ── 测试辅助 ─────────────────────────────────────────────

INDEXED_CONTENT = "莫凡觉醒了雷系。他是主角。"
UNINDEXED_CONTENT = "莫凡与叶心夏共同创立凡雪山，既是事业搭档，也是恋人关系。"


async def _seed_project(
    db: AsyncSession,
    *,
    indexed_content: str = INDEXED_CONTENT,
    unindexed_content: str | None = UNINDEXED_CONTENT,
) -> tuple[str, str]:
    """种子项目：人物词表 + 已索引资料 + 未索引资料。

    返回 (project_id, indexed_source_id)。
    - 已索引资料：ProjectSourceChunk 已投影到 retrieval_chunks + fts，
      ILIKE 与 lexical 都能召回；
    - 未索引资料：只有 ILIKE 能召回（验证 lexical 路径未回落到 ILIKE）。
    """
    pid = str(uuid.uuid4())
    db.add(CharacterProfile(project_id=pid, name="莫凡",
                            identity_desc="法师 觉醒", origin="manual", source_priority=100))
    # 已确认别名簇：entity_map 只从 character_alias_clusters 生成，
    # 是 LexicalRetriever 实体优先通道（normalize_entities）的数据来源。
    db.add(CharacterAliasCluster(project_id=pid, canonical_name="莫凡",
                                 aliases=["小莫"], source="manual"))
    await db.flush()

    source = ProjectSource(project_id=pid, title="莫凡资料", content=indexed_content)
    db.add(source)
    await db.flush()
    sid = str(source.id)
    db.add(ProjectSourceChunk(project_id=pid, source_id=sid, chunk_index=0,
                              content=indexed_content, token_count=20))

    if unindexed_content:
        source2 = ProjectSource(project_id=pid, title="未索引资料", content=unindexed_content)
        db.add(source2)
        await db.flush()
        db.add(ProjectSourceChunk(project_id=pid, source_id=str(source2.id), chunk_index=0,
                                  content=unindexed_content, token_count=20))
    await db.commit()

    gid = await index_source_chunks(db, pid, sid)
    assert gid is not None, "索引写入失败"
    return pid, sid


async def _ilike_chunk_ids(db: AsyncSession, pid: str) -> list[str]:
    """ILIKE 视角的 chunk id（ProjectSourceChunk.id）。"""
    result = await db.execute(
        select(ProjectSourceChunk.id).where(ProjectSourceChunk.project_id == pid)
    )
    return [str(c) for c in result.scalars().all()]


async def _ilike_chunk_ids_by_content(db: AsyncSession, pid: str) -> dict[str, str]:
    """content → ProjectSourceChunk.id 映射（供按内容定位 chunk）。"""
    result = await db.execute(
        select(ProjectSourceChunk.content, ProjectSourceChunk.id)
        .where(ProjectSourceChunk.project_id == pid)
    )
    return {content: str(cid) for content, cid in result.all()}


def _fake_hit(chunk_id: str = "c-fake", source_id: str = "src-fake",
              source_kind: str = "source_chunk", content: str = INDEXED_CONTENT,
              score: float = 12.0, title: str = "莫凡资料") -> RetrievalHit:
    """构造一条 fake RetrievalHit（等价 LexicalRetriever.search 的返回值）。"""
    return RetrievalHit(
        chunk_id=chunk_id, project_id="p", source_kind=source_kind,
        source_id=source_id, source_version_id=None, chapter_no=None,
        title=title, content=content, snippet=content[:300], score=score, rank=1,
    )


# ── RETRIEVAL_MODE 配置 ──────────────────────────────────

def test_retrieval_mode_default_is_ilike():
    """默认 ilike：未显式配置时行为与阶段零完全一致。"""
    from config.settings import settings

    assert settings.RETRIEVAL_MODE == "ilike"


# ── ilike 模式：原逻辑不变 ───────────────────────────────

@pytest.mark.asyncio
async def test_ilike_mode_keeps_original_behavior(db, retrieval_mode, monkeypatch):
    """ilike 模式：分发函数不被调用，未索引资料也能被 ILIKE 召回。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("ilike")

    # 分发函数若被调用即失败（ilike 模式不应进入分发）
    async def _forbidden(*args, **kwargs):
        raise AssertionError("ilike 模式不应调用检索分发函数")

    monkeypatch.setattr(knowledge_source, "_dispatch_lexical_or_ilike", _forbidden)

    results = await knowledge_source.search_project_knowledge(
        db, pid, "莫凡", limit=10,
    )
    assert results, "ILIKE 应有结果"
    # ILIKE 视角的 chunk（含未索引资料）全部可被召回
    ilike_ids = await _ilike_chunk_ids(db, pid)
    result_ids = {r["chunk_id"] for r in results}
    assert result_ids <= set(ilike_ids)
    assert any("凡雪山" in r["snippet"] for r in results), "未索引资料应被 ILIKE 召回"


# ── lexical 模式：走 LexicalRetriever ────────────────────

@pytest.mark.asyncio
async def test_lexical_mode_dispatches_with_policy_params(db, retrieval_mode, monkeypatch):
    """lexical 模式：按 retrieval_policy(intent) 传参调用 LexicalRetriever.search。

    intent="qa" → qa 策略（资料切片 source_chunk + 实体优先，领域隔离）；
    intent 未知 → generate 兜底（实体优先关闭）。
    limit 沿用调用方 limit（RetrievalPolicy 无 flat limit 字段）。
    """
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("lexical")

    calls: list[dict] = []

    async def fake_search(self, db_, project_id, query, *, source_kinds=None,
                          limit=10, entity_first=True, chapter_num=None):
        calls.append({
            "query": query, "source_kinds": source_kinds,
            "limit": limit, "entity_first": entity_first, "chapter_num": chapter_num,
        })
        return [_fake_hit()]

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", fake_search)

    results = await knowledge_source.search_project_knowledge(
        db, pid, "莫凡是什么系的", intent="qa", limit=7,
    )
    assert calls, "lexical 模式应调用 LexicalRetriever.search"
    assert calls[0]["query"] == "莫凡是什么系的"
    # qa 策略：只检索资料切片（source_chunk，投影命名）+ 实体优先
    assert calls[0]["source_kinds"] == ["source_chunk"]
    assert calls[0]["entity_first"] is True
    assert calls[0]["limit"] == 7
    assert calls[0]["chapter_num"] is None
    assert results, "应返回 fake 命中映射结果"

    # 未知 intent → 回退 generate 策略：实体优先关闭。
    # 注意：V1 的 ability 等问答类 intent 阶段二起已登记到 qa 策略
    # （entity_first=True、on_no_result="log"），不再走 generate 兜底，
    # 因此这里用真正未登记的 intent 验证兜底语义。
    await knowledge_source.search_project_knowledge(db, pid, "莫凡是什么系的", intent="no_such_intent")
    assert calls[-1]["entity_first"] is False, "generate 兜底策略 entity_first 应为 False"


@pytest.mark.asyncio
async def test_lexical_mode_maps_hits_to_ilike_compatible_format(db, retrieval_mode, monkeypatch):
    """RetrievalHit → knowledge_source 同构结果格式（下游分类/重排无需改动）。"""
    from services import knowledge_source

    pid, sid = await _seed_project(db)
    retrieval_mode("lexical")

    hit = _fake_hit(chunk_id="c-lex", source_id=sid, source_kind="source_chunk",
                    content="莫凡觉醒了雷系。他是主角。", title="莫凡资料")

    async def fake_search(self, db_, project_id, query, **kw):
        return [hit]

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", fake_search)

    results = await knowledge_source.search_project_knowledge(db, pid, "莫凡 雷系", limit=5)
    assert len(results) == 1
    r = results[0]
    # 与 ILIKE 结果同构的字段
    assert r["source_kind"] == "project_source_chunk"  # source_chunk → 约定命名
    assert r["source_id"] == sid
    assert r["chunk_id"] == "c-lex"
    assert r["title"] == "莫凡资料"
    assert "雷系" in r["snippet"]
    assert r["score"] == 12.0
    assert r["matched_query"] in ("莫凡", "雷系")


@pytest.mark.asyncio
async def test_lexical_mode_uses_real_retriever_when_index_present(db, retrieval_mode):
    """lexical 模式端到端：真实 LexicalRetriever + 真实检索索引。

    用 intent="qa"（qa 策略 entity_first=True，走实体优先通道，命中确定；
    未知 intent 走 generate 兜底 entity_first=False，词法通道无命中时会被
    策略 on_no_result="fallback" 拉回 ILIKE——见计划级测试）。
    已索引 chunk 被召回且映射为约定 source_kind；未索引资料不应出现
    （证明没有回落到 ILIKE）。
    """
    from services import knowledge_source

    pid, sid = await _seed_project(db)
    retrieval_mode("lexical")

    results = await knowledge_source.search_project_knowledge(
        db, pid, "莫凡是什么系的", intent="qa", limit=5,
    )
    assert results, "词法检索应有命中"
    assert results[0]["source_kind"] == "project_source_chunk"
    assert results[0]["source_id"] == sid
    assert any("雷系" in r["snippet"] for r in results)
    # 未索引资料只在 ILIKE 视角存在，lexical 模式下不应出现
    assert all("凡雪山" not in r["snippet"] for r in results), "lexical 模式不应回落到 ILIKE"


@pytest.mark.asyncio
async def test_lexical_mode_with_source_type_falls_back_to_ilike(db, retrieval_mode, monkeypatch):
    """source_type 过滤是 ILIKE 独有能力，lexical 模式下直接走 ILIKE。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("lexical")

    async def _forbidden(*args, **kwargs):
        raise AssertionError("source_type 过滤不应走 lexical 检索")

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", _forbidden)

    results = await knowledge_source.search_project_knowledge(
        db, pid, "莫凡", source_type="upload", limit=5,
    )
    assert results, "source_type 过滤应回退 ILIKE 并返回结果"


# ── hybrid 模式：合并去重 ────────────────────────────────

@pytest.mark.asyncio
async def test_hybrid_mode_rrf_fusion_keeps_lexical_hit(db, retrieval_mode, monkeypatch):
    """hybrid：三通道 RRF 融合（实体/词法/向量），lexical 命中进入融合结果。

    决策 D4 起 hybrid 语义从"lexical + ILIKE 合并"升级为三通道 RRF——
    索引内融合，保留词法命中（snippet/score 来自命中本身）；
    融合无命中/异常才回退 ILIKE（见 test_hybrid_mode_includes_ilike_when_lexical_empty）。
    """
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("hybrid")

    ids_by_content = await _ilike_chunk_ids_by_content(db, pid)
    indexed_chunk_id = ids_by_content[INDEXED_CONTENT]  # 已索引 chunk（词法通道命中）

    # fake lexical 命中：词法通道返回该 chunk → RRF 融合后保留其内容
    hit = _fake_hit(chunk_id=indexed_chunk_id, source_id="src-lex",
                    content="莫凡觉醒了雷系（lexical 版本）。", score=50.0)

    async def fake_search(self, db_, project_id, query, **kw):
        return [hit]

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", fake_search)

    results = await knowledge_source.search_project_knowledge(db, pid, "莫凡", limit=10)
    assert results, "hybrid 融合应有命中"
    assert results[0]["chunk_id"] == indexed_chunk_id
    assert "lexical 版本" in results[0]["snippet"], "词法命中内容应保留在融合结果中"


@pytest.mark.asyncio
async def test_hybrid_mode_includes_ilike_when_lexical_empty(db, retrieval_mode, monkeypatch):
    """hybrid：lexical 无命中时，合并结果 = ILIKE 结果（不丢召回）。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("hybrid")

    async def fake_search(self, db_, project_id, query, **kw):
        return []

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", fake_search)

    results = await knowledge_source.search_project_knowledge(db, pid, "莫凡", limit=10)
    ilike_ids = await _ilike_chunk_ids(db, pid)
    assert results
    assert {r["chunk_id"] for r in results} == set(ilike_ids)


# ── 降级路径 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lexical_mode_falls_back_when_index_missing(db, retrieval_mode, caplog):
    """项目无检索索引：回退 ILIKE 原逻辑并记 warning，不调用检索器。"""
    from services import knowledge_source

    # 只种 ILIKE 数据，不写 retrieval_chunks 索引
    pid = str(uuid.uuid4())
    source = ProjectSource(project_id=pid, title="莫凡资料", content=INDEXED_CONTENT)
    db.add(source)
    await db.flush()
    db.add(ProjectSourceChunk(project_id=pid, source_id=str(source.id), chunk_index=0,
                              content=INDEXED_CONTENT, token_count=20))
    await db.commit()

    retrieval_mode("lexical")
    with caplog.at_level(logging.WARNING, logger="services.knowledge_source"):
        results = await knowledge_source.search_project_knowledge(db, pid, "莫凡", limit=5)

    assert results, "索引缺失应回退 ILIKE 并返回结果"
    assert any("无检索索引" in rec.message for rec in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_lexical_mode_falls_back_on_retriever_exception(db, retrieval_mode, caplog, monkeypatch):
    """LexicalRetriever 抛异常：回退 ILIKE 原逻辑并记 warning。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("lexical")

    async def broken_search(self, db_, project_id, query, **kw):
        raise RuntimeError("indexer boom")

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", broken_search)

    with caplog.at_level(logging.WARNING, logger="services.knowledge_source"):
        results = await knowledge_source.search_project_knowledge(db, pid, "莫凡", limit=5)

    assert results, "lexical 异常应回退 ILIKE"
    assert any("lexical 检索失败" in rec.message for rec in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_lexical_empty_with_fallback_policy_degrades_to_ilike(db, retrieval_mode, caplog, monkeypatch):
    """lexical 无命中且策略 on_no_result == "fallback"（未知 intent 兜底）：
    回退 ILIKE 原逻辑。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("lexical")

    async def empty_search(self, db_, project_id, query, **kw):
        return []

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", empty_search)

    with caplog.at_level(logging.INFO, logger="services.knowledge_source"):
        results = await knowledge_source.search_project_knowledge(db, pid, "莫凡", limit=5)

    assert results, "fallback 策略下 lexical 无命中应转 ILIKE"
    ilike_ids = await _ilike_chunk_ids(db, pid)
    assert {r["chunk_id"] for r in results} <= set(ilike_ids)
    assert any("lexical 无命中且策略要求回退" in rec.message for rec in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_lexical_empty_with_log_policy_returns_empty(db, retrieval_mode, monkeypatch):
    """lexical 无命中且策略 on_no_result == "log"（qa 策略）：返回空，不回落 ILIKE。"""
    from services import knowledge_source

    pid, _ = await _seed_project(db)
    retrieval_mode("lexical")

    async def empty_search(self, db_, project_id, query, **kw):
        return []

    monkeypatch.setattr("services.lexical_retriever.LexicalRetriever.search", empty_search)

    # ILIKE 若被调用即失败（qa 策略不应回落）
    async def _forbidden(*args, **kwargs):
        raise AssertionError("log 策略下 lexical 无命中不应回退 ILIKE")

    monkeypatch.setattr(knowledge_source, "_search_project_knowledge_ilike", _forbidden)

    results = await knowledge_source.search_project_knowledge(
        db, pid, "莫凡", intent="qa", limit=5,
    )
    assert results == []


# ── 计划级入口（search_project_knowledge_with_plan） ──────

@pytest.mark.asyncio
async def test_plan_level_dispatch_through_with_plan(db, retrieval_mode):
    """知识问答检索链路（search_project_knowledge_with_plan）在 lexical 模式下
    分发到词法检索：chunk 结果来自检索索引并映射为约定格式。

    计划级传入 V1 intent（如 ability）→ 阶段二起已登记到 qa 策略
    （entity_first=True、on_no_result="log"），实体优先通道优先召回，
    无命中时不回退 ILIKE；断言保持宽松：结果非空、全部为
    project_source_chunk 约定格式、至少一条来自检索索引对应 source。"""
    from services import knowledge_source
    from services.knowledge_query_plan import build_knowledge_query_plan

    pid, sid = await _seed_project(db)
    retrieval_mode("lexical")

    plan = build_knowledge_query_plan("莫凡是什么系的", project_id=pid)
    assert plan.search_queries, "规则 planner 应产出检索词"

    structured, chunks = await knowledge_source.search_project_knowledge_with_plan(
        db, pid, plan, limit=10,
    )
    # 结构化表未种数据 → 空；chunk 走检索分发
    assert structured == []
    assert chunks, "计划级检索应有命中"
    assert all(c["source_kind"] == "project_source_chunk" for c in chunks), (
        f"chunk 结果应为约定格式，实际: {[c['source_kind'] for c in chunks]}"
    )
    assert any(c["source_id"] == sid for c in chunks), "应至少有一条来自检索索引的命中"
    assert any("雷系" in c["snippet"] for c in chunks)
