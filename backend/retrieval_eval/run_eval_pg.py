#!/usr/bin/env python3
"""在 PostgreSQL 上跑 run_baseline.py（lexical 检索器）的包装器（阶段一收尾）。

背景
----
run_baseline.main() 会无条件把 ``os.environ["DATABASE_URL"]`` 覆盖为
``--db`` 指向的 sqlite 路径（保证 ILIKE 基线/项目解析连 dev-web.sqlite3）。
因此直接用环境变量 DATABASE_URL 无法让检索侧切到 PG。

本包装器的做法：在调用 run_baseline.main() 之前，先把 DATABASE_URL 指向
PostgreSQL 并 **预先 import db.session**（settings 在 import 时读环境变量，
engine 在模块加载时创建并绑定 PG）。之后 main() 的 os.environ 覆盖对已创建
的全局 engine 无效 —— lexical 检索器即跑在 PG 上；``--db``（sqlite）仍用于
项目解析与数据集加载（只读），行为与 sqlite 模式完全一致。

前置条件
--------
1. 已运行 ``retrieval_eval/setup_pg.py --import-sqlite``（评测 chunks 已在 PG）；
2. 项目词汇：PG 业务表中有该 project 的词典数据时实体优先通道生效；
   若 PG 无该项目业务数据（如 dev-web 的 d62d4447），词汇表为空 → 仅默认
   jieba 分词 + 词法通道（报告中注明该差异）。

用法
----
    cd backend
    venv/bin/python retrieval_eval/run_eval_pg.py --retriever lexical --pg-url "$TEST_PG_URL" \
        --db backend/dev-web.sqlite3 --project <pid> \
        --dataset retrieval_eval/datasets/regression_candidates.json \
        --limit 5 --out retrieval_eval/results/lexical_pg_regression_76.json
    # 必须通过 --pg-url 或 DATABASE_URL 显式指定 PostgreSQL 测试库。

其余参数与 run_baseline.py 完全一致（透传），退出码也一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

def main() -> int:
    argv = sys.argv[1:]
    pg_url = os.environ.get("DATABASE_URL", "")
    if "--pg-url" in argv:
        idx = argv.index("--pg-url")
        if idx + 1 >= len(argv):
            print("[error] --pg-url 缺少连接串", file=sys.stderr)
            return 2
        pg_url = argv[idx + 1]
        del argv[idx:idx + 2]
    if not pg_url:
        print("[error] 必须通过 --pg-url 或 DATABASE_URL 显式指定 PostgreSQL 测试库", file=sys.stderr)
        return 2
    if not pg_url.startswith("postgresql"):
        print(f"[error] DATABASE_URL 必须是 PostgreSQL 连接串: {pg_url}", file=sys.stderr)
        return 2

    os.environ["DATABASE_URL"] = pg_url
    print(f"[eval-pg] 检索侧数据库: {pg_url}")

    # 关键步骤：预先导入 db.session，让全局 engine 绑定 PG。
    # run_baseline.main() 后续对 DATABASE_URL 的 sqlite 覆盖不影响已建 engine。
    from db.session import async_session  # noqa: F401

    from retrieval_eval.run_baseline import main as run_baseline_main
    sys.argv = [sys.argv[0]] + argv
    return run_baseline_main()


if __name__ == "__main__":
    sys.exit(main())
