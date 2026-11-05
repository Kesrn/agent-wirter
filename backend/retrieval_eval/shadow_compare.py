#!/usr/bin/env python3
"""旧 RAG 影子对比工具（退役流程第一步：feature flag + 影子对比，稳定后再删）。

背景
----
项目存在新旧两条上下文/检索链路：

- **新链路（主链路）**：``services/knowledge_source.search_project_knowledge``
  （``RETRIEVAL_MODE=lexical`` 时经 ``retrieval_policy`` 分发到
  ``services.lexical_retriever.LexicalRetriever``，读 ``retrieval_chunks`` /
  ``retrieval_chunks_fts`` 检索投影表）；
- **旧链路（退役候选）**：``rag/context_loader.ContextLoader`` ——
  ``search_similar``（world_entries / characters 的 embedding 内存余弦，
  embedding 为 SHA256 哈希伪向量）与 ``load_context``（仍挂在
  ``agents/workflow.py`` 作最终 fallback）。

本工具对同一项目 + 同一查询列表分别跑两条链路，输出：
1. **内容重合度**：两链路返回证据的 重叠数 / 并集；
2. **新链路对旧链路关键结果的覆盖**：旧链路 top-N 中有多少出现在新链路 top-K；
3. **耗时对比**：两链路各自 mean / p95 / max 延迟。

安全约束（与 retrieval_eval 既有脚本一致）
-----------------------------------------
- dev-web.sqlite3 全程只读（sqlite3 用 mode=ro；ORM 侧只发 SELECT，绝不 commit）；
- 不修改 services/ / models/ / rag/ / agents/ 任何现有代码；
- 旧链路经 ``db.session.set_engine`` 指向目标库（默认同一 dev 库，只读 SELECT）。

前置条件
--------
新链路的检索投影必须已建立：``retrieval_chunks`` 表有该项目数据。
缺失时本工具直接提示先跑 ``retrieval_eval/index_dev_db.py`` 并退出（码 2）。

退出码约定（v2 有效性门控，2026-08 起）
--------------------------------------
- 0：正常完成，报告含判定字段：
  * ``eligible=true`` 且 ``valid_for_retirement_decision=true`` → 含重合度指标，
    可计入 7 天观察期统计；
  * ``eligible=false``（目标项目无旧链路语料）→ 合法判定，报告不含重合度指标，
    不可计入观察期（观察期从找到有旧链路数据的项目起算）；
- 2：新链路检索投影缺失（``retrieval_chunks`` 无该项目数据），未到判定阶段；
- 3：旧链路目标库 schema 不兼容（world_entries / characters 缺 ORM 模型列），
  报告 ``valid_for_retirement_decision=false``，非零退出，不得用于观察期统计。

判定顺序（启动时门控，未通过则不运行两条链路、不产出重合度指标）：
新链路索引检查（码 2）→ schema 兼容性（码 3）→ 旧链路语料门槛（码 0）。

用法
----
    cd backend
    ./venv/bin/python retrieval_eval/shadow_compare.py                          # 内置 10 条演示查询
    ./venv/bin/python retrieval_eval/shadow_compare.py --queries q.json --out results/shadow.json
    ./venv/bin/python retrieval_eval/shadow_compare.py --old-limit 10 --limit 8

--queries JSON 格式：顶层为数组或 {"queries": [...]}，每条 {"query_id", "category",
"query", "notes"}（query 必填）。未传时使用内置 10 条演示查询（基于 dev-web.sqlite3
项目 d62d4447 的真实小说内容构造）。
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

# ── 路径与环境 ──────────────────────────────────────────

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"

# 内置演示查询：全部基于 dev-web.sqlite3 项目 d62d4447（全职法师全文）的真实内容，
# 实体/事件名均先 SELECT 核对（莫凡/雷系/火系/觉醒/水兰中学/莫家兴/穆贺/唐月/
# 张小侯/猴子/风轨/魔法世家 等均在库内真实出现）。
DEMO_QUERIES: list[dict] = [
    {"query_id": "shadow-001", "category": "ability", "query": "莫凡觉醒的是什么系？",
     "notes": "角色能力：莫凡觉醒雷系/火系，正文高频出现"},
    {"query_id": "shadow-002", "category": "ability", "query": "张小侯的风系初阶技能叫什么？",
     "notes": "角色能力：风系初阶技能=风轨（第19章『风之力，风轨！』）"},
    {"query_id": "shadow-003", "category": "relationship", "query": "莫凡的父亲是谁？",
     "notes": "人物关系：莫家兴"},
    {"query_id": "shadow-004", "category": "event", "query": "魔法觉醒仪式在哪里举行？",
     "notes": "事件：水兰中学开学典礼上的魔法觉醒仪式"},
    {"query_id": "shadow-005", "category": "event", "query": "新来的女老师是谁？",
     "notes": "事件：第16章新女老师唐月"},
    {"query_id": "shadow-006", "category": "world", "query": "元素魔法有哪些系？",
     "notes": "世界观：元素魔法七系（冰火雷土水风光…）"},
    {"query_id": "shadow-007", "category": "alias", "query": "猴子是什么系的法师？",
     "notes": "别名：猴子=张小侯，风系"},
    {"query_id": "shadow-008", "category": "event", "query": "穆贺和莫家兴谈了什么？",
     "notes": "事件/对话：穆贺登门与莫家兴交谈"},
    {"query_id": "shadow-009", "category": "world", "query": "这个世界里魔法体系的规则是什么？",
     "notes": "世界观：魔法体系规则（法师可觉醒多种系）"},
    {"query_id": "shadow-010", "category": "character", "query": "莫凡在哪里读中学？",
     "notes": "角色背景：水兰中学"},
]


def _readonly_conn(db_path: str) -> sqlite3.Connection:
    """只读打开 SQLite（mode=ro 时 SQLite 本身拒绝任何写操作）。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def fetch_first_project(db_path: str, requested: str | None) -> tuple[str, str]:
    """返回 (project_id, project_title)；未指定时取库中第一行项目。"""
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
            raise SystemExit("数据库中没有任何项目（projects 表为空）")
        pid, title = row
        if not requested:
            print(f"[info] 未指定 --project，使用库中第一个项目: {pid} ({col}={title!r})")
        return str(pid), str(title or "")
    finally:
        conn.close()


