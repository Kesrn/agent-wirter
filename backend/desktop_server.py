"""Desktop-packaged backend entrypoint.

This entrypoint is used by Electron. It configures a local SQLite database and
user-data upload directory before importing the FastAPI app.

桌面端不要求用户安装 PostgreSQL/Docker，所以这里在导入 main.app 之前先覆盖
DATABASE_URL、UPLOAD_DIR、JWT_SECRET 等环境变量。导入顺序很关键：settings.py
在 import 时就会读取环境变量，必须先 configure_desktop_environment。

除正常启动外还支持 ``--verify-fts5`` 自检：对打包产物（PyInstaller 捆绑运行时）
实际执行 SQLite FTS5 建表/中文插入/MATCH/bm25 断言（等价于
backend/scripts/verify_fts5.py 的验收逻辑），自检后直接退出、不启动服务。
Windows/macOS CI 均在 ``npm run backend:build`` 之后直接运行打包出的
ai-creative-backend --verify-fts5 完成验收。
"""

from __future__ import annotations

import argparse
import os
import platform
import secrets
import sqlite3
import sys
from pathlib import Path


APP_NAME = "AI Creative Platform"


def _default_data_dir() -> Path:
    """返回各平台推荐的用户数据目录。

    macOS: ~/Library/Application Support/...
    Windows: %APPDATA%/...
    Linux: $XDG_DATA_HOME 或 ~/.local/share/...
    """
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        return Path(os.getenv("APPDATA", home / "AppData" / "Roaming")) / APP_NAME
    return Path(os.getenv("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME


def configure_desktop_environment() -> Path:
    """配置桌面端运行环境，并返回数据目录。

    这里创建 SQLite 数据库路径、上传目录和持久化 jwt_secret 文件。
    jwt_secret 写到用户数据目录（权限 0600），可以保证应用重启后 token/API Key 解密
    仍然有效；同时放开首次登录建号（ALLOW_FIRST_USER_BOOTSTRAP），否则单用户本地
    场景第一次启动没有任何账号可登录。
    """
    data_dir = Path(os.getenv("AI_CREATIVE_DESKTOP_DATA_DIR", _default_data_dir()))
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = data_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # 桌面端的数据必须与打包应用的数据目录绑定。不能使用 setdefault：
    # 从终端、IDE 或 Web 开发环境继承的 DATABASE_URL 可能指向 PostgreSQL，
    # 会让桌面端看起来像是“项目/大纲丢失”，还可能因迁移版本不同导致接口 500。
    # 需要连接外部数据库时应使用 main.py，而不是桌面端入口。
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{data_dir / 'ai_creative.sqlite3'}"
    os.environ["UPLOAD_DIR"] = str(uploads_dir)
    os.environ["DEBUG"] = "false"
    secret_path = data_dir / "jwt_secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        # 密钥文件只允许当前用户读写：数据目录在多用户机器上是共享可遍历的。
        os.chmod(secret_path, 0o600)
    else:
        # 老版本写出的密钥文件默认是 0644，这里纠正一次，不改内容。
        try:
            if secret_path.stat().st_mode & 0o077:
                os.chmod(secret_path, 0o600)
        except OSError:
            pass
    # 同样固定使用随桌面数据目录持久化的密钥，保证重启后本地登录态有效。
    os.environ["JWT_SECRET"] = secret_path.read_text(encoding="utf-8").strip()
    # 桌面端是单用户本地使用，第一次启动必须先能建出账号，否则用户连界面都进不去。
    # 仅桌面入口放开该开关，不影响服务端部署（默认仍是 development 的保守值由 APP_ENV 决定）。
    os.environ.setdefault("ALLOW_FIRST_USER_BOOTSTRAP", "true")
    # 检索索引 embedding 配置（真实 embedding 端点）：数据目录下存在
    # embedding.env 时逐行注入环境变量（EMBEDDING_PROVIDER / EMBEDDING_API_KEY /
    # EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIMENSION / RETRIEVAL_MODE）。
    # 文件不存在时保持默认（openai provider 无 key 自动降级本地伪语义向量，不崩溃）。
    # setdefault：不覆盖终端/CI 显式注入的同名变量，仅补桌面端持久化配置。
    embedding_env = data_dir / "embedding.env"
    if embedding_env.exists():
        for raw_line in embedding_env.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            env_key, _, env_value = line.partition("=")
            os.environ.setdefault(env_key.strip(), env_value.strip())
    # Mock provider 已被禁用，用户需要在前端配置真实的 LLM provider
    # os.environ.setdefault("LLM_PROVIDER", "mock")
    # os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
    os.environ.setdefault("CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173,null")
    return data_dir


def _verify_fts5() -> int:
    """打包后 SQLite FTS5 能力自检（与 backend/scripts/verify_fts5.py 断言等价）。

    检索索引依赖 SQLite FTS5 虚拟表（``CREATE VIRTUAL TABLE ... USING fts5``）。
    FTS5 是 SQLite **编译期特性**（ENABLE_FTS5），开发环境通过不代表打包后的
    运行时（PyInstaller 捆绑的 Python/sqlite3 扩展）一定启用，因此必须对实际
    打包产物自检：《RAG增强评估_小说检索方法论引入.md》§6 要求打包后的后端进程内
    **实际执行**建表 + 插入中文 + 检索，成功才通过验收。

    这里内联与 verify_fts5.py 等价的全套断言，而不 import 该脚本：PyInstaller
    onefile 产物不会把 backend/scripts/ 下的脚本收集进捆绑运行时（除非在
    build-backend.mjs 显式 --hidden-import/--add-data，而本仓库不动构建脚本），
    内联保证打包产物内可独立执行。脚本本身保留，供开发环境直接运行。

    退出码（与 verify_fts5.py 一致）：
        0 = 全部通过（FTS5 可用，建表/插入/中文检索/bm25 排序正常）
        1 = 当前运行时 SQLite 未启用 FTS5（打包验收失败）
        2 = FTS5 已启用但实际建表/检索/排序断言失败
    """
    # 与 backend/services/retrieval_indexer.py 的 FTS5_CREATE_SQL 同构：
    # 列布局、UNINDEXED 标记与检索列（content/search_text）保持一致。
    fts5_create_sql = """
CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_chunks_fts USING fts5(
    chunk_id UNINDEXED, project_id UNINDEXED, source_kind UNINDEXED,
    source_id UNINDEXED, chapter_no UNINDEXED, title, content, search_text
)
"""
    # 中文测试数据：content 为原文，search_text 为空格分词后的检索文本。
    # 各文档 token 长度刻意不同（8/8/5），用于验证 bm25 长度归一化排序。
    test_docs: list[tuple[str, str, str]] = [
        # (chunk_id, content, search_text)
        ("doc-0001", "张小侯在宿舍走廊释放风轨击退了敌人", "张小侯 宿舍 走廊 释放 风轨 击退 敌人"),
        ("doc-0002", "莫凡在觉醒仪式上觉醒了火系与雷系", "莫凡 觉醒 仪式 觉醒 火系 雷系"),
        ("doc-0003", "张小侯的风系初阶技能是风轨", "张小侯 风系 初阶 技能 风轨"),
    ]

    pass_mark = "\u2714"
    fail_mark = "\u2716"
    steps: list[tuple[bool, str]] = []

    def ok(msg: str) -> None:
        steps.append((True, msg))

    def fail(msg: str) -> None:
        steps.append((False, msg))

    def print_steps() -> None:
        for is_ok, msg in steps:
            print(f"  [{pass_mark if is_ok else fail_mark}] {msg}")

    print("FTS5 打包自检（ai-creative-backend --verify-fts5，标准库 sqlite3）")
    print(f"  SQLite 版本: {sqlite3.sqlite_version}")
    print(f"  sqlite3 模块: {sqlite3.__file__}")

    # 1) PRAGMA compile_options 是否含 ENABLE_FTS5（编译期特性，运行时不可改）
    probe = sqlite3.connect(":memory:")
    try:
        options = [r[0] for r in probe.execute("PRAGMA compile_options")]
    finally:
        probe.close()
    if "ENABLE_FTS5" in options:
        ok("PRAGMA compile_options 含 ENABLE_FTS5")
    else:
        fts5_like = [o for o in options if "FTS5" in o]
        detail = ", ".join(fts5_like) if fts5_like else "无任何 FTS5 相关选项"
        fail(f"PRAGMA compile_options 未含 ENABLE_FTS5（现有相关选项: {detail}）")

    if not steps[-1][0]:
        print_steps()
        print(
            "\n[FAIL] 当前运行时 SQLite 未启用 FTS5（ENABLE_FTS5 编译选项缺失）。\n"
            "      打包产物将无法创建 retrieval_chunks_fts 虚拟表，检索索引不可用。\n"
            "      请在构建环境中更换启用了 FTS5 的 SQLite（或重新编译 Python/sqlite3），"
            "打包验收不通过。",
            file=sys.stderr,
        )
        return 1

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        # 2) 建表（与应用 DDL 同构）
        try:
            conn.execute(fts5_create_sql)
            ok("CREATE VIRTUAL TABLE ... USING fts5(...) 建表成功（与应用 DDL 同构）")
        except sqlite3.OperationalError as e:
            fail(f"CREATE VIRTUAL TABLE ... USING fts5 失败: {e}")

        # 3) 插入中文内容
        if steps[-1][0]:
            try:
                for cid, content, search_text in test_docs:
                    conn.execute(
                        "INSERT INTO retrieval_chunks_fts "
                        "(chunk_id, project_id, source_kind, source_id, chapter_no, title, content, search_text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (cid, "probe-project", "source", "probe-source", 19, "验收样例", content, search_text),
                    )
                ok(f"插入 {len(test_docs)} 条中文内容成功")
            except sqlite3.OperationalError as e:
                fail(f"FTS5 插入中文内容失败: {e}")

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
                fail(f"MATCH 查询 {query!r} 执行失败: {e}")
                return
            got = [r["chunk_id"] for r in rows]
            hits = [c for c in expect_chunks if c in got]
            if hits:
                ok(f"MATCH {query!r} 命中 {hits}（共 {len(rows)} 条，顺序={got}）")
            else:
                fail(f"MATCH {query!r} 未命中期望 chunk {expect_chunks}（实际={got}）")

        # 4) 中文分词检索：search_text 中分词后的单/双词检索
        assert_match("风轨", ["doc-0001", "doc-0003"])
        assert_match("火系", ["doc-0002"])
        assert_match("张小侯", ["doc-0001", "doc-0003"])

        # 5) 原始中文整串（unicode61 把连续 CJK 视为一个 token，用短语查询验证可达）
        assert_match('"张小侯在宿舍走廊释放风轨击退了敌人"', ["doc-0001"])

        # 6) 无命中查询应返回空（防过度召回）
        try:
            rows = fetch_matches("查无此词")
            if rows:
                fail(f"MATCH '查无此词' 不应有结果，实际返回 {len(rows)} 条: {[r['chunk_id'] for r in rows]}")
            else:
                ok("MATCH '查无此词' 返回 0 条（无过度召回）")
        except sqlite3.OperationalError as e:
            fail(f"MATCH '查无此词' 执行失败: {e}")

        # 7) bm25 排序：同 tf 时文档越短相关度越高（BM25 长度归一化）
        #    MATCH '张小侯' 命中 doc-0001（8 token）与 doc-0003（5 token），
        #    两者 tf 相同 → doc-0003 应排在前（bm25 值更小）。
        try:
            rows = fetch_matches("张小侯")
            got = [r["chunk_id"] for r in rows]
            if set(got[:2]) != {"doc-0001", "doc-0003"}:
                fail(f"MATCH '张小侯' 前两条应为 doc-0001/doc-0003，实际={got}")
            elif got and got[0] != "doc-0003":
                fail(f"bm25 长度归一化断言失败: 较短文档 doc-0003 应排首，实际={got}")
            else:
                ok(f"bm25 排序正常: 较短文档 doc-0003 排在 doc-0001 前（顺序={got}）")
        except sqlite3.OperationalError as e:
            fail(f"bm25 排序查询执行失败: {e}")

        # 8) AND 语义（空格 = 隐含 AND）：'张小侯 风轨' 只应命中同时含两词的
        #    doc-0001/doc-0003，不含 doc-0002（莫凡）。
        try:
            rows = fetch_matches("张小侯 风轨")
            got = [r["chunk_id"] for r in rows]
            if set(got) != {"doc-0001", "doc-0003"}:
                fail(f"AND 语义断言失败: MATCH '张小侯 风轨' 应仅命中 doc-0001/doc-0003，实际={got}")
            else:
                ok(f"MATCH '张小侯 风轨'（隐含 AND）仅命中双词文档（顺序={got}）")
        except sqlite3.OperationalError as e:
            fail(f"AND 语义查询执行失败: {e}")
    finally:
        conn.close()

    print_steps()

    failed = [m for is_ok, m in steps if not is_ok]
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


def main() -> None:
    """Electron 启动的后端进程入口（支持 --verify-fts5 打包自检）。"""
    parser = argparse.ArgumentParser(
        prog="ai-creative-backend",
        description="AI Creative Platform 桌面端后端进程（Electron 启动入口）。",
    )
    parser.add_argument(
        "--verify-fts5",
        action="store_true",
        help="打包后 SQLite FTS5 能力自检：实际执行建表/中文插入/MATCH/bm25 断言后直接退出（不启动服务）。"
        "退出码 0 = 通过，1 = 运行时未启用 FTS5，2 = 断言失败。",
    )
    args = parser.parse_args()

    if args.verify_fts5:
        # 自检不写用户数据目录、不导入 FastAPI app、不启动服务，自检即退出。
        sys.exit(_verify_fts5())

    configure_desktop_environment()

    import uvicorn
    from main import app
    from config.settings import settings

    host = os.getenv("AI_CREATIVE_BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("AI_CREATIVE_BACKEND_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level=settings.LOG_LEVEL.lower(), access_log=False)


if __name__ == "__main__":
    main()
