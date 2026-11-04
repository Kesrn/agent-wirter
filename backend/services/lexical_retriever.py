"""词法检索器（阶段一核心：检索侧）。

共享契约（与 retrieval_eval/run_baseline.py 对齐）::

    @dataclass
    class RetrievalHit:
        chunk_id: str; project_id: str; source_kind: str; source_id: str
        source_version_id: str | None; chapter_no: int | None; title: str
        content: str; snippet: str; score: float; rank: int

    class LexicalRetriever:
        async def search(self, db, project_id, query, *, source_kinds=None,
                         limit=10, entity_first=True, chapter_num=None) -> list[RetrievalHit]

双通道检索
----------
1. 实体优先通道：``normalize_entities(project_id, query)`` 先把查询中的正式名/
   已确认别名归一到 canonical 名；若命中实体且 entity_first=True，先用
   FTS 短语精确匹配实体名（SQLite: ``MATCH '"实体名"'``），命中块加权重
   （ENTITY_BOOST=1000）排在最前。
2. 词法通道：项目分词器（get_project_tokenizer）切词 → OR 拼接 FTS 查询，
   bm25（SQLite）/ ts_rank（PG）排序。

质量门槛：query 至少一个 token（或命中的实体名）真正出现在命中块的
search_text 中，否则视为无效命中；整次查询无任何有效命中时返回 []。

双后端
------
- SQLite：``retrieval_chunks_fts MATCH :q AND project_id=:pid`` + ``bm25()``
  排序（FTS5 虚拟表，中文走 unicode61，search_text 已由索引侧分词成空格
  分隔 token，查询侧同样处理，两侧一致即可精确命中）。
- PostgreSQL：本阶段用 tsvector 路径（``search_tsv @@ to_tsquery('simple', q)``
  + ts_rank，search_tsv 是 setup_pg.py 在 retrieval_chunks_fts 上建的存储生成列，
  见 _PG_FTS_SQL 注释），pg_trgm 作为后续候选（trgm 对中文子串匹配更宽容，
  已建 GIN 索引待后续评估）。
  该分支已用真实 PostgreSQL 16 验证（retrieval_eval/setup_pg.py +
  verify_pg.py），验证中发现并修复了两个 PG 特有 bug：
  1. ORM 侧 retrieval_chunks.id 是 UUID 类型，而 retrieval_chunks_fts.chunk_id
     是 VARCHAR，直接 ``c.id = f.chunk_id`` 报
     ``operator does not exist: uuid = character varying``，需显式 ``c.id::text``；
  2. 表达式索引（``to_tsvector('simple', search_text)``）方案在真实数据下
     Bitmap Recheck 与 ts_rank 会逐行重算分词（7.3k 块时单查询 1~3s），
     改用 search_tsv 存储生成列后单查询降到几十毫秒级。

依赖索引
--------
检索只读 retrieval_chunks / retrieval_chunks_fts 两张投影表，由
services.retrieval_indexer 写入（SQLite 侧必须先 ensure_fts_table 建表并
index_* 投影；评测前跑 retrieval_eval/index_dev_db.py）。表不存在时直接
报错，避免静默返回空结果掩盖"忘了建索引"的问题。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.retrieval_tokenizer import (
    build_project_vocabulary,
    get_project_tokenizer,
    normalize_entities,
)

logger = logging.getLogger(__name__)

# 实体命中权重：远大于 bm25 典型量级（[-20, 0]），保证实体优先排序稳定。
ENTITY_BOOST = 1000.0
# snippet 截取半径（命中词前后各 N 字符）
SNIPPET_RADIUS = 100
SNIPPET_MAX_LEN = 300

# SQLite FTS5 检索（bm25 越小越好；chunk_id 回表 retrieval_chunks 取
# source_version_id，该列只存在投影表，FTS 侧契约 DDL 无此列）。
_SQLITE_FTS_SQL = """
SELECT f.chunk_id, f.project_id, f.source_kind, f.source_id, f.chapter_no,
       f.title, f.content, f.search_text, c.source_version_id,
       bm25(retrieval_chunks_fts) AS bm