def check_new_chain_index(db_path: str, project_id: str) -> tuple[int, int]:
    """确认新链路检索投影就绪（只读）。返回 (retrieval_chunks 行数, 向量行数)。"""
    conn = _readonly_conn(db_path)
    try:
        try:
            n_chunks = conn.execute(
                "SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n_chunks = 0
        try:
            n_vec = conn.execute(
                "SELECT COUNT(*) FROM retrieval_vectors WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n_vec = 0
        return int(n_chunks), int(n_vec)
    finally:
        conn.close()


async def check_old_chain_schema(db_path: str, pg_url: str | None) -> dict:
    """旧链路目标库的 schema 兼容性诊断（只读）。

    旧链路 ContextLoader 用 ORM select(WorldEntry)/select(Character) 全列查询，
    若目标库表缺少 ORM 模型列（schema 漂移，如 dev sqlite 的 world_entries
    缺少 chapter_sequence_number），search_similar/load_context 内部捕获异常后
    静默返回空结果。这里显式比对 ORM 列 vs 实际表列，把"旧链路出错"与
    "旧链路无数据"区分开。

    返回 {"world_entries_missing": [...], "characters_missing": [...], "dialect": ...}。
    """
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

    from models.character import Character
    from models.world_entry import WorldEntry

    # 只读打开目标库（sqlite mode=ro；PG 走连接串）
    if pg_url:
        engine = _create_engine(pg_url)
        try:
            async with engine.connect() as conn:
                missing: dict[str, list[str]] = {}
                for table, model in (("world_entries", WorldEntry), ("characters", Character)):
                    actual = {
                        r[0]
                        for r in (
                            await conn.execute(_text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name=:t"
                            ), {"t": table})
                        ).all()
                    }
                    model_cols = {c.name for c in model.__table__.columns}
                    missing[table] = sorted(model_cols - actual)
                return {"world_entries_missing": missing["world_entries"],
                        "characters_missing": missing["characters"], "dialect": "postgresql"}
        finally:
            await engine.dispose()

    conn = _readonly_conn(db_path)
    try:
        def _missing(table: str, model_cols: set[str]) -> list[str]:
            try:
                actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.OperationalError:
                return ["<表不存在>"]
            return sorted(model_cols - actual)
        return {
            "world_entries_missing": _missing("world_entries", {c.name for c in WorldEntry.__table__.columns}),
            "characters_missing": _missing("characters", {c.name for c in Character.__table__.columns}),
            "dialect": "sqlite",
        }
    finally:
        conn.close()


def check_old_chain_data(db_path: str, project_id: str) -> dict:
    """旧链路数据可用性诊断（只读，不写库）。"""
    conn = _readonly_conn(db_path)
    try:
        def _count(table: str, emb: bool = False) -> int:
            try:
                if emb:
                    return int(conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE project_id = ? AND embedding IS NOT NULL", (project_id,)
                    ).fetchone()[0])
                return int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
                ).fetchone()[0])
            except sqlite3.OperationalError:
                return 0
        return {
            "world_entries": _count("world_entries"),
            "world_entries_with_embedding": _count("world_entries", emb=True),
            "characters": _count("characters"),
            "characters_with_embedding": _count("characters", emb=True),
            "chapters": _count("chapters"),
            "project_sources": _count("project_sources"),
        }
    finally:
        conn.close()


