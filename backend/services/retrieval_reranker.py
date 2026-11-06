"""RRF 融合检索器（阶段二：三通道融合）。

决策 D4：三通道检索 = 精确实体通道 / 词法通道 / 向量通道，用 RRF
（Reciprocal Rank Fusion）融合：::

    score(hit) = Σ_i  w_i / (k + rank_i(hit))

按 chunk_id 去重后按融合分降序返回。RRF 只依赖各通道的**排序位置**，
不依赖通道间不可比的原始分数（bm25 越小越好 vs 余弦越大越好），
因此天然适合异源通道融合。

search_hybrid 三路实现
----------------------
1. 精确实体通道：``LexicalRetriever.search(entity_first=True)`` —— 查询中
   正式名/已确认别名归一后 FTS 短语精确匹配，命中块加权排前（结果含实体
   加权 + 词法补充，见 lexical_retriever 模块 docstring）；
2. 词法通道：``LexicalRetriever.search(entity_first=False)`` —— 纯词法
   （bm25 / ts_rank）；
3. 向量通道：``VectorRetriever.search`` —— 语义向量余弦（本地伪语义
   embedding 或 openai provider）。

说明：实体通道与词法通道存在重叠（entity_first 结果包含实体加权块与词法
补充块），RRF 会自然强化多通道都命中的块，这正是融合要的效果。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from services.lexical_retriever import LexicalRetriever, RetrievalHit
from services.vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)

# RRF 平滑常数（标准取值 60，位置 1 的贡献 1/61 远大于位置 60 的 1/120）
DEFAULT_RRF_K = 60
# 每通道候选条数（多取一些供 RRF 交叉排序；去重后最终按 limit 截断）
CHANNEL_LIMIT_MULTIPLIER = 3
CHANNEL_LIMIT_FLOOR = 30


def rrf_merge(
    lists: list[list[RetrievalHit]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: list[float] | None = None,
) -> list[RetrievalHit]:
    """标准 RRF 融合多路检索结果。

    - score = Σ_i w_i / (k + rank_i)，rank 从 1 开始（取自各列表内位置）；
    - 按 chunk_id 去重：同一块多通道命中时分数累加，保留首个出现的 hit 对象；
    - 按 score 降序排序，重写 rank（从 1 开始）与 score（融合分）；
    - 传入的 hit 对象会被就地改写 score/rank（调用方每次查询新建，无副作用）。
    - k 与 weights：k 为平滑常数；weights 与 lists 等长（缺省全 1）。
    """
    if k <= 0:
        raise ValueError(f"RRF k 必须为正数，got {k}")
    n_lists = len(lists)
    if weights is None:
        weights = [1.0] * n_lists
    if len(weights) != n_lists:
        raise ValueError(f"weights 长度 {len(weights)} 与 lists 长度 {n_lists} 不一致")

    acc: dict[str, list] = {}  # chunk_id -> [hit, score]
    for lst, wi in zip(lists, weights):
        if not lst:
            continue
        for pos, hit in enumerate(lst, start=1):
            cid = str(hit.chunk_id)
            contribution = wi / (k + pos)
            entry = acc.get(cid)
            if entry is not None:
                entry[1] += contribution
            else:
                acc[cid] = [hit, contribution]

    ranked = sorted(acc.values(), key=lambda item: item[1], reverse=True)
    merged: list[RetrievalHit] = []
    for i, (hit, score) in enumerate(ranked, start=1):
        hit.score = round(score, 4)
        hit.rank = i
        merged.append(hit)
    return merged

async def search_hybrid(
    db: AsyncSession,
    project_id: str,
    query: str,
    *,
    limit: int = 10,
    source_kinds=None,
    chapter_num: int | None = None,
    weights: list[float] | None = None,
    k: int = DEFAULT_RRF_K,
) -> list[RetrievalHit]:
    """三通道检索 + RRF 融合（实体 / 词法 / 向量）。

    - 每通道取 max(limit * CHANNEL_LIMIT_MULTIPLIER, CHANNEL_LIMIT_FLOOR) 条候选；
    - 返回融合后 top ``limit`` 条（无任何命中时 []）；
    - 向量通道无数据时自动退化为两路（实体 + 词法），不报错。
    """
    pid = str(project_id)
    if limit is None or limit < 1:
        return []
    query_text = str(query or "").strip()
    if not query_text:
        return []

    per_channel = max(limit * CHANNEL_LIMIT_MULTIPLIER, CHANNEL_LIMIT_FLOOR)

    lex = LexicalRetriever()
    vec = VectorRetriever()

    entity_hits = await lex.search(
        db, pid, query_text,
        source_kinds=source_kinds, limit=per_channel,
        entity_first=True, chapter_num=chapter_num,
    )
    lexical_hits = await lex.search(
        db, pid, query_text,
        source_kinds=source_kinds, limit=per_channel,
        entity_first=False, chapter_num=chapter_num,
    )
    vector_hits = await vec.search(
        db, pid, query_text,
        source_kinds=source_kinds, limit=per_channel, chapter_num=chapter_num,
    )

    merged = rrf_merge(
        [entity_hits, lexical_hits, vector_hits],
        k=k, weights=weights,
    )
    return merged[:limit]


__all__ = ["rrf_merge", "search_hybrid", "DEFAULT_RRF_K"]
