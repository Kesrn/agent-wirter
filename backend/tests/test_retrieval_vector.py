"""阶段二向量检索层单元测试：embedding provider / RetrievalVector / VectorRetriever / RRF。

每个测试独立使用 SQLite 内存库（StaticPool 共享同一连接）：
Base.metadata.create_all（含 retrieval_chunks / retrieval_vectors / 业务种子表）
+ ensure_fts_table（供词法通道与 chapter_no 回查）→ 写入中文测试块 → 索引向量 →
验证 VectorRetriever.search 命中相关块；另覆盖 embedding 确定性与归一化、
RetrievalVector CRUD 与唯一约束、rrf_merge 去重/排序/权重。
"""

import hashlib
import math
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.retrieval_index import RetrievalChunk
from models.retrieval_vector import RetrievalVector
from models.structured_knowledge import (
    AbilityProfile,
    CharacterAliasCluster,
    CharacterProfile,
)
from models.world_entry import WorldEntry
from services.embedding_provider import (
    LocalEmbeddingProvider,
    clear_provider_cache,
    extract_ngrams,
    get_embedding,
)
from services.lexical_retriever import RetrievalHit
from services.retrieval_reranker import rrf_merge, search_hybrid
from services.retrieval_tokenizer import (
    build_project_vocabulary,
    clear_tokenizer_cache,
    get_project_tokenizer,
)
from services.vector_retriever import VectorRetriever, index_project_vectors


@pytest_asyncio.fixture
async def db():
    """独立的 SQLite 内存库会话（含 FTS5 虚拟表）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from services.retrieval_indexer import ensure_fts_table

        await ensure_fts_table(conn)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例前后清空分词器/索引器/provider 缓存，避免测试间串扰。"""
    from services.retrieval_indexer import clear_indexer_cache

    clear_tokenizer_cache()
    clear_indexer_cache()
    clear_provider_cache()
    yield
    clear_tokenizer_cache()
    clear_indexer_cache()
    clear_provider_cache()


def _pid() -> str:
    return str(uuid.uuid4())


def _chunk_id() -> str:
    return str(uuid.uuid4())


def _sha(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # 单位向量：点积即余弦


# ── 测试辅助 ─────────────────────────────────────────────

async def _insert_chunk(
    db: AsyncSession,
    pid: str,
    *,
    chunk_id: str,
    content: str,
    source_kind: str = "source_chunk",
    source_id: str,
    chapter_no=None,
    title: str = "",
    search_text: str | None = None,
    content_hash: str | None = None,
) -> None:
    """写入一条检索块（retrieval_chunks ORM + fts 行，同一事务）。"""
    chunk = RetrievalChunk(
        id=chunk_id,
        project_id=pid,
        source_kind=source_kind,
        source_id=source_id,
        source_version_id=None,
        index_generation_id="test-gen",
        chunk_index=0,
        content=content,
        search_text=search_text if search_text is not None else content,
        content_hash=content_hash or _sha(content),
    )
    db.add(chunk)
    await db.flush()
    await db.execute(
        text(
            "INSERT INTO retrieval_chunks_fts (chunk_id, project_id, source_kind, "
            "source_id, chapter_no, title, content, search_text) VALUES "
            "(:id, :pid, :sk, :sid, :cn, :title, :content, :st)"
        ),
        {
            "id": chunk_id, "pid": pid, "sk": source_kind, "sid": source_id,
            "cn": chapter_no, "title": title, "content": content,
            "st": search_text if search_text is not None else content,
        },
    )
    await db.commit()


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


def _hit(chunk_id: str, source_id: str = "src-1") -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id, project_id="p1", source_kind="source_chunk",
        source_id=source_id, source_version_id=None, chapter_no=None,
        title="", content="", snippet="", score=1.0, rank=0,
    )


# ── embedding：确定性 / 归一化 / 词面相似 ────────────────