async def check_old_chain_data_pg(pg_url: str, project_id: str) -> dict:
    """旧链路目标库为 PostgreSQL 时的数据可用性诊断（只读 SELECT）。

    与 check_old_chain_data 同构（同样的 key），供 --old-db-url 指向 PG 时对真实
    旧链路目标库做语料门槛判定。PG 不可达时抛错（与 schema 检查行为一致）。
    """
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

    engine = _create_engine(pg_url)
    try:
        async with engine.connect() as conn:
            async def _count(table: str, emb: bool = False) -> int:
                sql = (f"SELECT COUNT(*) FROM {table} WHERE project_id = :pid"
                       + (" AND embedding IS NOT NULL" if emb else ""))
                return int((await conn.execute(_text(sql), {"pid": project_id})).scalar_one())

            return {
                "world_entries": await _count("world_entries"),
                "world_entries_with_embedding": await _count("world_entries", emb=True),
                "characters": await _count("characters"),
                "characters_with_embedding": await _count("characters", emb=True),
                "chapters": await _count("chapters"),
                "project_sources": await _count("project_sources"),
            }
    finally:
        await engine.dispose()


def schema_ok(old_schema: dict) -> bool:
    """旧链路目标库 schema 是否兼容（world_entries / characters 无缺列）。"""
    return not bool(
        old_schema.get("world_entries_missing") or old_schema.get("characters_missing")
    )


def ineligibility_reason(old_diag: dict) -> str | None:
    """旧链路语料门槛判定：world_entries / characters 行数为 0 即不具备
    影子比较资格（业务证据完全不存在，重合度无意义）。返回 None 表示通过门槛。

    embedding 数量不作为门槛：旧 RAG 的 SHA256 哈希伪向量已整体清理
    （检索质量差、无保留价值），旧链路 search_similar 将返回空结果，
    load_context 走全量 fallback——这本身就是旧链路的真实退役前行为，
    影子对比继续有效。
    """
    zero_items = [
        f"{k}=0"
        for k in ("world_entries", "characters")
        if old_diag.get(k, 0) == 0
    ]
    if not zero_items:
        return None
    return f"项目无旧链路语料，不具备影子比较资格（{', '.join(zero_items)}）"


def write_report(payload: dict, out: str) -> None:
    """把报告 JSON 写入 --out 路径（相对路径先按 retrieval_eval/ 解析）。"""
    out_path = Path(out)
    if not out_path.is_absolute() and not out_path.parent.exists():
        out_path = EVAL_DIR / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 结果已写入: {out_path}")


# ── 查询加载 ────────────────────────────────────────────

def load_queries(path: str | None) -> list[dict]:
    """加载查询列表：--queries JSON（数组或 {"queries": [...]}）；未传用内置演示。"""
    if not path:
        print(f"[info] 使用内置 {len(DEMO_QUERIES)} 条演示查询（基于 d62d4447 真实内容）")
        return list(DEMO_QUERIES)
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"--queries 文件不存在: {p}")
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else raw.get("queries") or raw.get("samples") or []
    if not items:
        raise SystemExit(f"--queries 文件 {p} 中没有查询（顶层数组或 queries 字段）")
    valid = []
    for it in items:
        if it.get("query"):
            valid.append({
                "query_id": it.get("query_id", f"q-{len(valid) + 1:03d}"),
                "category": it.get("category", "unknown"),
                "query": it["query"],
                "notes": it.get("notes", ""),
            })
    if not valid:
        raise SystemExit(f"--queries 文件 {p} 中没有有效查询（缺少 query 字段）")
    print(f"[info] 加载查询 {len(valid)} 条: {p}")
    return valid


