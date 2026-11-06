"""桌面端启动配置必须隔离开发环境变量。"""

import os

from desktop_server import configure_desktop_environment


def test_desktop_server_overrides_inherited_database_url(monkeypatch, tmp_path):
    """即使终端带着 PostgreSQL 配置，桌面端也只能使用自己的 SQLite 文件。"""
    data_dir = tmp_path / "desktop-data"
    monkeypatch.setenv("AI_CREATIVE_DESKTOP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://should-not-be-used")
    monkeypatch.setenv("UPLOAD_DIR", "/tmp/wrong-upload-dir")
    monkeypatch.setenv("JWT_SECRET", "wrong-secret")

    configured_dir = configure_desktop_environment()

    assert configured_dir == data_dir
    assert os.environ["DATABASE_URL"] == f"sqlite+aiosqlite:///{data_dir / 'ai_creative.sqlite3'}"
    assert os.environ["UPLOAD_DIR"] == str(data_dir / "uploads")
    assert os.environ["JWT_SECRET"] == (data_dir / "jwt_secret").read_text(encoding="utf-8").strip()