FROM retrieval_chunks_fts f
JOIN retrieval_chunks c ON c.id = f.chunk_id
WHERE retrieval_chunks_fts MATCH :q
  AND f.project_id = :pid
{extra}
ORDER BY bm25(retrieval_chunks_fts)
LIMIT :lim
"""

# PostgreSQL tsvector 路径（ts_rank 越大越好；已在真实 PG 16 验证，见模块 docstring）。
# 依赖 setup_pg.py 在 retrieval_chunks_fts 上建的 search_tsv 存储生成列
# （to_tsvector('simple', search_text) STORED）+ GIN 索引：直接用列做 @@ 过滤
# 与 ts_rank 排序，避免逐行重算 to_tsvector（表达式索引在真实数据下 Bitmap
# Recheck/排序会把单查询拖到 1~3s）。列缺失时 PG 会报明确错误（fail-fast，
# 与"表不存在直接报错"的既有约定一致）。
# 另注意：retrieval_chunks.id 在 PG 上是 UUID 类型，而 retrieval_chunks_fts.chunk_id
# 是 VARCHAR（见 retrieval_indexer.PG_FTS_TABLE_CREATE_SQL），两者直接比较会报
# "operator does not exist: uuid = character varying"，必须显式 c.id::text 转换
# （chunk_id 存的就是 uuid4 字符串，::text 后与之一致）。
_PG_FTS_SQL = """
SELECT f.chunk_id, f.project_id, f.source_kind, f.source_id, f.chapter_no,
       f.title, f.content, f.search_text, c.source_version_id,
       ts_rank(f.search_tsv, to_tsquery('simple', :q)) AS bm
