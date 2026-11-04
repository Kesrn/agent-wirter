#!/usr/bin/env python3
"""一次性修复 dev 库 world_entries 缺列（备份先行，只补列、不改数据行）。

背景
----
``backend/dev-web.sqlite3`` 的 ``world_entries`` 表缺少 ORM 模型
``models/world_entry.py`` 声明的 ``chapter_sequence_number`` 列。该列由 Alembic
029 迁移引入（正式桌面库与 models 都有），但历史 dev 文件从未跑过迁移；
``shadow_compare.py`` 等评测工具只读打开 dev 库、绕过 ``init_db()``，因此缺列
从未被修复 —— 旧链路 ``ContextLoader`` 在该库上全列查询时内部捕获异常后静默
返回空，导致"旧链路出错"与"旧链路无数据"无法区分。

本脚本的修改（等价于 ``db/session.py`` ``_ensure_incremental_columns`` 中
world_entries 的 ADD COLUMN 逻辑；来源：``backend/db/session.py`` L279-281 与
L286）：

    ALTER TABLE world_entries ADD COLUMN chapter_sequence_number INTEGER
    CREATE INDEX IF NOT EXISTS ix_world_entries_chapter_sequence_number
        ON world_entries (chapter_sequence_number)

为何不直接 import 并调用 ``_ensure_incremental_columns``（session.py L69 起）：
该函数是桌面端启动时的全量兼容迁移，除了 ADD COLUMN 之外还会：
1. 对 llm_configs 执行数据回填 UPDATE（name / is_active，L98-127）；
2. 对 generation_records 执行去重 DELETE（L210-234）；
3. 当 llm_configs 存在 UNIQUE(user_id) 索引时（本 dev 库正是如此，见
   ``uq_llm_configs_active_user``）会 DROP TABLE 并重建 llm_configs（L141-167）。
任务约束是"只补列，不改数据"，因此这里复制其安全 ADD COLUMN 的最小等价逻辑并
注明来源；不执行任何数据修改语句（无 UPDATE / DELETE / DROP）。

流程：只读预检（确认缺列确实存在）→ 备份（cp 等价，源库只读校验通过后才复制）
→ 补列 → 验证（PRAGMA table_info(world_entries) 含 chapter_sequence_number；
world_entries / characters 与 ORM 模型列集合比对无缺列；索引存在）。

用法
----
    cd backend
    ./venv/bin/python retrieval_eval/fix_dev_schema.py
    ./venv/bin/python retrieval_eval/fix_dev_schema.py --db 某路径.sqlite3 --no-backup
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
DEFAULT_DB = BACKEND_DIR / "dev-web.sqlite3"

# 与 db/session.py `_ensure_incremental_columns`（L279-281）一致的 ADD COLUMN 定义
ADD_COLUMN_SQL = "ALTER TABLE world_entries ADD COLUMN chapter_sequence_number INTEGER"
# 与 db/session.py `_ensure_incremental_columns`（L286）一致的索引（幂等）
INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_world_entries_chapter_sequence_number "
    "ON world_entries (chapter_sequence_number)"
)
SOURCE_NOTE = (
    "逻辑来源：backend/db/session.py `_ensure_incremental_columns` L279-286"
    "（仅复制其 ADD COLUMN 部分，原因见文件头 docstring）"
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_world_entries_column(conn: sqlite3.Connection) -> list[str]:
    """补列 + 建索引（幂等）。返回执行记录列表。"""
    applied: list[str] = []
    existing = _columns(conn, "world_entries")
    if "chapter_sequence_number" in existing:
        applied.append("chapter_sequence_number 已存在，跳过 ADD COLUMN（幂等）")
    else:
        conn.execute(ADD_COLUMN_SQL)
        applied.append(ADD_COLUMN_SQL)
    conn.execute(INDEX_SQL)
    applied.append(INDEX_SQL)
    return applied


def _verify(db_path: str) -> dict:
    """只读验证：缺列已补、索引存在、world_entries/characters 与 ORM 模型无缺列。

    缺列比对口径与 shadow_compare.check_old_chain_schema 一致
    （model.__table__.columns - PRAGMA table_info 实际列）。
    """
    import os

    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    sys.path.insert(0, str(BACKEND_DIR))
    from models.character import Character
    from models.world_entry import WorldEntry

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        missing: dict[str, list[str]] = {}
        for table, model in (("world_entries", WorldEntry), ("characters", Character)):
            actual = _columns(conn, table)
            missing[table] = sorted({c.name for c in model.__table__.columns} - actual)
        return {
            "column_ok": "chapter_sequence_number" in _columns(conn, "world_entries"),
            "index_ok": "ix_world_entries_chapter_sequence_number"
                        in {r[1] for r in conn.execute("PRAGMA index_list(world_entries)").fetchall()},
            "missing": missing,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复 dev 库 world_entries 缺 chapter_sequence_number 列"
                    "（备份先行，只补列、不改数据）"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="SQLite 数据库路径（默认 backend/dev-web.sqlite3）")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"数据库文件不存在: {db_path}")

    # 0) 只读预检：确认源库可读、缺列确实存在（备份与修改前不写任何东西）
    try:
        conn_ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            before = _columns(conn_ro, "world_entries")
        finally:
            conn_ro.close()
    except sqlite3.Error as exc:
        raise SystemExit(f"无法只读打开 {db_path}: {exc}")
    already_fixed = "chapter_sequence_number" in before
    print(f"[info] db={db_path}（{db_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"[info] 只读预检通过（mode=ro）：world_entries 当前含 chapter_sequence_number: "
          f"{already_fixed}")

    # 1) 备份先行：cp 等价（shutil.copy2），源库已通过只读校验，备份先于任何修改完成
    if not args.no_backup:
        bak = db_path.with_name(f"{db_path.name}.bak-{datetime.now().strftime('%Y%m%d')}")
        print(f"[info] 备份（cp 等价）: {db_path} -> {bak}")
        shutil.copy2(db_path, bak)
        print(f"[ok] 备份完成（{bak.stat().st_size / 1024 / 1024:.1f} MB）")
    else:
        print("[warn] --no-backup：跳过备份（不推荐）")

    # 2) 补列：只 ADD COLUMN + CREATE INDEX，无任何数据修改语句
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        applied = _ensure_world_entries_column(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"[info] 执行（{SOURCE_NOTE}）:")
    for msg in applied:
        print(f"        {msg}")

    # 3) 验证（只读）
    result = _verify(str(db_path))
    print(f"[verify] PRAGMA table_info(world_entries) 含 chapter_sequence_number: "
          f"{result['column_ok']}")
    print(f"[verify] 索引 ix_world_entries_chapter_sequence_number 存在: {result['index_ok']}")
    print("[verify] world_entries / characters 与 ORM 模型缺列比对（应为空）:")
    all_ok = result["column_ok"] and result["index_ok"]
    for table, cols_missing in result["missing"].items():
        print(f"        {table}: {cols_missing if cols_missing else '无（完全兼容）'}")
        if cols_missing:
            all_ok = False
    if not all_ok:
        print("[error] 验证未通过，见上", file=sys.stderr)
        return 1

    print("[ok] 修复完成：只补列、未改动任何数据行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