@pytest.mark.asyncio
async def test_local_embedding_deterministic_and_normalized():
    """同文本同向量；向量为单位向量（L2 归一化）；维度固定。"""
    prov = LocalEmbeddingProvider(dimension=128)
    prov.set_corpus(["莫凡觉醒了雷系。", "张小侯使用了风之力。", "圣城是世界的中心。"])
    v1 = await prov.embed("莫凡的雷系是什么等级？")
    v2 = await prov.embed("莫凡的雷系是什么等级？")
    assert v1 == v2, "同文本必须产出完全相同的向量"
    assert len(v1) == 128
    norm = math.sqrt(sum(x * x for x in v1))
    assert abs(norm - 1.0) < 1e-9, f"向量应归一化，实际 norm={norm}"
    # 空文本 → 全零向量（确定、不崩溃）
    zero = await prov.embed("")
    assert all(x == 0.0 for x in zero)


@pytest.mark.asyncio
async def test_local_embedding_ngram_similarity():
    """词面重叠可量化：共享专名（莫凡/雷系）的文本余弦高于无关文本。"""
    prov = LocalEmbeddingProvider(dimension=256)
    prov.set_corpus(["莫凡觉醒了雷系。", "张小侯使用了风之力。", "圣城是世界的中心。"])
    a = await prov.embed("莫凡觉醒了雷系")
    b = await prov.embed("莫凡的雷系魔法")
    c = await prov.embed("张小侯的风之轨迹")
    assert _cosine(a, b) > _cosine(a, c), "共享 n-gram 越多余弦越高"


@pytest.mark.asyncio
async def test_get_embedding_module_level():
    """模块级 get_embedding：确定性 + 维度落在 [64, 512]（本地上限）。"""
    e1 = await get_embedding("测试文本 莫凡 雷系")
    e2 = await get_embedding("测试文本 莫凡 雷系")
    assert e1 == e2
    assert 64 <= len(e1) <= 512
    norm = math.sqrt(sum(x * x for x in e1))
    assert abs(norm - 1.0) < 1e-9


def test_extract_ngrams():
    grams = extract_ngrams("莫凡觉醒")
    assert "莫凡" in grams and "凡觉" in grams and "觉醒" in grams
    assert "莫凡觉" in grams and "凡觉醒" in grams
    assert "莫" in grams and "凡" in grams
    # 空白切块 + 纯标点窗口过滤
    assert "，。！" not in extract_ngrams("你好，世界！")


# ── RetrievalVector：CRUD + 唯一约束 ─────────────────────

@pytest.mark.asyncio
async def test_retrieval_vector_crud(db):
    pid = _pid()
    row = RetrievalVector(
        project_id=pid, chunk_id=_chunk_id(),
        embedding=[0.1, 0.2, 0.3], dim=3, content_hash="h1",
    )
    db.add(row)
    await db.commit()

    loaded = (
        await db.execute(select(RetrievalVector).where(RetrievalVector.project_id == pid))
    ).scalar_one()
    assert loaded.embedding == [0.1, 0.2, 0.3]
    assert loaded.dim == 3
    assert loaded.chunk_id == row.chunk_id
    assert loaded.created_at is not None

    loaded.embedding = [0.4, 0.5, 0.6]
    await db.commit()
    again = (
        await db.execute(select(RetrievalVector).where(RetrievalVector.id == row.id))
    ).scalar_one()
    assert again.embedding == [0.4, 0.5, 0.6]


