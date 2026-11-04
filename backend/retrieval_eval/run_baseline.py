#!/usr/bin/env python3
"""检索评测运行器（阶段零：ILIKE 基线；阶段一：多检索器对比；阶段二：向量/融合）。

--retriever 支持四种检索器，命中判定逻辑与输出 JSON 结构完全一致：

    1. ilike（默认，阶段零既有逻辑）：
       services.knowledge_source.search_project_knowledge(db, project_id, query, *, limit=K)
       -> list[dict]  每项含 source_kind / source_id / chunk_id / title / snippet / score / matched_query

    2. lexical（阶段一，由 lexical retriever 任务并行实现，本脚本延迟导入）：
       services.lexical_retriever.LexicalRetriever().search(db, project_id, query, *,
           source_kinds=None, limit=10, entity_first=True, chapter_num=None)
       -> list[RetrievalHit]  (chunk_id / project_id / source_kind / source_id /
           source_version_id / chapter_no / title / content / snippet / score / rank)
       脚本在函数内延迟 import 该模块：文件尚未实现时给出清晰报错，不会在启动时崩溃。

    3. vector（阶段二，向量语义通道）：
       services.vector_retriever.VectorRetriever().search(db, project_id, query, *,
           source_kinds=None, limit=10, chapter_num=None)
       前置条件：项目向量已索引（retrieval_vectors 表有数据），由
       retrieval_eval/run_vector_eval.py（--index）或 index_project_vectors 写入；
       未索引时返回 []（本脚本保持只读，不写库）。

    4. hybrid（阶段二，RRF 三通道融合）：
       services.retrieval_reranker.search_hybrid(db, project_id, query, *,
           limit=10, source_kinds=None, chapter_num=None)
       三路 = 精确实体通道（entity_first）+ 词法通道 + 向量通道，RRF 融合
       （score = Σ w/(k+rank)，k=60）。向量通道缺数据时自动退化为两路。

运行流程：
    1. 用 sqlite3 标准库（只读）解析 --db，取出默认项目（--project 未指定时取第一行）；
    2. 加载评测数据集（--dataset）；文件不存在时改用内置冒烟样本
       （冒烟样本基于真实库内容动态构造，见 build_smoke_samples）；
    3. 逐样本调用所选检索器（async），记录每查询耗时；
    4. 按命中规则判定每样本 hit（各检索器共用同一判定逻辑），
       计算 Recall@K / MRR / Precision@K / 误召回率 / P95 延迟；
    5. --out 写 JSON（每样本结果 + 汇总 + 分组），控制台打印按 category/difficulty 分组的汇总表。

────────────────────────────────────────────────────────────
命中判定规则（各检索器共用；仅当结果 dict 里真的存在某字段时才参与比对）

ilike 检索器返回 chunk 级结果，字段为：
    source_kind / source_id / chunk_id / title / snippet / score / matched_query
lexical/vector/hybrid 检索器返回 RetrievalHit（含 chapter_no / source_kind / rank 等），
本脚本先映射为与 ilike 同构的 dict（见 retrieval_hit_to_dict）再判定。
evidence_type / chapter_no 是结构化证据/未来证据类型的字段，仅当结果里真的存在才比对。

对单条结果 r 判定“命中 expected”：
    A. r["source_id"] ∈ expected.source_ids          → 命中
    B. r["chunk_id"] ∈ expected.source_ids            → 命中（兼容数据集写 chunk 级 id）
    C. r 含 evidence_type 且 == expected.evidence_type，
       且 r 的 snippet/title 拼接文本包含 expected.entities 中任一实体 → 命中
       （该分支当前基线结果不含 evidence_type 时不触发，为未来结构化证据预留）
    D. chapter_no 是全局约束：结果与期望都提供 chapter_no 且不相等时，
       任何分支都不算命中（ilike 结果无此字段不触发；
       lexical/vector/hybrid 结果自带 chapter_no，会正常参与约束）

样本级 hit = top-K 结果中任意一条命中。
────────────────────────────────────────────────────────────

指标定义（写入 summary 与分组表）：
    Recall@K    = 样本级 hit 比例（期望证据出现在 top-K 即记 1）
    MRR         = 每个样本第一个命中结果的 1/rank（无命中记 0），再取平均
    Precision@K = 每个样本 top-K 中命中条数 / min(K, 实际返回条数)，再取平均
    （无返回结果时该样本 Precision 记 0）
    误召回率    = 1 - Precision@K
    P95 延迟    = 所有查询耗时的 95 分位（ms），n 小时取第 ceil(0.95*n) 小值

安全约束：
    - 只读数据库：sqlite3 侧用 mode=ro URI；ORM 侧只调检索接口（纯 SELECT），
      全程不 commit，绝不对 dev-web.sqlite3 做任何写操作。
    - 不修改 services/ 或 models/ 任何现有代码。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────
# 路径与环境
# ──────────────────────────────────────────────────────────

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"
DEFAULT_DATASET = EVAL_DIR / "datasets" / "regression_candidates.json"
DEFAULT_OUT_DIR = EVAL_DIR / "results"


def resolve_eval_path(path: str, *, create: bool = False) -> Path:
    """命令行相对路径 → 绝对路径。

    优先按当前工作目录（cwd）解析（保持 README 中从 backend/ 运行的既有用法）；
    cwd 下不存在时回退到 retrieval_eval/（EVAL_DIR）解析，方便从项目根目录直接运行，
    例如 --dataset datasets/natural_queries.json 与 --out results/xxx.json。

    create=True 用于将要创建的输出文件（无法用存在性判断）：
    cwd 下存在路径首段目录（如 results/）则按 cwd，否则按 EVAL_DIR。
    """
    p = Path(path)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    alt = EVAL_DIR / p
    if alt.exists():
        return alt.resolve()
    if create:
        if (Path.cwd() / p.parts[0]).exists():
            return p.resolve()
        return alt.resolve()
    return p.resolve()

# 内置冒烟样本关键词模板：查询基于真实库内容构造（见 build_smoke_samples）。
# requires 里的词只要出现在该项目的任一 source/chunk 文本中，模板即可启用。
SMOKE_TEMPLATES: list[dict] = [
    {
        "category": "world_rule",
        "requires": ["冰系", "火系", "雷系"],
        "query": "元素魔法有哪些系？",
        "evidence_type": "world_rule",
        "entities": ["冰系", "火系", "土系", "水系", "风系", "光系", "雷系"],
        "difficulty": "smoke",
        "notes": "冒烟：基于库内真实内容自动生成的通用世界规则查询",
    },
    {
        "category": "character_first_appearance",
        "requires": ["莫凡", "觉醒"],
        "query": "莫凡是在哪里参加魔法觉醒仪式的？",
        "evidence_type": "event_timeline",
        "entities": ["莫凡", "觉醒"],
        "difficulty": "smoke",
        "notes": "冒烟：角色首次出场/觉醒相关事件查询",
    },
    {
        "category": "ability_profile",
        "requires": ["莫凡", "火系"],
        "query": "莫凡觉醒的是什么系？",
        "evidence_type": "ability_profile",
        "entities": ["莫凡", "火系"],
        "difficulty": "smoke",
        "notes": "冒烟：角色能力绑定查询",
    },
    {
        "category": "character_profile",
        "requires": ["张小侯", "风系"],
        "query": "张小侯掌握的是什么系？",
        "evidence_type": "ability_profile",
        "entities": ["张小侯", "风系"],
        "difficulty": "smoke",
        "notes": "冒烟：角色能力绑定查询",
    },
    {
        "category": "relationship",
        "requires": ["莫凡", "莫家兴"],
        "query": "莫凡的父亲是谁？",
        "evidence_type": "character_profile",
        "entities": ["莫凡", "莫家兴"],
        "difficulty": "smoke",
        "notes": "冒烟：人物关系查询",
    },
]


# ──────────────────────────────────────────────────────────
# 数据库只读辅助（标准库 sqlite3，绝不写库）
# ──────────────────────────────────────────────────────────

def _readonly_conn(db_path: str) -> sqlite3.Connection:
    """以只读模式打开 SQLite。mode=ro 时 SQLite 本身拒绝任何写操作。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def fetch_first_project(db_path: str, requested: str | None) -> tuple[str, str]:
    """返回 (project_id, project_title)。

    --project 传了就用传的值；否则取库中第一行项目。
    projects 表列名在部分版本叫 title，部分叫 name，这里探测兼容。
    """
    if requested:
        return requested, ""
    conn = _readonly_conn(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT id, title FROM projects ORDER BY rowid LIMIT 1"
            ).fetchone()
            col = "title"
        except sqlite3.OperationalError:
            row = conn.execute(
                "SELECT id, name FROM projects ORDER BY rowid LIMIT 1"
            ).fetchone()
            col = "name"
        if not row:
            raise SystemExit(f"数据库 {db_path} 中没有任何项目（projects 表为空）")
        pid, title = row
        if not requested:
            print(f"[info] 未指定 --project，使用库中第一个项目: {pid} ({col}={title!r})")
        return str(pid), str(title or "")
    finally:
        conn.close()


