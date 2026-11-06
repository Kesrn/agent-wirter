#!/usr/bin/env python3
"""打包后 SQLite FTS5 能力自动化验收脚本（零第三方依赖，仅标准库 sqlite3 / sys）。

背景
----
检索索引依赖 SQLite FTS5 虚拟表（``CREATE VIRTUAL TABLE ... USING fts5``）。
开发环境已验证 ENABLE_FTS5=True，但打包后的运行时（PyInstaller 捆绑的 Python
解释器与 sqlite3 扩展）是否启用 FTS5 必须在自动化构建中验收，不能只在开发机验证。
《RAG增强评估_小说检索方法论引入.md》§6 要求：打包后的后端进程内**实际执行**
``CREATE VIRTUAL TABLE IF NOT EXISTS ... USING fts5(...)``，插入并检索一条中文
文本，成功才通过验收。

本脚本即该验收的具体实现：
  1. 输出 SQLite 版本（sqlite3.sqlite_version）与编译选项
     （PRAGMA compile_options 是否含 ENABLE_FTS5）；
  2. 在内存库中实际执行与 backend/services/retrieval_indexer.py 同构的
     ``CREATE VIRTUAL TABLE ... USING fts5(...)``（含 UNINDEXED 列与 search_text）；
  3. 插入中文内容（content 原始文本 + 空格分词 search_text，模拟应用侧
     "jieba 预分词 → FTS5 unicode61 分词"的写入契约），执行 MATCH 检索、
     bm25 排序，断言命中与排序；
  4. 任一环节失败：stderr 输出明确错误并退出非 0
     （1 = FTS5 编译能力缺失，2 = 建表/检索/排序断言失败）。

用法
----
    python scripts/verify_fts5.py        # 退出码 0 = 通过

打包后（CI 推荐做法）：不单独打自检 exe，而是直接对 `npm run backend:build`
产出的实际后端产物运行 `--verify-fts5`——`backend/desktop_server.py` 的
`_verify_fts5()` 内联了与本脚本等价的全套断言（同一 DDL 同构建表、同一中文测试
数据、同一 MATCH/bm25/AND/无过度召回断言，退出码语义一致）。Windows 与 macOS
工作流的 `Smoke-test packaged SQLite FTS5 support` 步骤均为：
`frontend/backend-dist/ai-creative-backend[.exe] --verify-fts5`。本脚本保留，
供开发环境直接运行同一套验收（`cd backend && venv/bin/python scripts/verify_fts5.py`）。

退出码：
    0 = 全部通过（FTS5 可用，建表/插入/中文检索/bm25 排序正常）
    1 = 当前运行时 SQLite 未启用 FTS5（打包验收失败）
    2 = FTS5 已启用但实际建表/检索/排序断言失败
"""

from __future__ import annotations

import sqlite3
import sys