# ── 证据归一与重合判定 ──────────────────────────────────

def _norm(text: str) -> str:
    """空白归一化（用于文本指纹比较）。"""
    return " ".join((text or "").split())


def new_result_fingerprints(r: dict) -> list[str]:
    """新链路结果 → 指纹（source_id / chunk_id / title+snippet）。"""
    fps = []
    if r.get("source_id"):
        fps.append(f"id:{r['source_id']}")
    if r.get("chunk_id"):
        fps.append(f"chunk:{r['chunk_id']}")
    title = _norm(r.get("title") or "")
    snippet = _norm(r.get("snippet") or "")
    if title:
        fps.append(f"text:{title}")
    if title and snippet:
        fps.append(f"text:{title}|{snippet[:150]}")
    return fps


def old_result_fingerprints(r: dict) -> list[str]:
    """旧链路结果 → 指纹（source_id / title+content）。"""
    fps = []
    if r.get("source_id"):
        fps.append(f"id:{r['source_id']}")
    title = _norm(r.get("title") or r.get("name") or "")
    content = _norm(r.get("content") or r.get("profile") or "")
    if title:
        fps.append(f"text:{title}")
    if content:
        fps.append(f"text:{content[:150]}")
    if title and content:
        fps.append(f"text:{title}|{content[:150]}")
    return fps


def results_overlap(new_results: list[dict], old_results: list[dict]) -> tuple[list[tuple[int, int]], set[str]]:
    """计算两链路结果集的证据重叠（贪心匹配）。

    匹配规则（任一条命中即视为同一证据）：
    1. source_id 相同（ID 级）；
    2. 标题指纹相同；
    3. 标题+snippet/content 指纹相同；
    4. 文本包含关系（一方标题/片段包含另一方，且较短文本 >= 20 字符）。

    返回 (matched_pairs, overlap_reasons)。matched_pairs 为 (new_idx, old_idx) 列表。
    """
    new_fps = [new_result_fingerprints(r) for r in new_results]
    old_fps = [old_result_fingerprints(r) for r in old_results]

    def _texts(fps: list[str]) -> list[str]:
        return [f[5:] for f in fps if f.startswith("text:")]

    pairs: list[tuple[int, int]] = []
    reasons: set[str] = set()
    used_old: set[int] = set()
    for ni, nfps in enumerate(new_fps):
        for oi, ofps in enumerate(old_fps):
            if oi in used_old:
                continue
            # ID 级
            if any(f in ofps for f in nfps if f.startswith(("id:", "chunk:"))):
                pairs.append((ni, oi)); used_old.add(oi); reasons.add("id")
                break
            # 指纹相等
            if set(nfps) & set(ofps):
                pairs.append((ni, oi)); used_old.add(oi); reasons.add("fingerprint")
                break
            # 文本包含（较短 >= 20 字符）
            n_texts = _texts(nfps)
            o_texts = _texts(ofps)
            matched = False
            for nt in n_texts:
                for ot in o_texts:
                    if len(nt) < 20 and len(ot) < 20:
                        continue
                    if nt and ot and (nt in ot or ot in nt):
                        pairs.append((ni, oi)); used_old.add(oi)
                        reasons.add("containment"); matched = True
                        break
                if matched:
                    break
            if matched:
                break
    return pairs, reasons


# ── 两条链路执行 ────────────────────────────────────────