def load_project_content(db_path: str, project_id: str) -> dict:
    """读取项目真实内容（只读），供冒烟样本构造与元信息展示。

    返回 {sources: [...], chunks: [...], text: 拼接后的检索文本}。
    sources 只取 content 前 6000 字符，chunks 只取前 40 条，避免读入整本小说。
    """
    conn = _readonly_conn(db_path)
    try:
        sources = [
            {
                "id": str(r[0]),
                "title": r[1] or "",
                "source_type": r[2] or "",
                "content_prefix": (r[3] or "")[:6000],
            }
            for r in conn.execute(
                "SELECT id, title, source_type, content FROM project_sources "
                "WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
        ]
        chunks = [
            {"source_id": str(r[0]), "content": r[1] or ""}
            for r in conn.execute(
                "SELECT source_id, content FROM project_source_chunks "
                "WHERE project_id = ? ORDER BY chunk_index LIMIT 40",
                (project_id,),
            ).fetchall()
        ]
        # 拼接检索文本：chunk 内容为主（最新切片），source 前缀兜底。
        text = "\n".join(c["content"] for c in chunks)
        text += "\n" + "\n".join(
            f"{s['title']}\n{s['content_prefix']}" for s in sources
        )
        return {"sources": sources, "chunks": chunks, "text": text}
    finally:
        conn.close()


def build_smoke_samples(db_path: str, project_id: str) -> tuple[list[dict], str]:
    """基于真实库内容构造内置冒烟样本（数据集文件不存在时兜底）。

    每个模板的 requires 词若命中项目真实文本，就生成一条样本，
    expected.source_ids = 该词所在 chunk 的 source_id（兜底为第一个 source 的 id）。
    若一条模板都没命中（极端项目），生成一条基于文本首个 2-4 字实体的兜底查询。
    """
    content = load_project_content(db_path, project_id)
    text = content["text"]
    chunks = content["chunks"]
    sources = content["sources"]

    def default_source_id() -> str:
        if chunks:
            return chunks[0]["source_id"]
        if sources:
            return sources[0]["id"]
        raise SystemExit(f"项目 {project_id} 没有 project_sources 或 chunks，无法构造冒烟样本")

    samples: list[dict] = []
    used_queries: set[str] = set()
    for i, tpl in enumerate(SMOKE_TEMPLATES, start=1):
        if not any(term in text for term in tpl["requires"]):
            continue
        # 找 requires 首个词命中的 chunk 作为证据来源
        first_term = tpl["requires"][0]
        sid = default_source_id()
        for c in chunks:
            if first_term in c["content"]:
                sid = c["source_id"]
                break
        query = tpl["query"]
        if query in used_queries:
            continue
        used_queries.add(query)
        samples.append({
            "query_id": f"smoke-{i:03d}",
            "category": tpl["category"],
            "query": query,
            "expected": {
                "evidence_type": tpl["evidence_type"],
                "source_ids": [sid],
                "entities": tpl["entities"],
            },
            "difficulty": tpl["difficulty"],
            "notes": tpl["notes"],
        })

    if not samples:
        # 极端兜底：取文本中第一个 2-4 字中文词做查询
        import re
        match = re.search(r"[\u4e00-\u9fff]{2,4}", text)
        word = match.group(0) if match else "主角"
        samples.append({
            "query_id": "smoke-fallback",
            "category": "character_profile",
            "query": f"{word}是什么？",
            "expected": {
                "evidence_type": "character_profile",
                "source_ids": [default_source_id()],
                "entities": [word],
            },
            "difficulty": "smoke",
            "notes": "冒烟兜底：未命中任何模板，用文本首个中文词构造",
        })
    return samples, "builtin-smoke"


# ──────────────────────────────────────────────────────────
# 数据集加载
# ──────────────────────────────────────────────────────────

def load_dataset(path: str, project_id: str, db_path: str) -> tuple[list[dict], str, int]:
    """加载评测数据集。

    返回 (samples, source_label, skipped)。source_label 用于区分真实数据集与冒烟样本。
    兼容顶层是数组，或 {samples|items|data: [...]} 两种格式。
    """
    p = Path(path)
    if not p.exists():
        print(f"[warn] 数据集文件不存在: {p}，改用内置冒烟样本")
        samples, label = build_smoke_samples(db_path, project_id)
        return samples, label, 0

    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("samples") or raw.get("items") or raw.get("data") or []
    else:
        raise SystemExit(f"数据集 {p} 顶层必须是 JSON 数组或包含 samples/items/data 的对象")

    if not items:
        print(f"[warn] 数据集 {p} 为空，改用内置冒烟样本")
        samples, label = build_smoke_samples(db_path, project_id)
        return samples, label, 0

    # 校验并过滤关键字段缺失的样本
    valid, skipped = [], 0
    for it in items:
        exp = it.get("expected") or {}
        if not it.get("query_id") or not it.get("query") or not (exp.get("source_ids") or exp.get("entities")):
            skipped += 1
            continue
        valid.append(it)
    if not valid:
        raise SystemExit(f"数据集 {p} 中没有任何有效样本（缺少 query_id/query/expected.source_ids 或 entities）")
    print(f"[info] 数据集 {p}: 共 {len(items)} 条，有效 {len(valid)} 条，跳过 {skipped} 条")
    return valid, str(p), skipped


# ──────────────────────────────────────────────────────────
# 命中判定
# ──────────────────────────────────────────────────────────

def is_hit(result: dict, expected: dict) -> bool:
    """判定单条检索结果是否命中期望证据。规则见模块 docstring。"""
    # chapter_no 是全局约束：双方都提供时若不等，任何分支都不算命中。
    if result.get("chapter_no") is not None and expected.get("chapter_no") is not None:
        if result["chapter_no"] != expected["chapter_no"]:
            return False

    exp_ids = {str(x) for x in (expected.get("source_ids") or []) if x}
    if result.get("source_id") and str(result["source_id"]) in exp_ids:
        return True
    if result.get("chunk_id") and str(result["chunk_id"]) in exp_ids:
        return True

    # 证据类型分支：仅当结果自带 evidence_type 时启用（基线结果没有该字段）。
    if result.get("evidence_type") and expected.get("evidence_type"):
        if result["evidence_type"] != expected["evidence_type"]:
            return False
        text = f"{result.get('snippet') or ''}{result.get('title') or ''}"
        entities = expected.get("entities") or []
        if any(e and str(e) in text for e in entities):
            return True

    return False


# ──────────────────────────────────────────────────────────
# lexical 检索器（阶段一：延迟导入，接口契约见模块 docstring）
# ──────────────────────────────────────────────────────────

LEXICAL_MODULE = "services.lexical_retriever"
LEXICAL_CLASS = "LexicalRetriever"


def get_lexical_retriever():
    """延迟导入 services.lexical_retriever 并实例化 LexicalRetriever。

    该模块由 lexical retriever 任务并行实现，可能尚未合入；
    导入失败或不符合共享契约时给出清晰报错，而不是让脚本在 import 阶段崩溃。
    """
    try:
        import services.lexical_retriever as mod
    except ImportError as e:
        raise SystemExit(
            f"[error] --retriever lexical 需要 {LEXICAL_MODULE}.py（由 lexical retriever 任务实现），"
            f"当前无法导入: {e}\n"
            f"  请先合并 lexical_retriever 实现，或改用 --retriever ilike。"
        ) from e
    cls = getattr(mod, LEXICAL_CLASS, None)
    if cls is None:
        raise SystemExit(
            f"[error] {LEXICAL_MODULE}.py 未提供 {LEXICAL_CLASS} 类，不符合共享契约：\n"
            f"  {LEXICAL_CLASS}.search(db, project_id, query, *, source_kinds=None, "
            f"limit=10, entity_first=True, chapter_num=None) -> list[RetrievalHit]"
        )
    try:
        retriever = cls()
    except TypeError as e:
        raise SystemExit(f"[error] 实例化 {LEXICAL_CLASS} 失败: {e}") from e
    if not callable(getattr(retriever, "search", None)):
        raise SystemExit(
            f"[error] {LEXICAL_CLASS} 缺少 async search(...) 方法，不符合共享契约"
        )
    return retriever


def retrieval_hit_to_dict(hit) -> dict:
    """把 RetrievalHit（dataclass 或任意带同名属性的对象）映射为 ilike 兼容的 dict。

    与 search_project_knowledge 返回的 dict 同构（source_id / chunk_id / title / snippet /
    score / matched_query），命中判定 is_hit 与输出 JSON 结构保持不变。
    缺字段用 None 兜底，不抛异常；extra 字段（chapter_no / source_kind / rank）
    保留在 dict 里供判定与输出使用（ilike 结果无这些键，不影响其输出结构）。
    """
    return {
        "source_id": getattr(hit, "source_id", None),
        "chunk_id": getattr(hit, "chunk_id", None),
        "source_kind": getattr(hit, "source_kind", None),
        "source_version_id": getattr(hit, "source_version_id", None),
        "chapter_no": getattr(hit, "chapter_no", None),
        "title": getattr(hit, "title", None),
        "snippet": getattr(hit, "snippet", None),
        "score": getattr(hit, "score", None),
        "rank": getattr(hit, "rank", None),
        "matched_query": None,  # lexical 无此字段，占位保持输出结构一致
    }


async def search_lexical(retriever, db, project_id: str, query: str, args) -> list[dict]:
    """调用 LexicalRetriever.search 并映射为 dict 列表。

    命中判定规则（is_hit）与 ilike 模式完全共用；搜索参数透传共享契约。
    """
    hits = await retriever.search(
        db,
        project_id,
        query,
        source_kinds=args.source_kinds,   # None = 不限定（契约默认）
        limit=args.limit,
        entity_first=True,                # 契约默认值
        chapter_num=args.chapter_num,     # None = 不限定
    )
    if hits:
        first = hits[0]
        if getattr(first, "chunk_id", None) is None and getattr(first, "source_id", None) is None:
            raise SystemExit(
                "[error] LexicalRetriever.search 返回的 RetrievalHit 缺少 chunk_id/source_id，"
                "不符合共享契约（见 run_baseline.py 模块 docstring）"
            )
    return [retrieval_hit_to_dict(h) for h in hits]


# ──────────────────────────────────────────────────────────
# vector / hybrid 检索器（阶段二：延迟导入，接口契约见模块 docstring）
# ──────────────────────────────────────────────────────────

VECTOR_MODULE = "services.vector_retriever"
VECTOR_CLASS = "VectorRetriever"
HYBRID_MODULE = "services.retrieval_reranker"


def get_vector_retriever():
    """延迟导入 services.vector_retriever 并实例化 VectorRetriever。

    失败时给出清晰报错（同 get_lexical_retriever 的约定）。
    """
    try:
        import services.vector_retriever as mod
    except ImportError as e:
        raise SystemExit(
            f"[error] --retriever vector 需要 {VECTOR_MODULE}.py（由阶段二向量检索任务实现），"
            f"当前无法导入: {e}"
        ) from e
    cls = getattr(mod, VECTOR_CLASS, None)
    if cls is None:
        raise SystemExit(
            f"[error] {VECTOR_MODULE}.py 未提供 {VECTOR_CLASS} 类，不符合共享契约：\n"
            f"  {VECTOR_CLASS}.search(db, project_id, query, *, source_kinds=None, "
            f"limit=10, chapter_num=None) -> list[RetrievalHit]"
        )
    try:
        retriever = cls()
    except TypeError as e:
        raise SystemExit(f"[error] 实例化 {VECTOR_CLASS} 失败: {e}") from e
    if not callable(getattr(retriever, "search", None)):
        raise SystemExit(f"[error] {VECTOR_CLASS} 缺少 async search(...) 方法，不符合共享契约")
    return retriever


async def search_vector(retriever, db, project_id: str, query: str, args) -> list[dict]:
    """调用 VectorRetriever.search 并映射为 dict 列表（契约与 lexical 一致）。"""
    hits = await retriever.search(
        db,
        project_id,
        query,
        source_kinds=args.source_kinds,
        limit=args.limit,
        chapter_num=args.chapter_num,     # None = 不限定
    )
    return [retrieval_hit_to_dict(h) for h in hits]


async def search_hybrid_retriever(db, project_id: str, query: str, args) -> list[dict]:
    """调用 retrieval_reranker.search_hybrid（RRF 三通道融合）并映射为 dict 列表。"""
    from services.retrieval_reranker import search_hybrid

    hits = await search_hybrid(
        db,
        project_id,
        query,
        limit=args.limit,
        source_kinds=args.source_kinds,
        chapter_num=args.chapter_num,     # None = 不限定
    )
    return [retrieval_hit_to_dict(h) for h in hits]


# ──────────────────────────────────────────────────────────
# 指标
# ──────────────────────────────────────────────────────────

def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(0.95 * len(s)) - 1))
    return s[idx]


