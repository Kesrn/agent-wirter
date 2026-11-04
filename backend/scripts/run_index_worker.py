#!/usr/bin/env python3
"""检索索引 Worker 进程入口（PG 服务端部署）。

用法::

    cd backend
    venv/bin/python scripts/run_index_worker.py [--db URL] [--interval 5] [--batch 10] [--once]

- 默认数据库取 config.settings.DATABASE_URL（PG）；--db 可显式覆盖。
- SQLite 方言（桌面端）打印提示并退出：桌面端 finalize 由
  request_chapter_index 同步构建，不启动 Worker。
- SIGINT/SIGTERM 优雅退出（当前批处理完再停）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.retrieval_index_worker import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
