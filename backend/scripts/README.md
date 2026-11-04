# backend/scripts 脚本说明

| 脚本 | 用途 |
|---|---|
| `verify_fts5.py` | **打包后 SQLite FTS5 能力自动化验收**（零第三方依赖，开发环境直接用；打包产物走 `desktop_server.py --verify-fts5` 内联等价断言） |
| `run_index_worker.py` | PG 服务端部署的检索索引 Worker 进程入口 |
| `create_admin.py` | 创建账号（生产环境关闭自助注册后唯一的建号入口）；密码交互输入或不回显地传入，不写日志 |
| `reencrypt_api_keys.py` | 把 `llm_configs.encrypted_api_key` 从旧密钥（JWT_SECRET 派生）重新加密到主密钥 `ENCRYPTION_KEY`；默认 dry-run，加 `--apply` 才写库 |

## verify_fts5.py —— 打包后 FTS5 自动化验收

检索索引依赖 SQLite FTS5 虚拟表（`CREATE VIRTUAL TABLE ... USING fts5`，见
`services/retrieval_indexer.py` 的 `FTS5_CREATE_SQL`）。FTS5 是 **SQLite 编译期
特性**（`ENABLE_FTS5`），开发环境验证通过不代表打包后的运行时（PyInstaller 捆绑
的 Python/sqlite3 扩展）一定启用，因此必须在自动化构建中验收。
《RAG增强评估_小说检索方法论引入.md》§6 要求：打包后的后端进程内**实际执行**
建表 + 插入中文 + 检索，成功才通过验收。

### 验收内容

1. 输出 `sqlite3.sqlite_version` 与 `PRAGMA compile_options`（是否含 `ENABLE_FTS5`）；
2. 内存库中实际执行与应用 DDL 同构的 `CREATE VIRTUAL TABLE ... USING fts5(...)`
   （含 `UNINDEXED` 列与 `search_text` 检索列）；
3. 插入中文内容（原文 content + 空格分词 search_text，模拟应用侧 jieba 预分词契约），
   执行 MATCH 检索、bm25 排序，断言命中、AND 语义、无过度召回与排序；
4. 任一环节失败 → stderr 输出明确错误并退出非 0。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 通过：FTS5 可用，建表/中文插入/MATCH 检索/bm25 排序全部断言成功 |
| 1 | 当前运行时 SQLite **未启用 FTS5**（`ENABLE_FTS5` 缺失），打包验收失败 |
| 2 | FTS5 已启用但实际建表/检索/排序断言失败 |

### 用法

开发环境：

```bash
cd backend
venv/bin/python scripts/verify_fts5.py     # exit 0 = 通过
```

打包后（推荐做法，对**实际后端产物**自检）：

不单独把本脚本打成 exe，而是直接对 `npm run backend:build` 产出的
`ai-creative-backend` 可执行文件运行 `--verify-fts5`——`desktop_server.py` 的
`_verify_fts5()` 内联了与本脚本等价的全套断言（建表/中文插入/MATCH 检索/bm25
排序，退出码语义一致：0 = 通过，1 = 运行时未启用 FTS5，2 = 断言失败）。这样验收
的就是最终随 Electron 分发的真实产物，而不是旁路打出的自检 exe：

```bash
# macOS CI（产物为 PyInstaller onefile，位于 frontend/backend-dist/）
frontend/backend-dist/ai-creative-backend --verify-fts5   # 非 0 退出 → 构建失败

# Windows CI
frontend\backend-dist\ai-creative-backend.exe --verify-fts5
```

Windows（`.github/workflows/build-windows-desktop.yml`）与 macOS
（`.github/workflows/build-macos-desktop.yml`）两条工作流均在
`npm run backend:build` 之后以同名步骤 `Smoke-test packaged SQLite FTS5 support`
对实际产物执行 `--verify-fts5`，双平台打包运行时均被覆盖。

### 为什么不并入 run_index_worker.py

`run_index_worker.py` 是 **PG 服务端**部署的索引 Worker 入口：默认走
`config.settings.DATABASE_URL`（PostgreSQL），SQLite 方言（桌面端）下它会打印
提示并退出——桌面端索引由 `request_chapter_index` 同步构建，不启动 Worker。
FTS5 验收针对的是 **SQLite 打包运行时**（桌面端），归属打包冒烟步骤（CI 中在
`npm run backend:build` 之后执行），因此不并入该 Worker；如需在服务端也做一次性
自检，可直接运行 `venv/bin/python scripts/verify_fts5.py`（不依赖任何服务端配置）。
