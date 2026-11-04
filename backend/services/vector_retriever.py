"""向量检索器（阶段二：向量语义检索通道）。

职责
----
1. ``index_project_vectors(db, project_id)``：为该项目的 retrieval_chunks 生成
   embedding 并写入 ``retrieval_vectors`` 投影表（幂等：chunk_id 已存在且
   content_hash 未变则跳过；chunk 删除时清理对应向量行）。
2. ``VectorRetriever.search(...)``：查询文本 embedding → 与库内全部向量做余弦
   相似度（numpy 内存计算）→ 映射回 ``RetrievalHit``（与 LexicalRetriever 共享
   检索契约，content/source_id 等从 retrieval_chunks 回表）。

共享契约（与 services/lexical_retriever.RetrievalHit 完全一致）：:

    class VectorRetriever:
        async def search(self, db, project_id, query, *, source_kinds=None,
                         limit=10, chapter_num=None) -> list[RetrievalHit]

检索实现与性能
--------------
- 当前实现：全量向量 SELECT 到内存 → numpy 矩阵点积（向量均为单位向量，
  点积即余弦）。量级 7279 行 × 512 维 ≈ 30 MB float64，单次查询亚毫秒。
- 未来可换 pgvector / HNSW / IVFPQ 等 ANN 索引，或 SQLite 侧做逐行流式计算；
  本类只依赖 ``retrieval_vectors`` + ``retrieval_chunks`` 两张投影表，
  索引结构替换不影响上层调用。
- 无向量数据时返回 []（不报错）：向量通道是可选增强，缺失时上层（hybrid /
  问答链路）自动降级，不会阻断检索。

embedding provider 一致性（重要）
--------------------------------
本地 provider 的 IDF 语料挂在实例上：``index_project_vectors`` 会用项目
chunk 内容调用 ``provider.set_corpus(...)``，查询侧必须使用**同一 provider
实例**（默认取 embedding_provider.get_provider() 单例），否则 IDF 不一致导致
查询向量与库内向量不可比。测试/多项目场景请显式传入同一 provider。
"""

from __future__ import annotations

import hashlib
import logging

import numpy as np
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.retrieval_index import RetrievalChunk
from models.retrieval_vector import RetrievalVector
from services.embedding_provider import get_provider
from services.lexical_retriever import RetrievalHit

logger = logging.getLogger(__name__)

# snippet 截取半径/上限（与 lexical_retriever 的约定一致）
SNIPPET_RADIUS = 100
SNIPPET_MAX_LEN = 300


def _content_hash(text_: str) -> str:
    """内容指纹：与 retrieval_indexer._content_hash 同算法（sha256）。"""
    return hashlib.sha256((text_ or "").encode("utf-8")).hexdigest()


def _clean_source_kinds(source_kinds) -> list[str] | None:
    """规范化 source_kinds 过滤（空列表/空串视为不限定）。"""
    if not source_kinds:
        return None
    kinds = [str(k).strip() for k in source_kinds if str(k or "").strip()]
    return list(dict.fromkeys(kinds)) or None


