#!/usr/bin/env python3
"""PostgreSQL 双后端分支检索验证脚本（阶段一收尾）。

流程
----
1. 连 PG（--pg-url，默认 postgres:postgres@localhost:5432/ai_creative）；连接失败
   → 退出码 2（可跳过，不阻塞离线环境）。
2. 幂等建表/建索引（复用 setup_pg.setup_pg_tables / setup_indexes，安全）。
3. 前置数据安全检查：报告 PG 检索四表现有数据量（不动任何数据）。
4. 用独立 project_id（随机 UUID——PG 侧 retrieval_chunks.project_id 是 UUID
   类型，非 UUID 字符串无法写入）：
   - 种子项目词典（character_profile / ability_profile /
     character_alias_clusters / world_entries，仅该项目下写入，与 SQLite
     单测同款数据）；
   - 双写测试块（retrieval_chunks + retrieval_chunks_fts，中文
     莫凡/雷系/玫炎 等，chunk_id 用合法 UUID）；
   - 调用 LexicalRetriever.search（PG AsyncSession）验证：词法命中/排序/
     snippet、无结果返回 []、实体优先通道（别名 小莫 → 莫凡）、
     source_kinds 过滤、chapter_num 过滤。
5. finally 清理：删除本次全部测试 project_id 的行（检索四表 + 4 张词典
   业务表，按精确 project_id 匹配，绝不触碰其他项目），再断言无残留，
   不影响 ai_creative 库既有数据。

退出码：0=全部通过，1=存在失败，2=PG 不可达（跳过）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative"

# 检索四表 + verify 会写入的 4 张词典业务表（全部按 project_id 精确隔离）
_RETRIEVAL_TABLES = (
    "retrieval_chunks_fts",
    "retrieval_chunks",
    "retrieval_index_source_state",
    "retrieval_index_generation",
)
_VOCAB_TABLES = (
    "character_alias_clusters",
    "ability_profile",
    "character_profile",
    "world_entries",
)
_ALL_TABLES = _RETRIEVAL_TABLES + _VOCAB_TABLES


# ── 结果收集 ─────────────────────────────────────────────

class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks: int = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.checks += 1
        print(f"  [PASS] {name}{(' — ' + detail) if detail else ''}")

    def fail(self, name: str, detail: str) -> None:
        self.checks += 1
        self.failures.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} — {detail}")

    def check(self, name: str, cond: bool, detail: str) -> None:
        (self.ok if cond else self.fail)(name, detail)

    def done(self) -> int:
        print(f"\n验证结果: {self.checks - len(self.failures)}/{self.checks} 通过")
        if self.failures:
            print("失败项:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        return 0


# ── 测试数据（与 SQLite 单测同款，PG 适配） ──────────────

async def _seed_vocab(db: AsyncSession, pid: str) -> None:
    """种子项目词典（来源与生产一致，仅该项目）。"""
    from models.structured_knowledge import (
        AbilityProfile,
        CharacterAliasCluster,
        CharacterProfile,
    )
    from models.world_entry import WorldEntry

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
    """构建项目分词器（等价检索侧首次调用）。"""
    from services.retrieval_tokenizer import (
        build_project_vocabulary,
        get_project_tokenizer,
    )

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
    """双写一条测试块（retrieval_chunks + retrieval_chunks_fts 同一事务）。"""
    await db.execute(
        text(
            "INSERT INTO retrieval_chunks (id, project_id, source_kind, source_id, "
            "source_version_id, index_generation_id, chunk_index, content, search_text, "
            "content_hash, metadata) VALUES (:id, :pid, :sk, :sid, :svid, :gid, 0, "
            ":content, :st, :h, NULL)"
        ),
        {
            "id": uuid.UUID(chunk_id), "pid": pid, "sk": source_kind, "sid": source_id,
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


# ── 清理（测试隔离：按精确 project_id） ──────────────────

async def _cleanup_test_data(db: AsyncSession, pids: list[str]) -> None:
    """删除测试 project 的全部行（检索四表 + 4 张词典业务表）。"""
    if not pids:
        return
    # 若上一个用例异常导致事务中止，先回滚再清理
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass
    ph = ", ".join(f":p{i}" for i in range(len(pids)))
    params = {f"p{i}": str(p) for i, p in enumerate(pids)}
    for table in _ALL_TABLES:
        await db.execute(text(f"DELETE FROM {table} WHERE project_id IN ({ph})"), params)
    await db.commit()


async def _count_for_pids(db: AsyncSession, pids: list[str]) -> dict[str, int]:
    """统计测试 project 在全部相关表的残留行数（0 表示已清理干净）。"""
    counts: dict[str, int] = {}
    if not pids:
        return {t: 0 for t in _ALL_TABLES}
    ph = ", ".join(f":p{i}" for i in range(len(pids)))
    params = {f"p{i}": str(p) for i, p in enumerate(pids)}
    for table in _ALL_TABLES:
        n = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE project_id IN ({ph})"), params
            )
        ).scalar() or 0
        counts[table] = n
    return counts


async def _heal_stale_test_data(db: AsyncSession) -> list[str]:
    """自愈：清理上次中断遗留的测试数据（以 retrieval_chunks.content_hash='test-hash'
    为测试行签名，推导出测试 project 后删除全部相关表行）。返回清理的 project 列表。"""
    try:
        pids = (
            await db.execute(
                text("SELECT DISTINCT project_id FROM retrieval_chunks "
                     "WHERE content_hash = 'test-hash'")
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — 表可能不存在
        return []
    pids = [str(p) for p in pids]
    if pids:
        await _cleanup_test_data(db, pids)
        print(f"  [warn] 已清理上次中断遗留的测试数据: {len(pids)} 个 project")
    return pids


# ── 各测试用例（与 SQLite 单测同款断言） ─────────────────

async def test_lexical_search_finds_relevant_chunk(
    db: AsyncSession, checker: Checker, track: list[str],
) -> str:
    """中文查询分词后 PG tsvector 命中：含 莫凡/雷系 的块排最前，rank 从 1 开始。"""
    from services.lexical_retriever import LexicalRetriever

    pid = str(uuid.uuid4())
    track.append(pid)  # 先登记再写数据：中途异常也能被清理
    await _seed_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    c_a, c_b = str(uuid.uuid4()), str(uuid.uuid4())
    await _insert_test_chunk(
        db, pid, chunk_id=c_a, source_id="src-a",
        content="莫凡觉醒了雷系。他是主角。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。他是主角。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id=c_b, source_id="src-b",
        content="张小侯使用了风之力。",
        search_text=_search_text(tok, "张小侯使用了风之力。"),
    )

    hits = await LexicalRetriever().search(db, pid, "莫凡的雷系是什么？", limit=5)
    checker.check("词法命中非空", bool(hits), f"hits={len(hits)}")
    if hits:
        checker.check("首位 chunk 是含莫凡+雷系的块", hits[0].chunk_id == c_a,
                      f"got={hits[0].chunk_id}")
        checker.check("rank 从 1 开始", hits[0].rank == 1, f"rank={hits[0].rank}")
        checker.check("source_id 正确", hits[0].source_id == "src-a",
                      f"got={hits[0].source_id}")
        checker.check("source_kind 正确", hits[0].source_kind == "source_chunk",
                      f"got={hits[0].source_kind}")
        checker.check("project_id 正确", hits[0].project_id == pid, f"got={hits[0].project_id}")
        checker.check("score > 0（ts_rank 越大越好）", hits[0].score > 0,
                      f"score={hits[0].score}")
        checker.check("snippet 含 雷系", "雷系" in hits[0].snippet,
                      f"snippet={hits[0].snippet[:40]!r}")
        checker.check("content 原文正确", hits[0].content == "莫凡觉醒了雷系。他是主角。",
                      f"got={hits[0].content!r}")
        checker.check("c-b 不被召回（不含任何 query token）",
                      all(h.chunk_id != c_b for h in hits),
                      f"hits={[h.chunk_id for h in hits]}")
    return pid


async def test_search_returns_empty_on_no_match(
    db: AsyncSession, checker: Checker, track: list[str],
) -> str:
    from services.lexical_retriever import LexicalRetriever

    pid = str(uuid.uuid4())
    track.append(pid)
    await _seed_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    await _insert_test_chunk(
        db, pid, chunk_id=str(uuid.uuid4()), source_id="src-a",
        content="莫凡觉醒了雷系。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。"),
    )
    retriever = LexicalRetriever()
    r1 = await retriever.search(db, pid, "量子猫薄荷")
    r2 = await retriever.search(db, pid, "")
    r3 = await retriever.search(db, pid, "莫凡", limit=0)
    checker.check("无命中查询返回 []", r1 == [], f"got={r1}")
    checker.check("空查询返回 []", r2 == [], f"got={r2}")
    checker.check("limit=0 返回 []", r3 == [], f"got={r3}")
    return pid


async def test_entity_first_ordering(
    db: AsyncSession, checker: Checker, track: list[str],
) -> str:
    """别名 小莫 归一为 莫凡：含莫凡的块加权排最前（PG 实体通道 = to_tsquery）。"""
    from services.lexical_retriever import LexicalRetriever

    pid = str(uuid.uuid4())
    track.append(pid)
    await _seed_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    c_e1, c_e2 = str(uuid.uuid4()), str(uuid.uuid4())
    await _insert_test_chunk(
        db, pid, chunk_id=c_e1, source_id="src-e",
        content="莫凡在魔法高中参加觉醒仪式。",
        search_text=_search_text(tok, "莫凡在魔法高中参加觉醒仪式。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id=c_e2, source_id="src-e",
        content="雷系魔法威力强大。",
        search_text=_search_text(tok, "雷系魔法威力强大。"),
    )
    hits = await LexicalRetriever().search(db, pid, "小莫的雷系", limit=5)
    checker.check("实体命中（莫凡）排在词法命中（雷系）之前",
                  [h.chunk_id for h in hits] == [c_e1, c_e2],
                  f"got={[h.chunk_id for h in hits]}")
    if len(hits) >= 2:
        checker.check("实体块 rank=1 且分数更高",
                      hits[0].rank == 1 and hits[0].score > hits[1].score,
                      f"scores={[h.score for h in hits]}")
    return pid


async def test_source_kinds_filter(
    db: AsyncSession, checker: Checker, track: list[str],
) -> str:
    from services.lexical_retriever import LexicalRetriever

    pid = str(uuid.uuid4())
    track.append(pid)
    await _seed_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    content = "莫凡觉醒了雷系。"
    st = _search_text(tok, content)
    c_f1, c_f2 = str(uuid.uuid4()), str(uuid.uuid4())
    await _insert_test_chunk(
        db, pid, chunk_id=c_f1, source_kind="chapter_version", source_id="src-f",
        content=content, search_text=st,
    )
    await _insert_test_chunk(
        db, pid, chunk_id=c_f2, source_kind="source_chunk", source_id="src-f",
        content=content, search_text=st,
    )
    retriever = LexicalRetriever()
    only_chapter = await retriever.search(db, pid, "莫凡", source_kinds=["chapter_version"])
    checker.check("source_kinds 过滤只回 chapter_version",
                  [h.chunk_id for h in only_chapter] == [c_f1],
                  f"got={[h.chunk_id for h in only_chapter]}")
    both = await retriever.search(db, pid, "莫凡",
                                  source_kinds=["chapter_version", "source_chunk"])
    checker.check("多 source_kinds 全部返回",
                  {h.chunk_id for h in both} == {c_f1, c_f2},
                  f"got={[h.chunk_id for h in both]}")
    return pid


async def test_chapter_num_filter(
    db: AsyncSession, checker: Checker, track: list[str],
) -> str:
    from services.lexical_retriever import LexicalRetriever

    pid = str(uuid.uuid4())
    track.append(pid)
    await _seed_vocab(db, pid)
    tok = await _build_vocab(db, pid)
    c_g2, c_g3 = str(uuid.uuid4()), str(uuid.uuid4())
    await _insert_test_chunk(
        db, pid, chunk_id=c_g2, source_kind="chapter_version", source_id="src-g",
        chapter_no=2, title="第二章", content="莫凡觉醒了雷系。",
        search_text=_search_text(tok, "莫凡觉醒了雷系。"),
    )
    await _insert_test_chunk(
        db, pid, chunk_id=c_g3, source_kind="chapter_version", source_id="src-g",
        chapter_no=3, title="第三章", content="莫凡觉醒了火系。",
        search_text=_search_text(tok, "莫凡觉醒了火系。"),
    )
    retriever = LexicalRetriever()
    hits = await retriever.search(db, pid, "莫凡", chapter_num=3)
    checker.check("chapter_num=3 只回第3章块",
                  [h.chunk_id for h in hits] == [c_g3],
                  f"got={[h.chunk_id for h in hits]}")
    checker.check("chapter_no 字段透出",
                  bool(hits) and hits[0].chapter_no == 3,
                  f"chapter_no={hits[0].chapter_no if hits else None}")
    no_hit = await retriever.search(db, pid, "莫凡", chapter_num=99)
    checker.check("chapter_num=99 返回 []", no_hit == [], f"got={no_hit}")
    all_hits = await retriever.search(db, pid, "莫凡")
    checker.check("不限章节两条都出", {h.chunk_id for h in all_hits} == {c_g2, c_g3},
                  f"got={[h.chunk_id for h in all_hits]}")
    return pid


# ── 主流程 ───────────────────────────────────────────────

async def run(pg_url: str) -> int:
    from services.lexical_retriever import LexicalRetriever  # noqa: F401（确保模块可导入）
    from services.retrieval_tokenizer import clear_tokenizer_cache
    from retrieval_eval import setup_pg

    print(f"[verify] 目标数据库: {pg_url}")
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        # 连接探测（失败 → 退出码 2，可跳过）
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] PostgreSQL 不可达（{exc.__class__.__name__}: {exc}），跳过验证")
            return 2

        # 幂等建表/建索引（安全，不触碰既有业务数据）
        await setup_pg.setup_pg_tables(engine)
        await setup_pg.setup_indexes(engine, with_trgm=True)

        async with factory() as db:
            # 前置数据安全检查：报告现有数据量（不动任何数据）
            print("[verify] 前置数据安全检查:")
            for table in setup_pg._RETRIEVAL_TABLES:
                n = (await db.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
                print(f"  [pg] {table}: {n} 行")
            # 自愈：清理上次中断可能遗留的测试数据（不影响业务数据）
            await _heal_stale_test_data(db)

            checker = Checker()
            created_pids: list[str] = []
            try:
                print("\n[verify] 用例 1/5: 词法命中 + 排序 + snippet")
                await test_lexical_search_finds_relevant_chunk(db, checker, created_pids)
                print("\n[verify] 用例 2/5: 无结果返回 []")
                await test_search_returns_empty_on_no_match(db, checker, created_pids)
                print("\n[verify] 用例 3/5: 实体优先通道（别名 小莫 → 莫凡）")
                await test_entity_first_ordering(db, checker, created_pids)
                print("\n[verify] 用例 4/5: source_kinds 过滤")
                await test_source_kinds_filter(db, checker, created_pids)
                print("\n[verify] 用例 5/5: chapter_num 过滤")
                await test_chapter_num_filter(db, checker, created_pids)
            finally:
                # 测试隔离：清理本次全部测试 project 的数据（无论成败）
                clear_tokenizer_cache()
                await _cleanup_test_data(db, created_pids)

            # 终检：本次测试 project 无任何残留
            print("\n[verify] 清理终检（测试 project 残留）:")
            residue = await _count_for_pids(db, created_pids)
            dirty = {t: n for t, n in residue.items() if n}
            checker.check("测试数据已全部清理，无残留", not dirty,
                          f"residue={dirty or '无'}")
            return checker.done()
    finally:
        await engine.dispose()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="PostgreSQL 双后端分支检索验证（独立 project_id + 跑完清理）"
    )
    parser.add_argument("--pg-url", default=DEFAULT_PG_URL,
                        help=f"PostgreSQL 连接串（默认 {DEFAULT_PG_URL}）")
    args = parser.parse_args()
    return asyncio.run(run(args.pg_url))


if __name__ == "__main__":
    sys.exit(main())