@pytest.mark.asyncio
async def test_retrieval_vector_unique_project_chunk(db):
    """(project_id, chunk_id) 唯一约束：重复写入 IntegrityError。"""
    pid = _pid()
    cid = _chunk_id()
    db.add(RetrievalVector(project_id=pid, chunk_id=cid, embedding=[0.1] * 4, dim=4, content_hash="h1"))
    await db.commit()
    db.add(RetrievalVector(project_id=pid, chunk_id=cid, embedding=[0.9] * 4, dim=4, content_hash="h2"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    # 同 chunk 不同 project 不冲突
    db.add(RetrievalVector(project_id=_pid(), chunk_id=cid, embedding=[0.2] * 4, dim=4, content_hash="h3"))
    await db.commit()


# ── 向量索引：幂等 / 变更重算 / 删除清理 ─────────────────

@pytest.mark.asyncio
async def test_index_project_vectors_and_search(db):
    """索引后向量检索命中相关 chunk，回表字段正确。"""
    pid = _pid()
    prov = LocalEmbeddingProvider(dimension=128)
    c_a, c_b, c_c = _chunk_id(), _chunk_id(), _chunk_id()
    await _insert_chunk(db, pid, chunk_id=c_a, source_id="src-a", content="莫凡觉醒了雷系。他是主角。")
    await _insert_chunk(db, pid, chunk_id=c_b, source_id="src-b", content="张小侯是风系法师，来自博城。")
    await _insert_chunk(db, pid, chunk_id=c_c, source_id="src-c", content="圣城是魔法世界的中心。")

    stats = await index_project_vectors(db, pid, provider=prov)
    assert stats == {"total": 3, "indexed": 3, "skipped": 0, "removed": 0}

    retriever = VectorRetriever(provider=prov)
    hits = await retriever.search(db, pid, "莫凡的雷系", limit=5)
    assert hits, "有向量数据时应当有命中"
    assert hits[0].chunk_id == c_a, "共享 莫凡/雷系 的块应排第一"
    assert hits[0].rank == 1
    assert hits[0].source_id == "src-a"
    assert hits[0].source_kind == "source_chunk"
    assert hits[0].project_id == pid
    assert hits[0].content == "莫凡觉醒了雷系。他是主角。"
    assert hits[0].snippet
    assert hits[0].score > 0
    assert len(hits) == 3

    # 限定 source_kind
    kinds = await retriever.search(db, pid, "莫凡的雷系", source_kinds=["chapter_version"])
    assert kinds == []


@pytest.mark.asyncio
async def test_index_project_vectors_idempotent_and_reindex(db):
    """内容未变跳过（幂等）；内容变化重算更新 content_hash。"""
    pid = _pid()
    prov = LocalEmbeddingProvider(dimension=128)
    c_a = _chunk_id()
    await _insert_chunk(db, pid, chunk_id=c_a, source_id="src-a", content="莫凡觉醒了雷系。")
    await index_project_vectors(db, pid, provider=prov)

    stats2 = await index_project_vectors(db, pid, provider=prov)
    assert stats2["indexed"] == 0 and stats2["skipped"] == 1

    chunk = (
        await db.execute(select(RetrievalChunk).where(RetrievalChunk.id == c_a))
    ).scalar_one()
    chunk.content = "莫凡觉醒了火系。"
    chunk.content_hash = _sha(chunk.content)
    await db.commit()

    stats3 = await index_project_vectors(db, pid, provider=prov)
    assert stats3["indexed"] == 1 and stats3["skipped"] == 0
    vec = (
        await db.execute(select(RetrievalVector).where(RetrievalVector.project_id == pid))
    ).scalar_one()
    assert vec.content_hash == _sha("莫凡觉醒了火系。")

    # 新内容命中：查询火系相关的块排前
    hits = await VectorRetriever(provider=prov).search(db, pid, "莫凡的火系", limit=5)
    assert hits and hits[0].chunk_id == c_a


@pytest.mark.asyncio
async def test_index_project_vectors_cleans_removed(db):
    """chunk 删除后再次索引清理对应向量行。"""
    pid = _pid()
    prov = LocalEmbeddingProvider(dimension=128)
    c_a, c_b = _chunk_id(), _chunk_id()
    await _insert_chunk(db, pid, chunk_id=c_a, source_id="src-a", content="莫凡觉醒了雷系。")
    await _insert_chunk(db, pid, chunk_id=c_b, source_id="src-b", content="张小侯使用了风之力。")
    await index_project_vectors(db, pid, provider=prov)

    await db.execute(delete(RetrievalChunk).where(RetrievalChunk.id == c_b))
    await db.commit()

    stats = await index_project_vectors(db, pid, provider=prov)
    assert stats["removed"] == 1
    remain = (
        await db.execute(select(RetrievalVector).where(RetrievalVector.project_id == pid))
    ).scalars().all()
    assert [str(v.chunk_id) for v in remain] == [c_a]


@pytest.mark.asyncio
async def test_search_returns_empty_without_vectors(db):
    """未索引向量时返回 []（不报错）。"""
    pid = _pid()
    prov = LocalEmbeddingProvider(dimension=128)
    c_a = _chunk_id()
    await _insert_chunk(db, pid, chunk_id=c_a, source_id="src-a", content="莫凡觉醒了雷系。")
    assert await VectorRetriever(provider=prov).search(db, pid, "莫凡") == []
    assert await VectorRetriever(provider=prov).search(db, pid, "") == []


# ── RRF 融合 ─────────────────────────────────────────────

def test_rrf_merge_dedup_and_order():
    """去重（按 chunk_id）、分数累加、按融合分降序、rank 重写。"""
    l1 = [_hit("a"), _hit("b"), _hit("c")]
    l2 = [_hit("b"), _hit("c"), _hit("d")]
    merged = rrf_merge([l1, l2], k=60)
    # b: 1/62 + 1/61；c: 1/63 + 1/62；a: 1/61；d: 1/63
    assert [h.chunk_id for h in merged] == ["b", "c", "a", "d"]
    assert len(merged) == 4  # 去重后无重复 chunk
    for i, h in enumerate(merged, start=1):
        assert h.rank == i
    assert abs(merged[0].score - round(1 / 62 + 1 / 61, 4)) < 1e-9
    assert merged[0].score > merged[1].score > merged[2].score > merged[3].score


def test_rrf_merge_weights():
    """weights 放大某通道贡献，且融合顺序随权重变化（与无权重顺序不同）。"""
    l1 = [_hit("a"), _hit("b"), _hit("c")]
    l2 = [_hit("b"), _hit("c"), _hit("d")]
    # 无权重：b(1/62+1/61) > c(1/63+1/62) > a(1/61) > d(1/63)
    assert [h.chunk_id for h in rrf_merge([l1, l2], k=60)] == ["b", "c", "a", "d"]
    # 权重 [100, 1]：a = 100/61 ≈ 1.6393 > b = 100/62 + 1/61 ≈ 1.6293
    merged = rrf_merge([l1, l2], k=60, weights=[100.0, 1.0])
    assert [h.chunk_id for h in merged] == ["a", "b", "c", "d"]
    assert abs(merged[0].score - round(100 / 61, 4)) < 1e-9


def test_rrf_merge_empty_and_errors():
    assert rrf_merge([[], []]) == []
    assert rrf_merge([]) == []
    with pytest.raises(ValueError):
        rrf_merge([[_hit("a")]], k=0)
    with pytest.raises(ValueError):
        rrf_merge([[_hit("a")]], weights=[1.0, 2.0])


# ── hybrid 三通道融合（集成） ────────────────────────────

@pytest.mark.asyncio
async def test_search_hybrid_merges_channels(db):
    """实体 + 词法 + 向量三路融合：相关块去重后排第一。"""
    pid = _pid()
    await _seed_project_vocab(db, pid)
    tok = await _build_vocab(db, pid)

    c_a, c_b = _chunk_id(), _chunk_id()
    content_a = "莫凡觉醒了雷系。他是主角。"
    content_b = "张小侯使用了风之力。"
    await _insert_chunk(
        db, pid, chunk_id=c_a, source_id="src-a", content=content_a,
        search_text=_search_text(tok, content_a),
    )
    await _insert_chunk(
        db, pid, chunk_id=c_b, source_id="src-b", content=content_b,
        search_text=_search_text(tok, content_b),
    )
    # 默认 provider（单例）：索引与查询 IDF 一致
    stats = await index_project_vectors(db, pid)
    assert stats["indexed"] == 2

    hits = await search_hybrid(db, pid, "莫凡的雷系是什么？", limit=5)
    assert hits, "hybrid 应至少命中一个块"
    assert hits[0].chunk_id == c_a
    assert len([h for h in hits if h.chunk_id == c_a]) == 1  # 去重
    assert all(h.chunk_id in (c_a, c_b) for h in hits)

    # 向量通道对任何查询都返回 top-k（余弦排序），hybrid 结果不会为空；
    # 但词面完全无关的查询不会把相关块顶到首位（此处只验证空查询返回 []）
    assert await search_hybrid(db, pid, "") == []