# 与 backend/services/retrieval_indexer.py 的 FTS5_CREATE_SQL 同构：
# 列布局、UNINDEXED 标记与检索列（content/search_text）保持一致，
# 保证"打包后进程内实测"覆盖的是应用真实使用的 DDL 形状。
FTS5_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_chunks_fts USING fts5(
    chunk_id UNINDEXED, project_id UNINDEXED, source_kind UNINDEXED,
    source_id UNINDEXED, chapter_no UNINDEXED, title, content, search_text
)
"""

# 中文测试数据：content 为原文，search_text 为空格分词后的检索文本
# （应用侧由项目分词器产出，这里手写等价结果，模拟同一写入契约）。
# 各文档 token 长度刻意不同（8/8/5），用于验证 bm25 长度归一化排序。
TEST_DOCS: list[tuple[str, str, str]] = [
    # (chunk_id, content, search_text)
    ("doc-0001", "张小侯在宿舍走廊释放风轨击退了敌人", "张小侯 宿舍 走廊 释放 风轨 击退 敌人"),
    ("doc-0002", "莫凡在觉醒仪式上觉醒了火系与雷系", "莫凡 觉醒 仪式 觉醒 火系 雷系"),
    ("doc-0003", "张小侯的风系初阶技能是风轨", "张小侯 风系 初阶 技能 风轨"),
]

PASS = "\u2714"
FAIL = "\u2716"


class CheckResult:
    """收集各检查步骤的结果：steps 为 (ok, message) 列表。"""

    def __init__(self) -> None:
        self.steps: list[tuple[bool, str]] = []

    def ok(self, msg: str) -> None:
        self.steps.append((True, msg))

    def fail(self, msg: str) -> None:
        self.steps.append((False, msg))


def print_result(result: CheckResult) -> None:
    for ok, msg in result.steps:
        print(f"  [{PASS if ok else FAIL}] {msg}")


def check_compile_option(conn: sqlite3.Connection) -> tuple[bool, str]:
    """PRAGMA compile_options 是否含 ENABLE_FTS5（FTS5 是编译期特性，运行时不可改）。"""
    options = [r[0] for r in conn.execute("PRAGMA compile_options")]
    if "ENABLE_FTS5" in options:
        return True, "PRAGMA compile_options 含 ENABLE_FTS5"
    fts5_like = [o for o in options if "FTS5" in o]
    detail = ", ".join(fts5_like) if fts5_like else "无任何 FTS5 相关选项"
    return False, f"PRAGMA compile_options 未含 ENABLE_FTS5（现有相关选项: {detail}）"


def run_fts5_probe(conn: sqlite3.Connection, result: CheckResult) -> None:
    """实际执行建表 / 插入 / MATCH 检索 / bm25 排序，任一断言失败记入 result。"""
    # 1) 建表（与应用 DDL 同构）
    try:
        conn.execute(FTS5_CREATE_SQL)
        result.ok("CREATE VIRTUAL TABLE ... USING fts5(...) 建表成功（与应用 DDL 同构）")
    except sqlite3.OperationalError as e:
        result.fail(f"CREATE VIRTUAL TABLE ... USING fts5 失败: {e}")
        return

    # 2) 插入中文内容
    try:
        for cid, content, search_text in TEST_DOCS:
            conn.execute(
                "INSERT INTO retrieval_chunks_fts "
                "(chunk_id, project_id, source_kind, source_id, chapter_no, title, content, search_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, "probe-project", "source", "probe-source", 19, "验收样例", content, search_text),
            )
        result.ok(f"插入 {len(TEST_DOCS)} 条中文内容成功")
    except sqlite3.OperationalError as e:
        result.fail(f"FTS5 插入中文内容失败: {e}")
        return

    def fetch_matches(query: str) -> list[sqlite3.Row]:
        # FTS5 隐藏列 rank 即 bm25 分数（越小越相关），与应用侧 ORDER BY rank 一致
        return conn.execute(
            "SELECT chunk_id, content, rank FROM retrieval_chunks_fts "
            "WHERE retrieval_chunks_fts MATCH ? ORDER BY rank LIMIT 10",
            (query,),
        ).fetchall()

    def assert_match(query: str, expect_chunks: list[str]) -> None:
        try:
            rows = fetch_matches(query)
        except sqlite3.OperationalError as e:
            result.fail(f"MATCH 查询 {query!r} 执行失败: {e}")
            return
        got = [r["chunk_id"] for r in rows]
        hits = [c for c in expect_chunks if c in got]
        if hits:
            result.ok(f"MATCH {query!r} 命中 {hits}（共 {len(rows)} 条，顺序={got}）")
        else:
            result.fail(f"MATCH {query!r} 未命中期望 chunk {expect_chunks}（实际={got}）")

    # 3) 中文分词检索：search_text 中分词后的单/双词检索
    assert_match("风轨", ["doc-0001", "doc-0003"])
    assert_match("火系", ["doc-0002"])
    assert_match("张小侯", ["doc-0001", "doc-0003"])

    # 4) 原始中文整串（unicode61 把连续 CJK 视为一个 token，用短语查询验证可达）
    assert_match('"张小侯在宿舍走廊释放风轨击退了敌人"', ["doc-0001"])

    # 5) 无命中查询应返回空（防过度召回）
    try:
        rows = fetch_matches("查无此词")
        if rows:
            result.fail(f"MATCH '查无此词' 不应有结果，实际返回 {len(rows)} 条: {[r['chunk_id'] for r in rows]}")
        else:
            result.ok("MATCH '查无此词' 返回 0 条（无过度召回）")
    except sqlite3.OperationalError as e:
        result.fail(f"MATCH '查无此词' 执行失败: {e}")

    # 6) bm25 排序：同 tf 时文档越短相关度越高（BM25 长度归一化）
    #    MATCH '张小侯' 命中 doc-0001（8 token）与 doc-0003（5 token），
    #    两者 tf 相同 → doc-0003 应排在前（bm25 值更小）。
    try:
        rows = fetch_matches("张小侯")
        got = [r["chunk_id"] for r in rows]
        if set(got[:2]) != {"doc-0001", "doc-0003"}:
            result.fail(f"MATCH '张小侯' 前两条应为 doc-0001/doc-0003，实际={got}")
        elif got and got[0] != "doc-0003":
            result.fail(f"bm25 长度归一化断言失败: 较短文档 doc-0003 应排首，实际={got}")
        else:
            result.ok(f"bm25 排序正常: 较短文档 doc-0003 排在 doc-0001 前（顺序={got}）")
    except sqlite3.OperationalError as e:
        result.fail(f"bm25 排序查询执行失败: {e}")

    # 7) AND 语义（空格 = 隐含 AND）：'张小侯 风轨' 只应命中同时含两词的
    #    doc-0001/doc-0003，不含 doc-0002（莫凡）。
    try:
        rows = fetch_matches("张小侯 风轨")
        got = [r["chunk_id"] for r in rows]
        if set(got) != {"doc-0001", "doc-0003"}:
            result.fail(f"AND 语义断言失败: MATCH '张小侯 风轨' 应仅命中 doc-0001/doc-0003，实际={got}")
        else:
            result.ok(f"MATCH '张小侯 风轨'（隐含 AND）仅命中双词文档（顺序={got}）")
    except sqlite3.OperationalError as e:
        result.fail(f"AND 语义查询执行失败: {e}")


def main() -> int:
    print("FTS5 打包验收（标准库 sqlite3，无第三方依赖）")
    print(f"  SQLite 版本: {sqlite3.sqlite_version}")
    print(f"  sqlite3 模块: {sqlite3.__file__}")

    result = CheckResult()

    # 只读探测编译选项
    probe = sqlite3.connect(":memory:")
    try:
        fts5_enabled, msg = check_compile_option(probe)
        if fts5_enabled:
            result.ok(msg)
        else:
            result.fail(msg)
    finally:
        probe.close()

    if not fts5_enabled:
        print_result(result)
        print(
            "\n[FAIL] 当前运行时 SQLite 未启用 FTS5（ENABLE_FTS5 编译选项缺失）。\n"
            "      打包产物将无法创建 retrieval_chunks_fts 虚拟表，检索索引不可用。\n"
            "      请在构建环境中更换启用了 FTS5 的 SQLite（或重新编译 Python/sqlite3），"
            "打包验收不通过。",
            file=sys.stderr,
        )
        return 1

    # 实际执行建表/插入/检索/排序断言
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        run_fts5_probe(conn, result)
    finally:
        conn.close()

    print_result(result)

    failed = [m for ok, m in result.steps if not ok]
    if failed:
        print(f"\n[FAIL] FTS5 验收未通过，共 {len(failed)} 项失败:", file=sys.stderr)
        for m in failed:
            print(f"  - {m}", file=sys.stderr)
        return 2

    print(
        f"\n[PASS] 打包运行时 SQLite {sqlite3.sqlite_version} 的 FTS5 验收通过："
        "建表/中文插入/MATCH 检索/bm25 排序全部断言成功。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
