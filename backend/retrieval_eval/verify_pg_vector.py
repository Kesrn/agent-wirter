#!/usr/bin/env python3
"""PG 端向量检索实测（阶段二收尾：VectorRetriever 的 PG 分支实测）。

背景
----
``services.vector_retriever.VectorRetriever``（numpy 内存余弦）已在 SQLite dev 库
评测（recall 与词法持平 0.816），但 PG 分支此前未实测。本脚本在本地 PG 上：

1. 探测 PG 可达（失败 → 退出码 2，可跳过，不阻塞离线环境）；
2. 确认 ``retrieval_vectors`` 表存在（缺失时自动跑 ``alembic upgrade head``
   补齐；仍缺失则报错）；
3. 用**独立测试 project_id**（随机 UUID）在 PG 上：
   - 从 dev-web.sqlite3 **只读**导入前 N（默认 500）条真实 retrieval_chunks
     （含检索投影四表契约列；index_generation_id 打上哨兵
     ``pg-vector-verify`` 便于自愈清理）；
   - 同步导入对应的 retrieval_chunks_fts 行（title / chapter_no 供回表）；
   - ``index_project_vectors``（本地可复现 embedding provider，维度与
     SQLite 评测一致 = min(EMBEDDING_DIMENSION, 512)）→ 打印索引统计；
   - ``VectorRetriever.search`` 冒烟：断言命中非空、rank/score 合理、top-5 含
     真实实体证据；记录逐查询耗时；
4. **一致性对照**：把同一 500 条 chunks 载入内存 SQLite（同一 provider 实例、
   同一语料 → 向量完全一致），同查询跑 VectorRetriever，比对 PG / SQLite
   的命中 chunk_id 顺序与分数（应完全一致）；
5. **finally 清理**：删除本次测试 project 的全部行（retrieval_chunks /
   retrieval_chunks_fts / retrieval_vectors / 检索 generation 等，按精确
   project_id），断言零残留，不碰其他数据；源项目（dev 导入源）的 PG 向量行
   数在前后保持不变。

安全约束（与 verify_pg.py 同约定）
----------------------------------
- dev-web.sqlite3 全程只读（sqlite3 mode=ro）；
- PG 只写：测试 project_id 的行 + 哨兵 generation 标记；其余数据一律不动；
- 不修改 services/ / models/ 任何现有代码。

用法
----
    cd backend
    ./venv/bin/python retrieval_eval/verify_pg_vector.py --pg-url "$TEST_PG_URL"  # 全流程
    ./venv/bin/python retrieval_eval/verify_pg_vector.py --pg-url "$TEST_PG_URL" --n-chunks 200 --limit 5
    ./venv/bin/python retrieval_eval/verify_pg_vector.py \
        --pg-url "$TEST_PG_URL" \
        --out retrieval_eval/results/pg_vector_verify.json

退出码：0=全部通过，1=存在失败，2=PG 不可达（跳过）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"

# 测试行哨兵：retrieval_chunks.index_generation_id 打上该值，用于自愈清理
# （真实业务数据的 generation id 是 UUID 串，绝不会等于该哨兵）。
SENTINEL_GENERATION = "pg-vector-verify"

# 冒烟查询：全部基于导入窗口（dev 库前 500 块 = 第 1~15 章）真实内容，
# 实体/事件名已先 SELECT 核对（莫凡/张小侯/莫家兴/穆贺/唐月/觉醒/风轨 均在窗口内）。
SMOKE_QUERIES: list[dict] = [
    {"query_id": "pgv-001", "query": "莫凡觉醒的是什么系？", "entity": "莫凡",
     "category": "ability"},
    {"query_id": "pgv-002", "query": "张小侯的风系技能叫什么？", "entity": "张小侯",
     "category": "ability"},
    {"query_id": "pgv-003", "query": "莫家兴是莫凡的什么人？", "entity": "莫家兴",
     "category": "relationship"},
    {"query_id": "pgv-004", "query": "魔法觉醒仪式在哪里举行？", "entity": "觉醒",
     "category": "event"},
    {"query_id": "pgv-005", "query": "新来的女老师是谁？", "entity": "唐月",
     "category": "event"},
    {"query_id": "pgv-006", "query": "元素魔法有哪些系？", "entity": "魔法",
     "category": "world"},
    {"query_id": "pgv-007", "query": "穆贺和莫家兴谈了什么？", "entity": "穆贺",
     "category": "event"},
    # 无阈值行为验证：向量检索无相似度阈值，无关查询仍返回 top-k（记录行为，不做断言）
    {"query_id": "pgv-008", "query": "量子猫薄荷量子纠缠实验", "entity": None,
     "category": "no_match"},
]

# 清理涉及的检索表（全部按 project_id 精确隔离）
_CLEANUP_TABLES = (
    "retrieval_chunks_fts",
    "retrieval_chunks",
    "retrieval_vectors",
    "retrieval_index_source_state",
    "retrieval_index_generation",
)


def _readonly_conn(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def fetch_first_project(db_path: str, requested: str | None) -> tuple[str, str]:
    if requested:
        return requested, ""
    conn = _readonly_conn(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT id, title FROM projects ORDER BY rowid LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = conn.execute(
                "SELECT id, name FROM projects ORDER BY rowid LIMIT 1"
            ).fetchone()
        if not row:
            raise SystemExit("数据库中没有任何项目（projects 表为空）")
        pid, title = row
        if not requested:
            print(f"[info] 未指定 --source-project，使用库中第一个项目: {pid} ({title!r})")
        return str(pid), str(title or "")
    finally:
        conn.close()


def read_sqlite_window(db_path: str, project_id: str, n: int) -> tuple[list[dict], list[dict], int]:
    """只读 sqlite：取该项目前 n 条 retrieval_chunks + 对应 fts 行。

    返回 (chunk_rows, fts_rows, total_chunks)。chunk_rows 为 dict 列表
    （含 sqlite 侧全部契约列）。
    """
    conn = _readonly_conn(db_path)
    try:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM retrieval_chunks WHERE project_id = ?", (project_id,)
        ).fetchone()[0])
        rows = conn.execute(
            "SELECT id, project_id, source_kind, source_id, source_version_id, "
            "index_generation_id, chunk_index, content, search_text, content_hash, metadata "
            "FROM retrieval_chunks WHERE project_id = ? ORDER BY chunk_index LIMIT ?",
            (project_id, n),
        ).fetchall()
        chunk_rows = [
            {
                "id": str(r[0]), "project_id": str(r[1]), "source_kind": str(r[2]),
                "source_id": str(r[3]), "source_version_id": r[4],
                "index_generation_id": SENTINEL_GENERATION,  # 哨兵：自愈清理用
                "chunk_index": int(r[6]), "content": r[7] or "", "search_text": r[8] or "",
                "content_hash": str(r[9] or ""), "metadata": r[10],
            }
            for r in rows
        ]
        ids = [r["id"] for r in chunk_rows]
        if ids:
            ph = ",".join("?" * len(ids))
            fts_rows = [
                {
                    "chunk_id": str(f[0]), "project_id": str(f[1]),
                    "source_kind": str(f[2]), "source_id": str(f[3]),
                    "chapter_no": f[4], "title": f[5] or "", "content": f[6] or "",
                    "search_text": f[7] or "",
                }
                for f in conn.execute(
                    f"SELECT chunk_id, project_id, source_kind, source_id, chapter_no, "
                    f"title, content, search_text FROM retrieval_chunks_fts "
                    f"WHERE project_id = ? AND chunk_id IN ({ph})", (project_id, *ids)
                ).fetchall()
            ]
        else:
            fts_rows = []
        return chunk_rows, fts_rows, total
    finally:
        conn.close()


# ── PG 写入 / 清理 ───────────────────────────────────────

async def _pg_insert_chunks(db, chunks: list[dict]) -> None:
    """插入 retrieval_chunks（raw SQL，类型与 setup_pg.py 契约一致）。"""
    for c in chunks:
        await db.execute(
            text(
                "INSERT INTO retrieval_chunks (id, project_id, source_kind, source_id, "
                "source_version_id, index_generation_id, chunk_index, content, search_text, "
                "content_hash, metadata) VALUES (:id, :pid, :sk, :sid, :svid, :gid, :ci, "
                ":content, :st, :h, :meta)"
            ),
            {
                "id": uuid.UUID(c["id"]), "pid": uuid.UUID(c["project_id"]),
                "sk": c["source_kind"], "sid": c["source_id"], "svid": c["source_version_id"],
                "gid": c["index_generation_id"], "ci": c["chunk_index"],
                "content": c["content"], "st": c["search_text"], "h": c["content_hash"],
                "meta": c["metadata"],  # PG JSONB：None 或 dict（asyncpg 适配 JSONB 需 str/dict）
            },
        )


async def _pg_insert_fts(db, fts_rows: list[dict]) -> None:
    """插入 retrieval_chunks_fts（普通表，与 retrieval_indexer.PG_FTS_TABLE_CREATE_SQL 一致）。"""
    for f in fts_rows:
        await db.execute(
            text(
                "INSERT INTO retrieval_chunks_fts (chunk_id, project_id, source_kind, "
                "source_id, chapter_no, title, content, search_text) VALUES "
                "(:id, :pid, :sk, :sid, :cn, :title, :content, :st)"
            ),
            {
                "id": f["chunk_id"], "pid": f["project_id"], "sk": f["source_kind"],
                "sid": f["source_id"], "cn": f["chapter_no"], "title": f["title"],
                "content": f["content"], "st": f["search_text"],
            },
        )


async def _cleanup_pg(db, pids: list[str]) -> None:
    """删除测试 project 在全部检索表的行（按精确 project_id）。"""
    if not pids:
        return
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass
    ph = ", ".join(f":p{i}" for i in range(len(pids)))
    params = {f"p{i}": str(p) for i, p in enumerate(pids)}
    for table in _CLEANUP_TABLES:
        await db.execute(text(f"DELETE FROM {table} WHERE project_id IN ({ph})"), params)
    await db.commit()


async def _count_pg(db, pids: list[str]) -> dict[str, int]:
    if not pids:
        return {t: 0 for t in _CLEANUP_TABLES}
    ph = ", ".join(f":p{i}" for i in range(len(pids)))
    params = {f"p{i}": str(p) for i, p in enumerate(pids)}
    counts = {}
    for table in _CLEANUP_TABLES:
        n = (await db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE project_id IN ({ph})"), params
        )).scalar() or 0
        counts[table] = int(n)
    return counts


async def _heal_stale_pg(db) -> list[str]:
    """自愈：清理上次中断遗留的测试数据（以哨兵 generation 推导 project 后删除）。"""
    try:
        pids = (
            await db.execute(
                text("SELECT DISTINCT project_id FROM retrieval_chunks "
                     "WHERE index_generation_id = :s"), {"s": SENTINEL_GENERATION}
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return []
    pids = [str(p) for p in pids]
    if pids:
        await _cleanup_pg(db, pids)
        print(f"  [warn] 已清理上次中断遗留的测试数据: {len(pids)} 个 project")
    return pids


# ── 内存 SQLite 对照库（一致性验证） ────────────────────

async def _build_inmemory_sqlite(chunks: list[dict], fts_rows: list[dict]):
    """载入同一窗口数据的内存 SQLite，返回 (engine, session_factory)。"""
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        from models.base import Base
        from models.retrieval_index import RetrievalChunk
        from models.retrieval_vector import RetrievalVector

        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[RetrievalChunk.__table__, RetrievalVector.__table__],
            )
        )
        # fts 侧用普通表（VectorRetriever 只做 SELECT 回表 title/chapter_no）
        await conn.execute(text(
            "CREATE TABLE retrieval_chunks_fts (chunk_id TEXT, project_id TEXT, "
            "source_kind TEXT, source_id TEXT, chapter_no INTEGER, title TEXT, "
            "content TEXT, search_text TEXT)"
        ))
        for c in chunks:
            meta = c["metadata"]
            if meta is not None:
                try:
                    meta = json.loads(meta) if isinstance(meta, str) else meta
                except (ValueError, TypeError):
                    meta = {}
            await conn.execute(text(
                "INSERT INTO retrieval_chunks (id, project_id, source_kind, source_id, "
                "source_version_id, index_generation_id, chunk_index, content, search_text, "
                "content_hash, metadata) VALUES (:id, :pid, :sk, :sid, :svid, :gid, :ci, "
                ":content, :st, :h, :meta)"
            ), {
                "id": c["id"], "pid": c["project_id"], "sk": c["source_kind"],
                "sid": c["source_id"], "svid": c["source_version_id"],
                "gid": c["index_generation_id"], "ci": c["chunk_index"],
                "content": c["content"], "st": c["search_text"], "h": c["content_hash"],
                "meta": json.dumps(meta) if meta is not None else None,
            })
        for f in fts_rows:
            await conn.execute(text(
                "INSERT INTO retrieval_chunks_fts (chunk_id, project_id, source_kind, "
                "source_id, chapter_no, title, content, search_text) VALUES "
                "(:id, :pid, :sk, :sid, :cn, :title, :content, :st)"
            ), {
                "id": f["chunk_id"], "pid": f["project_id"], "sk": f["source_kind"],
                "sid": f["source_id"], "cn": f["chapter_no"], "title": f["title"],
                "content": f["content"], "st": f["search_text"],
            })
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


# ── 主流程 ──────────────────────────────────────────────

async def run(pg_url: str, sqlite_path: str, source_project: str, n_chunks: int,
              limit: int, queries: list[dict], out: str | None) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from services.embedding_provider import EmbeddingProvider
    from services.vector_retriever import VectorRetriever, index_project_vectors

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str) -> None:
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {name} — {detail}")
        if not cond:
            failures.append(f"{name}: {detail}")

    print(f"[verify-pg-vector] PG: {pg_url}")
    print(f"[verify-pg-vector] 源窗口: {sqlite_path} project={source_project} n_chunks={n_chunks}")

    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    test_pids: list[str] = []
    try:
        # 0) 连接探测（失败 → 退出码 2）
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] PostgreSQL 不可达（{exc.__class__.__name__}: {exc}），跳过验证")
            return 2

        # 1) 确认检索表存在（retrieval_vectors 缺失时跑 alembic upgrade head 补齐）
        async with engine.connect() as conn:
            tables = {}
            for t in ("retrieval_vectors", "retrieval_chunks", "retrieval_chunks_fts"):
                tables[t] = (await conn.execute(
                    text("SELECT to_regclass('public.' || :t)"), {"t": t}
                )).scalar()
        print("[verify-pg-vector] 检索表检查:")
        for t, reg in tables.items():
            print(f"        {t}: {'存在' if reg else '缺失'}")
        if not tables["retrieval_vectors"]:
            print("[verify-pg-vector] retrieval_vectors 缺失，运行 alembic upgrade head 补齐…")
            proc = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
                cwd=str(BACKEND_DIR), capture_output=True, text=True,
            )
            if proc.returncode != 0:
                print(f"[error] alembic upgrade head 失败:\n{proc.stderr[-2000:]}", file=sys.stderr)
                return 1
            async with engine.connect() as conn:
                tables["retrieval_vectors"] = (await conn.execute(
                    text("SELECT to_regclass('public.retrieval_vectors')")
                )).scalar()
            check("alembic 补齐 retrieval_vectors", bool(tables["retrieval_vectors"]),
                  "upgrade head 后表仍不存在")
        for t in ("retrieval_chunks", "retrieval_chunks_fts"):
            if not tables[t]:
                print(f"[error] PG 缺少 {t}，请先运行 retrieval_eval/setup_pg.py 建表", file=sys.stderr)
                return 1

        async with factory() as db:
            # 2) 自愈 + 前置安全检查（源项目向量行数应保持为 0）
            healed = await _heal_stale_pg(db)
            source_vec_before = (await db.execute(
                text("SELECT COUNT(*) FROM retrieval_vectors WHERE project_id = :p"),
                {"p": source_project},
            )).scalar() or 0
            print(f"[verify-pg-vector] 源项目 {source_project[:8]}.. 现有向量行: {source_vec_before}"
                  + (f"（已清理遗留测试 {len(healed)} 个 project）" if healed else ""))

            # 3) 只读 sqlite → 测试窗口
            chunks, fts_rows, total = read_sqlite_window(
                sqlite_path, source_project, n_chunks
            )
            if not chunks:
                print(f"[error] sqlite 项目 {source_project} 无 retrieval_chunks", file=sys.stderr)
                return 1
            n = min(n_chunks, len(chunks))
            print(f"[verify-pg-vector] sqlite 只读窗口: chunks={len(chunks)} fts={len(fts_rows)} "
                  f"（源项目共 {total} 块）")

            # 4) 写入 PG（独立测试 project_id）
            test_pid = str(uuid.uuid4())
            test_pids.append(test_pid)
            print(f"[verify-pg-vector] 测试 project_id: {test_pid}（哨兵 generation={SENTINEL_GENERATION}）")
            # 测试块用新 chunk id（避免与真实数据撞 uq），fts 侧 chunk_id 同步重映射
            id_map = {c["id"]: str(uuid.uuid4()) for c in chunks}
            for c in chunks:
                c["project_id"] = test_pid
                c["id"] = id_map[c["id"]]
            for f in fts_rows:
                f["project_id"] = test_pid
                f["chunk_id"] = id_map.get(f["chunk_id"], f["chunk_id"])
            t0 = time.perf_counter()
            await _pg_insert_chunks(db, chunks)
            await _pg_insert_fts(db, fts_rows)
            await db.commit()
            insert_sec = time.perf_counter() - t0
            print(f"[verify-pg-vector] PG 写入完成: chunks={len(chunks)} fts={len(fts_rows)} "
                  f"（{insert_sec:.2f}s）")

            # 5) 索引向量（本地 provider，与 SQLite 评测同维度）
            provider = EmbeddingProvider(provider="local")
            t0 = time.perf_counter()
            stats = await index_project_vectors(db, test_pid, provider=provider)
            pg_index_sec = time.perf_counter() - t0
            print(f"[verify-pg-vector] PG 向量索引: {stats}（{pg_index_sec:.2f}s）")
            check("PG 向量索引完成且条数正确",
                  stats["indexed"] == len(chunks) and stats["total"] == len(chunks),
                  f"indexed={stats['indexed']} total={stats['total']} 期望 {len(chunks)}")
            n_vec = (await db.execute(
                text("SELECT COUNT(*) FROM retrieval_vectors WHERE project_id = :p"),
                {"p": test_pid},
            )).scalar()
            check("PG retrieval_vectors 实际行数一致", int(n_vec) == len(chunks),
                  f"rows={n_vec}")

            # 6) 内存 SQLite 对照库（同一 provider 实例 → 同语料同向量）
            engine_s, factory_s = await _build_inmemory_sqlite(chunks, fts_rows)
            try:
                async with factory_s() as db_s:
                    t0 = time.perf_counter()
                    stats_s = await index_project_vectors(db_s, test_pid, provider=provider)
                    sqlite_index_sec = time.perf_counter() - t0
                    print(f"[verify-pg-vector] 内存 SQLite 对照索引: {stats_s}（{sqlite_index_sec:.2f}s）")
                    check("内存 SQLite 索引条数一致", stats_s["indexed"] == len(chunks),
                          f"indexed={stats_s['indexed']}")

                    # 7) 检索冒烟 + 一致性比对
                    retriever = VectorRetriever(provider=provider)
                    header = (f"{'query_id':<10}{'pg_n':>5}{'sqlite_n':>9}{'identical':>10}"
                              f"{'pg_ms':>9}{'sqlite_ms':>10}  entity_hit")
                    print("\n" + "-" * len(header))
                    print(header)
                    print("-" * len(header))
                    per_query: list[dict] = []
                    for q in queries:
                        t0 = time.perf_counter()
                        pg_hits = await retriever.search(db, test_pid, q["query"], limit=limit)
                        pg_ms = (time.perf_counter() - t0) * 1000
                        t0 = time.perf_counter()
                        sqlite_hits = await retriever.search(db_s, test_pid, q["query"], limit=limit)
                        sqlite_ms = (time.perf_counter() - t0) * 1000

                        pg_ids = [h.chunk_id for h in pg_hits]
                        sqlite_ids = [h.chunk_id for h in sqlite_hits]
                        identical = pg_ids == sqlite_ids and all(
                            abs(a.score - b.score) < 1e-6
                            for a, b in zip(pg_hits, sqlite_hits)
                        )
                        entity_hit = False
                        if q.get("entity"):
                            entity_hit = any(
                                q["entity"] in (h.content or "") for h in pg_hits[:5]
                            )
                        check(
                            f"{q['query_id']} 命中非空", bool(pg_hits),
                            f"pg_n={len(pg_hits)}",
                        )
                        if q.get("entity"):
                            check(
                                f"{q['query_id']} 实体证据命中",
                                entity_hit,
                                f"top-5 含 {q['entity']!r}: {entity_hit}",
                            )
                        check(
                            f"{q['query_id']} 顺序分数一致(PG vs SQLite)", identical,
                            f"ids_equal={pg_ids == sqlite_ids} scores_max_delta="
                            f"{max((abs(a.score - b.score) for a, b in zip(pg_hits, sqlite_hits)), default=0.0):.2e}",
                        )
                        print(
                            f"{q['query_id']:<10}{len(pg_hits):>5}{len(sqlite_hits):>9}"
                            f"{str(identical):>10}{pg_ms:>9.1f}{sqlite_ms:>10.1f}"
                            f"  {q.get('entity') or '-':<8}{entity_hit}"
                        )
                        per_query.append({
                            "query_id": q["query_id"], "category": q.get("category"),
                            "query": q["query"], "entity": q.get("entity"),
                            "pg_n": len(pg_hits), "sqlite_n": len(sqlite_hits),
                            "identical": identical,
                            "pg_latency_ms": round(pg_ms, 2),
                            "sqlite_latency_ms": round(sqlite_ms, 2),
                            "entity_hit": entity_hit,
                            "pg_top": [{"chunk_id": h.chunk_id, "score": h.score,
                                        "rank": h.rank, "title": (h.title or "")[:40],
                                        "chapter_no": h.chapter_no} for h in pg_hits],
                            "sqlite_top": [{"chunk_id": h.chunk_id, "score": h.score}
                                           for h in sqlite_hits],
                        })

            finally:
                await engine_s.dispose()
    finally:
        # 8) 清理（无论成败）
        async with factory() as db:
            await _cleanup_pg(db, test_pids)
            residue = await _count_pg(db, test_pids)
            dirty = {t: n for t, n in residue.items() if n}
            print("\n[verify-pg-vector] 清理终检（测试 project 残留）:")
            check("测试数据已全部清理，无残留", not dirty,
                  f"residue={dirty or '无'}")
            source_vec_after = (await db.execute(
                text("SELECT COUNT(*) FROM retrieval_vectors WHERE project_id = :p"),
                {"p": source_project},
            )).scalar() or 0
            check("源项目向量行数未被触碰", int(source_vec_after) == int(source_vec_before),
                  f"before={source_vec_before} after={source_vec_after}")
        await engine.dispose()

    # 9) 汇总输出
    print("\n" + "═" * 64)
    print("PG 向量检索实测汇总")
    print("═" * 64)
    pg_lats = [r["pg_latency_ms"] for r in per_query]
    sqlite_lats = [r["sqlite_latency_ms"] for r in per_query]
    ident = sum(1 for r in per_query if r["identical"])
    print(f"向量索引：PG {stats['indexed']}/{stats['total']} 条（{pg_index_sec:.2f}s），"
          f"内存 SQLite 对照 {stats_s['indexed']} 条（{sqlite_index_sec:.2f}s）")
    print(f"检索耗时（ms）PG: mean={sum(pg_lats)/len(pg_lats):.1f}  "
          f"p95={sorted(pg_lats)[min(len(pg_lats)-1, math.ceil(0.95*len(pg_lats))-1)]:.1f}  "
          f"max={max(pg_lats):.1f}")
    print(f"检索耗时（ms）SQLite: mean={sum(sqlite_lats)/len(sqlite_lats):.1f}  "
          f"p95={sorted(sqlite_lats)[min(len(sqlite_lats)-1, math.ceil(0.95*len(sqlite_lats))-1)]:.1f}  "
          f"max={max(sqlite_lats):.1f}")
    print(f"一致性：{ident}/{len(per_query)} 条查询命中 chunk_id 顺序与分数完全一致")
    print(f"PG 写入 {insert_sec:.2f}s（{len(chunks)} chunks + {len(fts_rows)} fts）")
    verdict = "全部通过" if not failures else f"{len(failures)} 项失败"
    print(f"结论: {verdict}")

    payload = {
        "meta": {
            "runner": "retrieval_eval/verify_pg_vector.py",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pg_url": pg_url,
            "sqlite_source": str(Path(sqlite_path).resolve()),
            "source_project": source_project,
            "test_project_id": test_pid,
            "sentinel_generation": SENTINEL_GENERATION,
            "n_chunks": len(chunks),
            "top_k": limit,
            "embedding_provider": "local",
            "embedding_dimension_note": "min(EMBEDDING_DIMENSION, 512)，与 SQLite 评测一致",
        },
        "checks": {
            "index": {"pg": stats, "pg_rows": int(n_vec),
                      "sqlite_inmemory": stats_s},
            "index_latency_sec": {"pg": round(pg_index_sec, 3),
                                  "sqlite_inmemory": round(sqlite_index_sec, 3)},
            "pg_insert_sec": round(insert_sec, 3),
            "search_latency_ms": {
                "pg": {"mean_ms": round(sum(pg_lats) / len(pg_lats), 2),
                       "p95_ms": round(sorted(pg_lats)[min(len(pg_lats) - 1, math.ceil(0.95 * len(pg_lats)) - 1)], 2),
                       "max_ms": round(max(pg_lats), 2)},
                "sqlite_inmemory": {"mean_ms": round(sum(sqlite_lats) / len(sqlite_lats), 2),
                                    "p95_ms": round(sorted(sqlite_lats)[min(len(sqlite_lats) - 1, math.ceil(0.95 * len(sqlite_lats)) - 1)], 2),
                                    "max_ms": round(max(sqlite_lats), 2)},
            },
            "consistency": {"queries_compared": len(per_query),
                            "identical_queries": ident},
            "failures": failures,
        },
        "per_query": per_query,
    }
    if out:
        out_path = Path(out)
        if not out_path.is_absolute() and not out_path.parent.exists():
            out_path = EVAL_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[ok] 结果已写入: {out_path}")
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PG 端向量检索实测：独立 project 导入真实 chunks → 索引 → 检索冒烟 + SQLite 一致性 + 耗时"
    )
    parser.add_argument("--pg-url", required=True,
                        help="必须显式指定隔离 PostgreSQL 测试库连接串")
    parser.add_argument("--sqlite", default=str(DEFAULT_DB), help="源 sqlite 数据库（只读）")
    parser.add_argument("--source-project", default=None,
                        help="源项目（从 sqlite 取前 N 块）；默认取库中第一个项目")
    parser.add_argument("--n-chunks", type=int, default=500, help="导入的 chunk 条数（默认 500）")
    parser.add_argument("--limit", type=int, default=5, help="检索 top-k（默认 5）")
    parser.add_argument("--queries", default=None, help="查询列表 JSON（可选，默认内置 8 条冒烟查询）")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    sqlite_path = str(Path(args.sqlite).resolve())
    if not Path(sqlite_path).exists():
        raise SystemExit(f"sqlite 数据库不存在: {sqlite_path}")

    sys.path.insert(0, str(BACKEND_DIR))  # 保证能 import db/services/models（脚本位于 backend/ 下）

    project_id, _ = fetch_first_project(sqlite_path, args.source_project)
    queries = list(SMOKE_QUERIES)
    if args.queries:
        with open(args.queries, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw if isinstance(raw, list) else raw.get("queries") or raw.get("samples") or []
        queries = [
            {"query_id": it.get("query_id", f"q-{i:03d}"), "query": it["query"],
             "entity": it.get("entity"), "category": it.get("category", "unknown")}
            for i, it in enumerate(items, start=1) if it.get("query")
        ]
        if not queries:
            raise SystemExit(f"--queries 文件 {args.queries} 中没有有效查询")
    print(f"[info] queries={len(queries)}  top_k={args.limit}  n_chunks={args.n_chunks}")
    return asyncio.run(run(
        args.pg_url, sqlite_path, project_id,
        args.n_chunks, args.limit, queries, args.out,
    ))


if __name__ == "__main__":
    sys.exit(main())
