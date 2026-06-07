"""Desktop-packaged backend entrypoint.

This entrypoint is used by Electron. It configures a local SQLite database and
user-data upload directory before importing the FastAPI app.
"""

from __future__ import annotations

import os
import platform
import secrets
from pathlib import Path


APP_NAME = "AI Creative Platform"


def _default_data_dir() -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        return Path(os.getenv("APPDATA", home / "AppData" / "Roaming")) / APP_NAME
    return Path(os.getenv("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME


def configure_desktop_environment() -> Path:
    data_dir = Path(os.getenv("AI_CREATIVE_DESKTOP_DATA_DIR", _default_data_dir()))
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = data_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'ai_creative.sqlite3'}")
    os.environ.setdefault("UPLOAD_DIR", str(uploads_dir))
    os.environ.setdefault("DEBUG", "false")
    secret_path = data_dir / "jwt_secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    os.environ.setdefault("JWT_SECRET", secret_path.read_text(encoding="utf-8").strip())
    os.environ.setdefault("LLM_PROVIDER", "mock")
    os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
    os.environ.setdefault("CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173,null")
    return data_dir


def main() -> None:
    configure_desktop_environment()

    import uvicorn
    from main import app

    host = os.getenv("AI_CREATIVE_BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("AI_CREATIVE_BACKEND_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