async def run_new_chain(db_path: str, project_id: str, queries: list[dict], limit: int) -> tuple[list[dict], float]:
    """新链路：search_project_knowledge（RETRIEVAL_MODE=lexical，读 retrieval_chunks）。

    先跑一条 warmup 查询（构建项目分词器的一次性开销不计入逐查询耗时），
    返回 (每查询记录, warmup_latency_ms)。
    每查询记录：{query_id, latency_ms, results: [ {source_kind, source_id, chunk_id,
    title, snippet, score, matched_query} ]}。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from services.knowledge_source import search_project_knowledge

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    records: list[dict] = []
    warmup_ms = 0.0
    try:
        async with factory() as db:
            # warmup：项目分词器词汇表构建只发生一次，不应计入稳态耗时
            t0 = time.perf_counter()
            await search_project_knowledge(db, project_id, "莫凡", limit=3, intent="general")
            warmup_ms = (time.perf_counter() - t0) * 1000
            for q in queries:
                t0 = time.perf_counter()
                results = await search_project_knowledge(
                    db, project_id, q["query"], limit=limit, intent="general"
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                records.append({
                    "query_id": q["query_id"],
                    "latency_ms": round(latency_ms, 2),
                    "results": results,
                })
    finally:
        await engine.dispose()
    return records, round(warmup_ms, 2)


async def run_old_chain(
    db_path: str, project_id: str, queries: list[dict],
    old_limit: int, old_db_url: str | None,
) -> tuple[list[dict], dict]:
    """旧链路：ContextLoader.search_similar（world_entries/characters 哈希伪向量检索）。

    旧链路经 db.session.set_engine 指向目标库（默认同一 dev 库；--old-db-url 可换，
    如生产 PG）。同时跑一次 load_context(project_id, chapter_id=None) 观察上下文
    组装行为（workflow.py fallback 的实际调用形态）。

    返回 (每查询记录, load_context 诊断)。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from db.session import set_engine
    from rag.context_loader import ContextLoader

    engine = create_async_engine(
        old_db_url if old_db_url else f"sqlite+aiosqlite:///{db_path}"
    )
    set_engine(engine)  # 旧链路内部使用全局 async_session，替换为其目标库引擎
    loader = ContextLoader()

    records: list[dict] = []
    try:
        for q in queries:
            t0 = time.perf_counter()
            results = await loader.search_similar(project_id, q["query"], top_k=old_limit)
            latency_ms = (time.perf_counter() - t0) * 1000
            records.append({
                "query_id": q["query_id"],
                "latency_ms": round(latency_ms, 2),
                "results": results,
            })

        ctx_diag: dict = {"chapter_id": None, "load_context_kwargs": {
            "include_world": True, "include_characters": True,
            "include_previous_chapters": 3, "include_outline": True}}
        t0 = time.perf_counter()
        context_text = await loader.load_context(project_id, chapter_id=None)
        ctx_diag["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        ctx_diag["context_len"] = len(context_text)
        ctx_diag["context_preview"] = context_text[:200]
    finally:
        await engine.dispose()
    return records, ctx_diag


# ── 指标与输出 ──────────────────────────────────────────

def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(0.95 * len(s)) - 1))
    return s[idx]


def latency_stats(records: list[dict]) -> dict:
    lat = [r["latency_ms"] for r in records]
    return {
        "mean_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
        "p95_ms": round(_p95(lat), 2),
        "max_ms": round(max(lat), 2) if lat else 0.0,
    }


def summarize(per_query: list[dict], n_queries: int) -> dict:
    ratios = [r["overlap_ratio"] for r in per_query]
    return {
        "n_queries": n_queries,
        "queries_with_old_results": sum(1 for r in per_query if r["old_count"] > 0),
        "queries_with_overlap": sum(1 for r in per_query if r["overlap"] > 0),
        "mean_overlap_ratio": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        "total_overlap": sum(r["overlap"] for r in per_query),
        "total_union": sum(r["union"] for r in per_query),
        "new_covers_old_any": round(
            sum(1 for r in per_query if r["new_covers_old_ratio"] and r["new_covers_old_ratio"] >= 1.0)
            / n_queries, 4) if n_queries else 0.0,
        "new_covers_old_mean": round(
            sum(r["new_covers_old_ratio"] for r in per_query) / n_queries, 4) if n_queries else 0.0,
        "old_covers_new_mean": round(
            sum(r["old_covers_new_ratio"] for r in per_query) / n_queries, 4) if n_queries else 0.0,
        "new_latency": latency_stats(per_query),
        "old_latency": latency_stats(per_query),
    }