def _make_snippet(content: str, query: str) -> str:
    """截取 query 首个字符 2-gram 命中位置附近的片段（与 lexical 侧风格一致）。"""
    content = content or ""
    query = query or ""
    grams = [query[i : i + 2] for i in range(len(query) - 1)]
    grams = sorted(dict.fromkeys(g for g in grams if g.strip()), key=len, reverse=True)
    best: int | None = None
    for gram in grams:
        idx = content.find(gram)
        if idx >= 0 and (best is None or idx < best):
            best = idx
    if best is None:
        return content[:SNIPPET_MAX_LEN]
    start = max(0, best - SNIPPET_RADIUS)
    end = min(len(content), best + 2 + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return (prefix + content[start:end] + suffix)[: SNIPPET_MAX_LEN + 4]


# ── 向量索引（写入 retrieval_vectors 投影表） ─────────────

async def index_project_vectors(
    db: AsyncSession,
    project_id: str,
    progress_cb=None,
    *,
    provider=None,
) -> dict:
    """为项目全部 retrieval_chunks 生成向量并写入 retrieval_vectors（幂等）。

    - 语料 = 项目全部 chunk 内容（provider.set_corpus，仅本实例生效）；
    - 已有 (chunk_id, content_hash) 未变的跳过；内容变化则重算更新；
    - retrieval_vectors 中 chunk_id 已不存在于 retrieval_chunks 的行删除
      （chunk 删除时清理）；
    - progress_cb(done, total, stage) 每处理一个 chunk 调用一次。

    返回::

        {"total": n, "indexed": m, "skipped": k, "removed": r}
    """
    pid = str(project_id)
    prov = provider if provider is not None else get_provider()

    chunks = (
        await db.execute(
            select(RetrievalChunk).where(RetrievalChunk.project_id == pid)
        )
    ).scalars().all()
    total = len(chunks)
    if total == 0:
        # 项目已无 chunk：清空该项目全部向量行
        removed = (
            await db.execute(
                delete(RetrievalVector).where(RetrievalVector.project_id == pid)
            )
        ).rowcount
        await db.commit()
        return {"total": 0, "indexed": 0, "skipped": 0, "removed": removed or 0}

    # IDF 语料：项目自身的 chunk 内容（同一 provider 实例的查询侧自动对齐）
    prov.set_corpus([c.content or "" for c in chunks])

    existing = {
        str(v.chunk_id): v
        for v in (
            await db.execute(
                select(RetrievalVector).where(RetrievalVector.project_id == pid)
            )
        ).scalars().all()
    }

    current_ids: set[str] = set()
    indexed = skipped = 0
    # 批量 embedding：一次 API 请求多条（端点支持 input 数组），
    # 调用次数从 N 降到 N/BATCH（真实模型端点尤其重要，限流/成本友好）。
    BATCH = 32
    pending: list[tuple[int, object]] = []  # (chunk_id, chunk) 待算向量
    for i, c in enumerate(chunks, start=1):
        cid = str(c.id)
        current_ids.add(cid)
        chunk_hash = c.content_hash or _content_hash(c.content or "")
        old = existing.get(cid)
        if old is not None and old.content_hash == chunk_hash:
            skipped += 1
        else:
            pending.append((cid, c))

    for start in range(0, len(pending), BATCH):
        batch = pending[start : start + BATCH]
        vecs = await prov.embed_many([c.content or "" for _, c in batch])
        for (cid, c), emb in zip(batch, vecs):
            dim = len(emb)
            old = existing.get(cid)
            chunk_hash = c.content_hash or _content_hash(c.content or "")
            if old is not None:
                old.embedding = emb
                old.dim = dim
                old.content_hash = chunk_hash
            else:
                db.add(
                    RetrievalVector(
                        project_id=pid,
                        chunk_id=cid,
                        embedding=emb,
                        dim=dim,
                        content_hash=chunk_hash,
                    )
                )
            indexed += 1
        if progress_cb is not None:
            progress_cb(skipped + indexed, total, "vector_index")

    removed = 0
    for cid, row in existing.items():
        if cid not in current_ids:
            await db.delete(row)
            removed += 1

    await db.commit()
    return {"total": total, "indexed": indexed, "skipped": skipped, "removed": removed}


# ── 向量检索 ─────────────────────────────────────────────

class VectorRetriever:
    """基于 retrieval_vectors 投影表的向量检索器（无状态可复用）。

    provider 默认取 embedding_provider.get_provider() 单例；构造时传同一
    provider 实例（与 index_project_vectors 一致）可保证 IDF 对齐。
    """

    def __init__(self, provider=None):
        self.provider = provider if provider is not None else get_provider()

    async def search(
        self,
        db: AsyncSession,
        project_id: str,
        query: str,
        *,
        source_kinds=None,
        limit: int = 10,
        chapter_num: int | None = None,
    ) -> list[RetrievalHit]:
        """查询文本 embedding → 余弦相似度排序 → 回表 RetrievalHit。

        - source_kinds：限定来源类型（None/空 = 不限）；
        - chapter_num：限定章节号（从 retrieval_chunks_fts 回查 chapter_no，
          表缺失时降级用 chunk metadata，见下）；
        - 无向量数据（未索引）返回 []（不报错，由上层决定降级）。
        """
        pid = str(project_id)
        query_text = str(query or "").strip()
        if not query_text or limit is None or limit < 1:
            return []

        qvec = np.asarray(await self.provider.embed(query_text), dtype=np.float64)
        if qvec.size == 0:
            return []

        vector_rows = (
            await db.execute(
                select(RetrievalVector).where(RetrievalVector.project_id == pid)
            )
        ).scalars().all()
        if not vector_rows:
            return []

        chunks = {
            str(c.id): c
            for c in (
                await db.execute(
                    select(RetrievalChunk).where(RetrievalChunk.project_id == pid)
                )
            ).scalars().all()
        }
        kinds = _clean_source_kinds(source_kinds)

        # chapter_no/title 只存在 FTS 侧（DDL 契约）；表缺失时降级：
        # chapter_no 从 chunk metadata 取，title 置空。
        fts_meta: dict[str, tuple[int | None, str]] = {}
        try:
            rows = (
                await db.execute(
                    text(
                        "SELECT chunk_id, chapter_no, title FROM retrieval_chunks_fts "
                        "WHERE project_id = :pid"
                    ),
                    {"pid": pid},
                )
            ).all()
            fts_meta = {
                str(r[0]): (r[1], str(r[2] or "")) for r in rows
            }
        except Exception:  # noqa: BLE001 — FTS 表缺失时降级（不阻断向量通道）
            logger.debug("retrieval_chunks_fts 不可用，chapter_no/title 降级")

        ids: list[str] = []
        matrix: list[np.ndarray] = []
        for v in vector_rows:
            cid = str(v.chunk_id)
            chunk = chunks.get(cid)
            if chunk is None:
                continue  # 孤儿向量（chunk 已删，等下次索引清理）
            if kinds and chunk.source_kind not in kinds:
                continue
            if chapter_num is not None:
                cn = fts_meta.get(cid, (None, ""))[0]
                if cn is None:
                    cn = (chunk.metadata_ or {}).get("chapter_no")
                if cn != chapter_num:
                    continue
            emb = np.asarray(v.embedding, dtype=np.float64)
            if emb.size == 0 or emb.shape != qvec.shape:
                continue  # 维度不匹配（provider 维度变化），跳过该行
            ids.append(cid)
            matrix.append(emb)

        if not matrix:
            return []

        mat = np.stack(matrix)
        # 防御性归一（存储侧已归一；防历史脏数据），归一后点积 = 余弦
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
        qnorm = float(np.linalg.norm(qvec))
        qvec = qvec / qnorm if qnorm > 0 else qvec

        scores = mat @ qvec
        order = np.argsort(-scores)

        hits: list[RetrievalHit] = []
        for rank, idx in enumerate(order[:limit], start=1):
            cid = ids[int(idx)]
            chunk = chunks[cid]
            chapter_no, title = fts_meta.get(cid, (None, ""))
            if chapter_no is None:
                chapter_no = (chunk.metadata_ or {}).get("chapter_no")
            content = chunk.content or ""
            hits.append(
                RetrievalHit(
                    chunk_id=cid,
                    project_id=pid,
                    source_kind=chunk.source_kind,
                    source_id=str(chunk.source_id),
                    source_version_id=chunk.source_version_id,
                    chapter_no=chapter_no,
                    title=title,
                    content=content,
                    snippet=_make_snippet(content, query_text),
                    score=round(float(scores[int(idx)]), 4),
                    rank=rank,
                )
            )
        return hits


__all__ = ["VectorRetriever", "index_project_vectors"]