def aggregate(records: list[dict]) -> dict:
    """从样本记录汇总指标。records 每项含 hit / mrr / precision / latency_ms。"""
    if not records:
        return {
            "n_samples": 0, "recall_at_k": 0.0, "mrr": 0.0,
            "precision_at_k": 0.0, "false_recall_rate": 0.0, "p95_latency_ms": 0.0,
        }
    recall = sum(1 for r in records if r["hit"]) / len(records)
    mrr = sum(r["mrr"] for r in records) / len(records)
    prec = sum(r["precision"] for r in records) / len(records)
    latencies = [r["latency_ms"] for r in records]
    return {
        "n_samples": len(records),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "precision_at_k": round(prec, 4),
        "false_recall_rate": round(1 - prec, 4),
        "p95_latency_ms": round(_p95(latencies), 1),
    }


def group_summaries(records: list[dict]) -> dict:
    """按 category / difficulty 分组汇总。"""
    by_category: dict[str, list[dict]] = {}
    by_difficulty: dict[str, list[dict]] = {}
    for r in records:
        by_category.setdefault(r["category"], []).append(r)
        by_difficulty.setdefault(r["difficulty"], []).append(r)
    return {
        "by_category": {k: aggregate(v) for k, v in sorted(by_category.items())},
        "by_difficulty": {k: aggregate(v) for k, v in sorted(by_difficulty.items())},
    }


