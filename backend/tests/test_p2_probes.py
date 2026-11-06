"""P2 运行探针、连接池及知识集合分页边界。"""

import db.session as db_session
import main
from config.settings import settings
from test_smoke import _auth_headers, client, setup_db, test_engine  # noqa: F401


def test_health_live_is_independent_of_database(monkeypatch):
    def fail_if_touched():
        raise AssertionError("liveness probe accessed database")

    monkeypatch.setattr(main, "get_engine", fail_if_touched)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_returns_each_check_status(monkeypatch):
    monkeypatch.setattr(main, "get_engine", lambda: test_engine)
    main.app.state.database_ready = True
    main.app.state.checkpointer_ready = True

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database_initialized": True,
            "database_connected": True,
            "checkpointer_initialized": True,
        },
    }


def test_health_ready_hides_database_errors(monkeypatch):
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("private connection details")

    monkeypatch.setattr(main, "get_engine", BrokenEngine)
    main.app.state.database_ready = True
    main.app.state.checkpointer_ready = True

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database_connected"] is False
    assert "private connection details" not in response.text


def test_pool_settings_only_apply_to_non_sqlite(monkeypatch):
    calls = []

    def fake_create_async_engine(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(db_session, "create_async_engine", fake_create_async_engine)

    db_session._create_engine("postgresql+asyncpg://db/test", echo=True)
    assert calls[-1] == (
        "postgresql+asyncpg://db/test",
        {
            "echo": True,
            "pool_pre_ping": True,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_timeout": settings.DB_POOL_TIMEOUT,
            "pool_recycle": settings.DB_POOL_RECYCLE,
        },
    )

    db_session._create_engine("sqlite+aiosqlite:////tmp/p2-pool-test.sqlite", echo=False)
    assert calls[-1] == (
        "sqlite+aiosqlite:////tmp/p2-pool-test.sqlite",
        {"echo": False},
    )


def test_knowledge_list_pagination_bounds_return_422():
    headers = _auth_headers("p2_pagination_probe")
    project_response = client.post("/api/projects", json={"title": "分页边界"}, headers=headers)
    assert project_response.status_code == 200, project_response.text
    project_id = project_response.json()["id"]

    urls = [
        f"/api/projects/{project_id}/knowledge/facts?limit=201",
        f"/api/projects/{project_id}/knowledge/facts?offset=-1",
        f"/api/projects/{project_id}/knowledge/structured/characters?limit=201",
        f"/api/projects/{project_id}/knowledge/structured/characters?offset=-1",
        f"/api/projects/{project_id}/knowledge/structured/characters/00000000-0000-0000-0000-000000000000/appearances?limit=201",
        f"/api/projects/{project_id}/knowledge/structured/characters/00000000-0000-0000-0000-000000000000/appearances?offset=-1",
    ]
    for url in urls:
        response = client.get(url, headers=headers)
        assert response.status_code == 422, f"{url}: {response.status_code} {response.text}"