async def run(
    db_path: str, project_id: str, project_title: str, queries: list[dict],
    limit: int, old_limit: int, old_db_url: str | None, out: str | None, verbose: bool,
) -> int:
    # 0) 前置检查：新链路检索投影就绪
    n_chunks, n_vec = check_new_chain_index(db_path, project_id)
    if n_chunks == 0:
        print(
            f"[error] 项目 {project_id} 在 {db_path} 的 retrieval_chunks 中没有任何索引数据，"
            "无法跑新链路 lexical 检索。\n"
            "        请先运行: cd backend && ./venv/bin/python retrieval_eval/index_dev_db.py",
            file=sys.stderr,
        )
        return 2
    print(f"[info] 新链路前置检查通过：retrieval_chunks={n_chunks} 行，retrieval_vectors={n_vec} 行")

    # 1) 旧链路数据可用性诊断（目标库 = 旧链路实际读取的库；PG 目标时走 PG 只读查询）
    if old_db_url and "postgres" in old_db_url:
        old_diag = await check_old_chain_data_pg(old_db_url, project_id)
    else:
        old_diag = check_old_chain_data(db_path, project_id)
    print("[info] 旧链路数据可用性（只读诊断）:")
    for k, v in old_diag.items():
        print(f"        {k} = {v}")

    # 1.5) 旧链路目标库 schema 兼容性（区分"旧链路出错"与"旧链路无数据"）
    old_schema = await check_old_chain_schema(db_path, old_db_url)
    schema_broken = not schema_ok(old_schema)
    print(f"[info] 旧链路目标库 schema 兼容性（{old_schema.get('dialect')}）:")
    for k in ("world_entries_missing", "characters_missing"):
        v = old_schema.get(k) or []
        print(f"        {k} = {v if v else '无（兼容）'}")

    # 1.6) 环境诊断块（门控报告与正式报告共用）
    environment = {
        "new_chain_index": {"retrieval_chunks": n_chunks, "retrieval_vectors": n_vec},
        "old_chain_data": old_diag,
        "old_chain_schema": old_schema,
        "old_chain_schema_broken": schema_broken,
    }
    meta_base = {
        "runner": "retrieval_eval/shadow_compare.py",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": db_path,
        "project_id": project_id,
        "project_title": project_title,
        "new_chain": "services.knowledge_source.search_project_knowledge "
                     "(RETRIEVAL_MODE=lexical -> LexicalRetriever, retrieval_chunks)",
        "old_chain": "rag/context_loader.ContextLoader.search_similar / load_context "
                     "(SHA256 哈希伪向量 embedding)",
        "top_k_new": limit,
        "top_n_old": old_limit,
        "old_db_url": old_db_url or ("同 dev 库: " + db_path),
        "matching": "ID 级 / 标题指纹 / 片段指纹 / 文本包含（较短>=20字符）任一命中即算同一证据",
        "retrieval_mode": os.environ.get("RETRIEVAL_MODE", "ilike"),
    }

    # 2) 门控 A：schema 不兼容 → 无法判定，退出码 3，绝不产出重合度指标
    if schema_broken:
        reason = ("旧链路目标库 schema 不兼容，旧链路无法执行"
                  "（缺列时 ContextLoader 内部异常后静默返回空，重合度无意义）: "
                  + "; ".join(
                      f"{k} 缺列 {v}" for k, v in (
                          ("world_entries", old_schema.get("world_entries_missing") or []),
                          ("characters", old_schema.get("characters_missing") or []),
                      ) if v
                  ))
        payload = {
            "meta": {**meta_base, "new_chain_warmup_latency_ms": None},
            "environment": {**environment, "load_context_diag": None},
            "valid_for_retirement_decision": False,
            "schema_ok": False,
            "eligible": False,
            "ineligibility_reason": reason,
        }
        print(f"\n[error] {reason}", file=sys.stderr)
        print("[error] 本报告 valid_for_retirement_decision=false，不得用于 7 天观察统计；"
              "退出码 3（非零）。", file=sys.stderr)
        print("[error] 修复目标库 schema 后重跑（dev 库可运行 "
              "./venv/bin/python retrieval_eval/fix_dev_schema.py）。", file=sys.stderr)
        if out:
            write_report(payload, out)
        return 3

    # 3) 门控 B：旧链路语料门槛 → 合法判定（eligible=false），退出码 0，无指标
    inel_reason = ineligibility_reason(old_diag)
    if inel_reason:
        payload = {
            "meta": {**meta_base, "new_chain_warmup_latency_ms": None},
            "environment": {**environment, "load_context_diag": None},
            "valid_for_retirement_decision": False,
            "schema_ok": True,
            "eligible": False,
            "ineligibility_reason": inel_reason,
        }
        print(f"\n[warn] {inel_reason}")
        print("[info] 不具备影子比较资格：不运行两条链路、不产生重合度指标。"
              "本判定合法（退出码 0），但报告不可计入 7 天观察"
              "（观察只统计 eligible=true 且 valid_for_retirement_decision=true 的报告）。")
        if out:
            write_report(payload, out)
        return 0

    # 4) 分别跑两条链路（已通过 schema 兼容与语料门槛，正常产出重合度指标）
    print(f"\n[run] 新链路：search_project_knowledge(RETRIEVAL_MODE=lexical)  top_k={limit}")
    new_records, new_warmup_ms = await run_new_chain(db_path, project_id, queries, limit)
    print(f"[run] 旧链路：ContextLoader.search_similar / load_context  top_n={old_limit}"
          + (f"  old_db={old_db_url}" if old_db_url else "（同一 dev 库）"))
    old_records, ctx_diag = await run_old_chain(
        db_path, project_id, queries, old_limit, old_db_url
    )

    # 3) 逐查询对比
    per_query: list[dict] = []
    header = (f"{'query_id':<12}{'category':<14}{'new_n':>6}{'old_n':>6}{'overlap':>8}"
              f"{'ratio':>8}{'newCovOld':>10}{'lat_new':>10}{'lat_old':>10}")
    print("\n" + "-" * len(header))
    print(header)
    print("-" * len(header))
    new_by_id = {r["query_id"]: r for r in new_records}
    old_by_id = {r["query_id"]: r for r in old_records}
    for q in queries:
        nrec = new_by_id.get(q["query_id"])
        orec = old_by_id.get(q["query_id"])
        new_results = (nrec or {}).get("results", [])
        old_results = (orec or {}).get("results", [])
        pairs, reasons = results_overlap(new_results, old_results)
        overlap = len(pairs)
        union = len(new_results) + len(old_results) - overlap
        ratio = overlap / union if union else 0.0
        new_covers_old = overlap / len(old_results) if old_results else None
        old_covers_new = overlap / len(new_results) if new_results else None
        per_query.append({
            "query_id": q["query_id"],
            "category": q["category"],
            "query": q["query"],
            "notes": q.get("notes", ""),
            "new_count": len(new_results),
            "old_count": len(old_results),
            "overlap": overlap,
            "union": union,
            "overlap_ratio": round(ratio, 4),
            "new_covers_old_ratio": round(new_covers_old, 4) if new_covers_old is not None else None,
            "old_covers_new_ratio": round(old_covers_new, 4) if old_covers_new is not None else None,
            "overlap_reasons": sorted(reasons),
            "new_latency_ms": (nrec or {}).get("latency_ms"),
            "old_latency_ms": (orec or {}).get("latency_ms"),
            "new_top": [{"source_id": r.get("source_id"), "chunk_id": r.get("chunk_id"),
                         "source_kind": r.get("source_kind"), "title": (r.get("title") or "")[:40],
                         "score": r.get("score")} for r in new_results],
            "old_top": [{"source_type": r.get("source_type"), "source_id": r.get("source_id"),
                         "title": (r.get("title") or r.get("name") or "")[:40],
                         "distance": r.get("distance")} for r in old_results],
        })
        cov = "N/A(旧空)" if new_covers_old is None else f"{new_covers_old:.2f}"
        print(
            f"{q['query_id']:<12}{q['category']:<14}{len(new_results):>6}{len(old_results):>6}"
            f"{overlap:>8}{ratio:>8.3f}{cov:>10}"
            f"{(nrec or {}).get('latency_ms', 0):>9.1f}ms{(orec or {}).get('latency_ms', 0):>9.1f}ms"
        )
        if verbose:
            print(f"        query: {q['query']}")
            print(f"        match_reasons={sorted(reasons) or '无'}")

    # 4) 汇总
    summary = {
        "n_queries": len(queries),
        "queries_with_old_results": sum(1 for r in per_query if r["old_count"] > 0),
        "queries_with_overlap": sum(1 for r in per_query if r["overlap"] > 0),
        "mean_overlap_ratio": round(
            sum(r["overlap_ratio"] for r in per_query) / len(per_query), 4) if per_query else 0.0,
        "total_overlap": sum(r["overlap"] for r in per_query),
        "total_union": sum(r["union"] for r in per_query),
        "new_covers_old_mean": round(
            sum(r["new_covers_old_ratio"] or 0.0 for r in per_query) / len(per_query), 4) if per_query else 0.0,
        "old_covers_new_mean": round(
            sum(r["old_covers_new_ratio"] or 0.0 for r in per_query) / len(per_query), 4) if per_query else 0.0,
        "new_latency": latency_stats(new_records),
        "old_latency": latency_stats(old_records),
    }
    print("\n" + "═" * 60)
    print("汇总（新链路 lexical vs 旧链路 ContextLoader）")
    print("═" * 60)
    s = summary
    print(f"查询数={s['n_queries']}  旧链路有结果的查询={s['queries_with_old_results']}  "
          f"有重合的查询={s['queries_with_overlap']}")
    print(f"内容重合度（重叠/并集）：均值 {s['mean_overlap_ratio']:.4f}  "
          f"（总重叠 {s['total_overlap']} / 总并集 {s['total_union']}）")
    print(f"新链路覆盖旧链路 top-N 均值：{s['new_covers_old_mean']:.4f}（旧链路无结果时按 0 计）")
    print(f"旧链路覆盖新链路 top-K 均值：{s['old_covers_new_mean']:.4f}")
    print(f"耗时（ms）新链路: mean={s['new_latency']['mean_ms']}  "
          f"p95={s['new_latency']['p95_ms']}  max={s['new_latency']['max_ms']}"
          f"（首查 warmup 建词表 {new_warmup_ms}ms 未计入）")
    print(f"耗时（ms）旧链路: mean={s['old_latency']['mean_ms']}  "
          f"p95={s['old_latency']['p95_ms']}  max={s['old_latency']['max_ms']}")
    if s["queries_with_old_results"] == 0:
        print("[解读] 旧链路 schema 兼容、语料非空（已过语料门槛），但逐查询 "
              "search_similar 无返回（可能均无命中）—— 重合度 0 是合法观测，"
              "需结合 old_chain_data 确认是否属于'旧链路空转'")
    print(f"\n[diag] load_context(chapter_id=None) 行为：len={ctx_diag['context_len']}  "
          f"lat={ctx_diag['latency_ms']}ms  preview={ctx_diag['context_preview']!r}")

    # 5) 输出 JSON（含有效性判定字段）
    payload = {
        "meta": {**meta_base, "new_chain_warmup_latency_ms": new_warmup_ms},
        "environment": {**environment, "load_context_diag": ctx_diag},
        "valid_for_retirement_decision": True,
        "schema_ok": True,
        "eligible": True,
        "ineligibility_reason": None,
        "summary": summary,
        "per_query": per_query,
    }
    if out:
        write_report(payload, out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="旧 RAG 影子对比：新链路(lexical) vs 旧链路(ContextLoader) 内容重合度/覆盖/耗时"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径（默认 backend/dev-web.sqlite3）")
    parser.add_argument("--project", default=None, help="项目 ID；默认取库中第一个项目")
    parser.add_argument("--queries", default=None, help="查询列表 JSON（数组或 {\"queries\": [...]}）；默认内置 10 条演示查询")
    parser.add_argument("--limit", type=int, default=5, help="新链路 top-k（默认 5）")
    parser.add_argument("--old-limit", type=int, default=5, help="旧链路 top-N（默认 5）")
    parser.add_argument("--old-db-url", default=None,
                        help="旧链路数据库连接串（默认同一 dev SQLite 库；如需 PG 请显式指定隔离测试库）")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径（默认仅控制台）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印查询与匹配原因")
    args = parser.parse_args()

    db_path = str(Path(args.db).resolve())
    if not Path(db_path).exists():
        raise SystemExit(f"数据库文件不存在: {db_path}")

    # 关键：在导入任何项目模块之前设置环境变量（settings 在 import 时读取）
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    # 默认 lexical 基线（与历史报告可比）；显式 RETRIEVAL_MODE=hybrid 时反映
    # 生产三通道链路（实体+词法+向量 RRF，需 EMBEDDING_* env + 向量数据）。
    os.environ.setdefault("RETRIEVAL_MODE", "lexical")
    os.environ.setdefault("DEBUG", "false")
    sys.path.insert(0, str(BACKEND_DIR))

    project_id, project_title = fetch_first_project(db_path, args.project)
    queries = load_queries(args.queries)
    print(f"[info] db={db_path}")
    print(f"[info] project={project_id} ({project_title})")
    print(f"[info] queries={len(queries)}  new_top_k={args.limit}  old_top_n={args.old_limit}")
    t_start = time.perf_counter()
    code = asyncio.run(run(
        db_path, project_id, project_title, queries,
        args.limit, args.old_limit, args.old_db_url, args.out, args.verbose,
    ))
    print(f"\n[ok] 总耗时 {time.perf_counter() - t_start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
