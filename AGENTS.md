# 项目约定

## 桌面端发布流程（重要，用户明确要求）

- 本项目的日常使用形态是 **Electron 桌面端**：`frontend/release/mac-arm64/AI 创作平台.app`，
  由 `npm run desktop:build:mac` 构建（内嵌 PyInstaller 打包的后端二进制）。
- **每次修改前端或后端代码后，默认都要：**
  1. 在 `frontend/` 下运行 `npm run desktop:build:mac` 重新构建（约 8-10 分钟：
     assets → 后端 PyInstaller → vue-tsc + vite → electron-builder 产出 DMG/ZIP/.app）；
  2. 退出正在运行的旧版桌面端（`kill` 主进程或用户 Cmd+Q，确认 8765 端口释放），
     再启动新版：`open frontend/release/mac-arm64/AI 创作平台.app`。
- 浏览器 dev（localhost:5173，vite 热更新）仅用于快速预览；**最终交付以桌面端为准**。
- 产物位置：`frontend/release/`（DMG/ZIP）与 `frontend/release/mac-arm64/AI 创作平台.app`。

## 数据环境

- 桌面端数据：`~/Library/Application Support/AI 创作平台/backend-data/ai_creative.sqlite3`
  （迁移前先备份副本）。
- 网页端（开发/PostgreSQL）：`postgresql://postgres:postgres@localhost:5432/ai_creative`
  （docker-compose）。两边是同一套数据的不同副本，注意同步。
- API Key 用 Fernet 加密，密钥派生自 JWT_SECRET：桌面端读 `backend-data/jwt_secret` 文件，
  网页端是随机生成（进程退出即丢失）→ 跨库迁移配置时 API Key 无法解密，需用户重填。

## 测试

- 前端：`cd frontend && npm test`（源码契约断言风格，无 DOM）。
- 后端：`cd backend && venv/bin/python -m pytest tests/xxx.py`（逐文件跑，
  批量跑会触发认证限流 429：AUTH_RATE_LIMIT_PER_MINUTE=10）。
