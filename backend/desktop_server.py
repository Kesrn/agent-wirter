"""Desktop-packaged backend entrypoint.

This entrypoint is used by Electron. It configures a local SQLite database and
user-data upload directory before importing the FastAPI app.

桌面端不要求用户安装 PostgreSQL/Docker，所以这里在导入 main.app 之前先覆盖
DATABASE_URL、UPLOAD_DIR、JWT_SECRET 等环境变量。导入顺序很关键：settings.py
在 import 时就会读取环境变量，必须先 configure_desktop_environment。
"""

from __future__ import annotations

import os
import platform
import secrets
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
    jwt_secret 写到用户数据目录，可以保证应用重启后 token/API Key 解密仍然有效。
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
    # 同样固定使用随桌面数据目录持久化的密钥，保证重启后本地登录态有效。
    os.environ["JWT_SECRET"] = secret_path.read_text(encoding="utf-8").strip()
    # Mock provider 已被禁用，用户需要在前端配置真实的 LLM provider
    # os.environ.setdefault("LLM_PROVIDER", "mock")
    # os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
    os.environ.setdefault("CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173,null")
    return data_dir


def main() -> None:
    """Electron 启动的后端进程入口。"""
    configure_desktop_environment()

    import uvicorn
    from main import app
    from config.settings import settings

    host = os.getenv("AI_CREATIVE_BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("AI_CREATIVE_BACKEND_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level=settings.LOG_LEVEL.lower(), access_log=False)


if __name__ == "__main__":
    main()