FROM retrieval_chunks_fts f
JOIN retrieval_chunks c ON c.id::text = f.chunk_id
WHERE f.search_tsv @@ to_tsquery('simple', :q)
  AND f.project_id = :pid
{extra}
ORDER BY bm DESC
LIMIT :lim
"""


@dataclass
class RetrievalHit:
    """单条检索命中（契约字段，rank 从 1 开始）。"""

    chunk_id: str
    project_id: str
    source_kind: str
    source_id: str
    source_version_id: str | None
    chapter_no: int | None
    title: str
    content: str
    snippet: str
    score: float
    rank: int = field(default=0)


def _clean_source_kinds(source_kinds) -> list[str] | None:
    """规范化 source_kinds 过滤（空列表/空串视为不限定）。"""
    if not source_kinds:
        return None
    kinds = [str(k).strip() for k in source_kinds if str(k or "").strip()]
    return list(dict.fromkeys(kinds)) or None


def _make_snippet(content: str, terms: list[str]) -> str:
    """截取核心命中词附近的片段（前后 SNIPPET_RADIUS 字符）。

    按词长降序找命中位置：查询里的专有名词（3 字+人名/地名）通常比高频
    功能词（"死了/结局/吗"）承载更多信息——若按"第一个命中词"截取，
    功能词先出现时 snippet 会丢失实体关键信息（如"张英璐就那样被杀死"
    里的"被杀"）。同长词取最先出现者。
    """
    content = content or ""
    for term in sorted([t for t in terms if t], key=len, reverse=True):
        idx = content.find(term)
        if idx >= 0:
            start = max(0, idx - SNIPPET_RADIUS)
            end = min(len(content), idx + len(term) + SNIPPET_RADIUS)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(content) else ""
            return (prefix + content[start:end] + suffix)[: SNIPPET_MAX_LEN + 4]
    return content[:SNIPPET_MAX_LEN]


class LexicalRetriever:
    """基于检索投影表（retrieval_chunks + fts）的词法检索器。

    无状态可复用；进程内按项目缓存词汇表（同 retrieval_tokenizer 缓存机制）。
    """

    def __init__(self):
        self._vocab_ready: set[str] = set()

    async def _ensure_vocab(self, db: AsyncSession, project_id: str) -> None:
        """首次检索某项目时构建项目分词器（后续直接复用缓存实例）。"""
        pid = str(project_id)
        if pid in self._vocab_ready:
            return
        data = await build_project_vocabulary(db, pid)
        get_project_tokenizer(pid, data["version"])
        self._vocab_ready.add(pid)

    async def search(
        self,
        db: AsyncSession,
        project_id: str,
        query: str,
        *,
        source_kinds=None,
        limit: int = 10,
        entity_first: bool = True,
        chapter_num: int | None = None,
    ) -> list[RetrievalHit]:
        """执行词法检索（实体优先 + 词法补充），返回按分数降序的 RetrievalHit。

        - source_kinds：限定来源类型（None/空 = 不限）；
        - chapter_num：限定章节（None = 不限）；
        - 质量门槛：query 至少一个 token（或命中实体）真实出现在命中块
          search_text 中，否则该块不算命中；整体无命中返回 []。
        """
        pid = str(project_id)
        if limit is None or limit < 1:
            return []
        query_text = str(query or "").strip()
        if not query_text:
            return []
        await self._ensure_vocab(db, pid)
        tokenizer = get_project_tokenizer(pid)
        tokens = tokenizer.tokenize(query_text)
        if not tokens:
            return []

        kinds = _clean_source_kinds(source_kinds)
        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

        canonical_names: list[str] = []
        if entity_first:
            canonical_names, _ = normalize_entities(pid, query_text)

        # 质量门槛用词：query token ∪ 归一化实体名（实体名也是查询的有效词素）
        gate_terms = list(dict.fromkeys(tokens + canonical_names))

        best: dict[str, RetrievalHit] = {}

        def keep(hit: RetrievalHit) -> None:
            existing = best.get(hit.chunk_id)
            if existing is None or hit.score > existing.score:
                best[hit.chunk_id] = hit

        # 1) 实体优先通道：实体名精确短语匹配，命中加权
        if canonical_names:
            for name in canonical_names:
                fts_q = f'"{name}"' if dialect == "sqlite" else name
                rows = await self._run_fts(
                    db, pid, fts_q, dialect, kinds, chapter_num, limit
                )
                for row in rows:
                    hit = self._build_hit(row, gate_terms, dialect, boost=ENTITY_BOOST)
                    if hit is not None:
                        keep(hit)

        # 2) 词法通道：全 query token OR 匹配，bm25/ts_rank 排序
        fts_q = (
            " OR ".join(f'"{t}"' for t in tokens)
            if dialect == "sqlite"
            else " | ".join(tokens)
        )
        rows = await self._run_fts(db, pid, fts_q, dialect, kinds, chapter_num, limit)
        for row in rows:
            hit = self._build_hit(row, gate_terms, dialect, boost=0.0)
            if hit is not None:
                keep(hit)

        if not best:
            return []
        ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)[:limit]
        for i, hit in enumerate(ranked, start=1):
            hit.rank = i
        return ranked

    # ── 内部实现 ─────────────────────────────────────────

    async def _run_fts(
        self,
        db: AsyncSession,
        pid: str,
        fts_q: str,
        dialect: str,
        kinds: list[str] | None,
        chapter_num: int | None,
        limit: int,
    ) -> list[dict]:
        """执行 FTS 查询并返回原始行（含 bm 分数，未归一化）。"""
        extra_parts: list[str] = []
        params: dict = {"q": fts_q, "pid": pid, "lim": limit}
        if kinds:
            extra_parts.append(
                "AND f.source_kind IN ("
                + ", ".join(f":sk{i}" for i in range(len(kinds)))
                + ")"
            )
            params.update({f"sk{i}": k for i, k in enumerate(kinds)})
        if chapter_num is not None:
            extra_parts.append("AND f.chapter_no = :cn")
            params["cn"] = chapter_num

        sql = (
            _SQLITE_FTS_SQL if dialect == "sqlite" else _PG_FTS_SQL
        ).format(extra="\n".join(extra_parts))
        result = await db.execute(text(sql), params)
        return list(result.mappings())

    def _build_hit(
        self,
        row: dict,
        gate_terms: list[str],
        dialect: str,
        *,
        boost: float,
    ) -> RetrievalHit | None:
        """行 → RetrievalHit；质量门槛不通过返回 None。

        row 键：chunk_id / project_id / source_kind / source_id / chapter_no /
        title / content / search_text / source_version_id / bm。
        """
        search_text = row["search_text"] or ""
        # 质量门槛：至少一个 query token / 实体名真实出现在该块的检索文本中
        if not any(t and t in search_text.split() for t in gate_terms):
            return None
        raw_bm = float(row["bm"] or 0.0)
        # SQLite bm25 越小越好 → 取负；PG ts_rank 越大越好 → 直接加
        score = round(-raw_bm + boost, 4) if dialect == "sqlite" else round(raw_bm + boost, 4)
        content = row["content"] or ""
        return RetrievalHit(
            chunk_id=str(row["chunk_id"]),
            project_id=str(row["project_id"]),
            source_kind=str(row["source_kind"]),
            source_id=str(row["source_id"]),
            source_version_id=row.get("source_version_id"),
            chapter_no=row.get("chapter_no"),
            title=row.get("title") or "",
            content=content,
            snippet=_make_snippet(content, gate_terms),
            score=score,
            rank=0,  # 最终排序后统一赋值
        )


__all__ = ["RetrievalHit", "LexicalRetriever"]