def print_summary_table(title: str, grouped: dict[str, dict]) -> None:
    """控制台打印分组汇总表。"""
    if not grouped:
        return
    print(f"\n── {title} ──")
    header = f"{'group':<22}{'n':>4}{'recall@k':>10}{'MRR':>8}{'prec@k':>9}{'err':>8}{'p95(ms)':>10}"
    print(header)
    print("-" * len(header))
    for name, agg in grouped.items():
        print(
            f"{name:<22}{agg['n_samples']:>4}"
            f"{agg['recall_at_k']:>10.3f}{agg['mrr']:>8.3f}"
            f"{agg['precision_at_k']:>9.3f}{agg['false_recall_rate']:>8.3f}"
            f"{agg['p95_latency_ms']:>10.1f}"
        )


# ──────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────

async def run_eval(args, project_id: str, samples: list[dict]) -> list[dict]:
    """逐样本调用所选检索器，返回样本记录列表。

    ilike   → services.knowledge_source.search_project_knowledge（既有逻辑）
    lexical → services.lexical_retriever.LexicalRetriever（函数内延迟导入）
    vector  → services.vector_retriever.VectorRetriever（阶段二，要求向量已索引）
    hybrid  → services.retrieval_reranker.search_hybrid（RRF 三通道融合，阶段二）
    各模式的命中判定、指标计算、输出结构完全一致。
    """
    # 延迟导入：DATABASE_URL 环境变量必须在导入 settings/db.session 之前设置
    from db.session import async_session

    if args.retriever == "lexical":
        retriever = get_lexical_retriever()

        async def search_one(db, query: str) -> list[dict]:
            return await search_lexical(retriever, db, project_id, query, args)
    elif args.retriever == "vector":
        retriever = get_vector_retriever()

        async def search_one(db, query: str) -> list[dict]:
            return await search_vector(retriever, db, project_id, query, args)
    elif args.retriever == "hybrid":

        async def search_one(db, query: str) -> list[dict]:
            return await search_hybrid_retriever(db, project_id, query, args)
    else:
        from services.knowledge_source import search_project_knowledge

        async def search_one(db, query: str) -> list[dict]:
            return await search_project_knowledge(db, project_id, query, limit=args.limit)

    records: list[dict] = []
    async with async_session() as db:
        for idx, sample in enumerate(samples, start=1):
            t0 = time.perf_counter()
            results = await search_one(db, sample["query"])
            latency_ms = (time.perf_counter() - t0) * 1000

            expected = sample.get("expected") or {}
            topk = results[: args.limit]

            hit_flags = [is_hit(r, expected) for r in topk]
            hit = any(hit_flags)
            first_rank = next((i + 1 for i, h in enumerate(hit_flags) if h), None)
            mrr = 1.0 / first_rank if first_rank else 0.0
            denom = min(args.limit, len(topk)) if topk else 0
            precision = (sum(hit_flags) / denom) if denom else 0.0

            out_results: list[dict] = []
            for r, h in zip(topk, hit_flags):
                item = {
                    "source_id": r.get("source_id"),
                    "chunk_id": r.get("chunk_id"),
                    "title": r.get("title"),
                    "score": r.get("score"),
                    "matched_query": r.get("matched_query"),
                    "hit": h,
                    "snippet": (r.get("snippet") or "")[:200],
                }
                # lexical 结果额外携带的字段（ilike 结果无这些键，输出结构保持不变）
                for extra in ("chapter_no", "source_kind", "rank"):
                    if r.get(extra) is not None:
                        item[extra] = r.get(extra)
                out_results.append(item)

            records.append({
                "query_id": sample["query_id"],
                "category": sample.get("category", "unknown"),
                "difficulty": sample.get("difficulty", "unknown"),
                "query": sample["query"],
                "expected": expected,
                "latency_ms": round(latency_ms, 1),
                "hit": bool(hit),
                "mrr": round(mrr, 4),
                "precision": round(precision, 4),
                "results": out_results,
            })

            flag = "HIT " if hit else "miss"
            print(
                f"  [{idx:>3}/{len(samples)}] {flag} {sample['query_id']:<12} "
                f"{sample.get('category', '?'):<24} mr={mrr:.2f} p={precision:.2f} "
                f"lat={latency_ms:>7.1f}ms"
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description="检索评测运行器：加载评测数据集 → 调检索器（ilike/lexical/vector/hybrid）→ 计算指标 → 保存结果"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径（默认 backend/dev-web.sqlite3）")
    parser.add_argument("--project", default=None, help="项目 ID；默认取库中第一个项目")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="评测数据集 JSON 路径（默认 datasets/regression_candidates.json；不存在时用内置冒烟样本）")
    parser.add_argument("--retriever", choices=["ilike", "lexical", "vector", "hybrid"], default="ilike",
                        help="检索器：ilike=现有关键词检索（默认，阶段零逻辑）；"
                             "lexical=services.lexical_retriever.LexicalRetriever（阶段一）；"
                             "vector=services.vector_retriever.VectorRetriever（阶段二，要求先索引向量，"
                             "见 retrieval_eval/run_vector_eval.py --index）；"
                             "hybrid=services.retrieval_reranker.search_hybrid（阶段二，RRF 三通道融合）")
    parser.add_argument("--embedding-provider", choices=["openai", "local"], default=None,
                        help="仅 vector/hybrid 生效：embedding provider（默认读 EMBEDDING_PROVIDER 环境变量，"
                             "两者都未设置时用 local 伪语义 embedding；openai 需配置 EMBEDDING_API_KEY）")
    parser.add_argument("--limit", type=int, default=5, help="检索 top-k（默认 5）")
    parser.add_argument("--source-kinds", default=None,
                        help="仅 lexical/vector/hybrid 生效：限定 source_kind，逗号分隔（如 source,note）；ilike 忽略")
    parser.add_argument("--chapter-num", type=int, default=None,
                        help="仅 lexical/vector/hybrid 生效：限定章节号，透传给检索器；ilike 忽略")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径（默认 results/baseline_<时间戳>.json，lexical/vector/hybrid 模式默认分别用对应前缀）")
    parser.add_argument("--max-samples", type=int, default=None, help="最多跑前 N 条样本（调试/大数据集时用）")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit 必须 >= 1")

    db_path = str(Path(args.db).resolve())
    if not Path(db_path).exists():
        raise SystemExit(f"数据库文件不存在: {db_path}")

    # 1. 项目解析（纯标准库，只读）
    project_id, project_title = fetch_first_project(db_path, args.project)

    # 1.5 仅 lexical/vector/hybrid 生效的参数预处理与提示
    if args.source_kinds:
        args.source_kinds = [s.strip() for s in args.source_kinds.split(",") if s.strip()] or None
    else:
        args.source_kinds = None
    if args.retriever == "ilike" and (args.source_kinds or args.chapter_num is not None):
        print("[warn] --source-kinds / --chapter-num 仅对 --retriever lexical/vector/hybrid 生效，ilike 模式忽略")

    # 1.6 vector/hybrid：确认向量已索引（只读检查），并设置 embedding provider
    if args.retriever in ("vector", "hybrid"):
        # 向量未索引时给出明确提示（返回 [] 会跑出全 0 指标，掩盖问题）
        try:
            conn = _readonly_conn(db_path)
            try:
                n_vec = conn.execute(
                    "SELECT COUNT(*) FROM retrieval_vectors WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
            except sqlite3.OperationalError:
                n_vec = 0
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — 只读检查失败不阻断评测
            n_vec = 0
        if n_vec == 0:
            print(
                f"[warn] 项目 {project_id} 在 retrieval_vectors 中没有任何向量："
                f"请先运行 retrieval_eval/run_vector_eval.py --index（或 index_project_vectors），"
                f"否则 vector/hybrid 检索将返回空结果。"
            )
        if args.embedding_provider:
            os.environ["EMBEDDING_PROVIDER"] = args.embedding_provider
        else:
            # 评测默认本地可复现 embedding；settings 在 import 时读环境变量
            os.environ.setdefault("EMBEDDING_PROVIDER", "local")
        print(f"[info] embedding_provider={os.environ.get('EMBEDDING_PROVIDER', 'local')}")

    # 2. 数据集加载（相对路径 cwd 找不到时回退 retrieval_eval/；都找不到 → 冒烟样本）
    samples, dataset_label, _ = load_dataset(str(resolve_eval_path(args.dataset)), project_id, db_path)
    if args.max_samples:
        samples = samples[: args.max_samples]

    # 3. 在 import 任何项目模块之前覆盖数据库连接（settings 在 import 时读环境变量）
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ.setdefault("DEBUG", "false")  # 压掉无关日志噪音

    # 4. 跑检索（内部延迟 import 项目模块）
    sys.path.insert(0, str(BACKEND_DIR))  # 保证能 import db/services/models（脚本位于 backend/ 下）
    print(f"[info] db={db_path}")
    print(f"[info] project={project_id} ({project_title})")
    print(f"[info] retriever={args.retriever}")
    print(f"[info] dataset={dataset_label}  samples={len(samples)}  top_k={args.limit}")
    t_start = time.perf_counter()
    records = asyncio.run(run_eval(args, project_id, samples))
    total_sec = time.perf_counter() - t_start

    # 5. 指标汇总
    summary = aggregate(records)
    summary["total_runtime_sec"] = round(total_sec, 2)
    grouped = group_summaries(records)

    # 6. 控制台汇总表
    print("\n════════ 汇总 ════════")
    s = summary
    print(
        f"n_samples={s['n_samples']}  recall@k={s['recall_at_k']:.3f}  "
        f"mrr={s['mrr']:.3f}  prec@k={s['precision_at_k']:.3f}  "
        f"err={s['false_recall_rate']:.3f}  p95={s['p95_latency_ms']:.1f}ms  "
        f"runtime={s['total_runtime_sec']}s"
    )
    print_summary_table("按 category 分组", grouped["by_category"])
    print_summary_table("按 difficulty 分组", grouped["by_difficulty"])

    # 7. 输出 JSON
    if args.out:
        out_path = resolve_eval_path(args.out, create=True)
    else:
        stem = {"lexical": "lexical", "vector": "vector", "hybrid": "hybrid"}.get(args.retriever, "baseline")
        out_path = DEFAULT_OUT_DIR / f"{stem}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    runner = (
        "run_baseline.py (ILIKE keyword baseline)"
        if args.retriever == "ilike"
        else f"run_baseline.py (retriever: {args.retriever})"
    )
    meta = {
        "runner": runner,
        "retriever": args.retriever,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": db_path,
        "project_id": project_id,
        "project_title": project_title,
        "dataset": dataset_label,
        "top_k": args.limit,
        "hit_rules": (
            "hit = any top-K result: source_id/chunk_id in expected.source_ids, "
            "or (evidence_type matches AND snippet/title contains any expected.entities); "
            "chapter_no compared when both present"
        ),
    }
    if args.retriever in ("vector", "hybrid"):
        meta["embedding"] = {
            "provider": os.environ.get("EMBEDDING_PROVIDER", "local"),
            "configured_dimension": os.environ.get("EMBEDDING_DIMENSION", ""),
            "note": "local 伪语义 embedding：字符 n-gram + IDF + feature hashing，"
                    "维度 = min(EMBEDDING_DIMENSION, 512)，见 services/embedding_provider.py",
        }
    payload = {
        "meta": meta,
        "summary": summary,
        "by_category": grouped["by_category"],
        "by_difficulty": grouped["by_difficulty"],
        "samples": records,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 结果已写入: {out_path}")


if __name__ == "__main__":
    sys.exit(main())
