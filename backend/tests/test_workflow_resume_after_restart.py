"""持久化 checkpointer 的应用重启恢复测试。"""

import asyncio
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from test_smoke import app, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


@pytest.fixture(autouse=True)
def _mock_llm_provider(monkeypatch):
    from agents.llm_provider import MockProvider
    import agents.llm_provider as llm_provider
    import agents.workflow as workflow
    import agents.workflow_v2 as workflow_v2
    import api.routes as routes

    def _get_mock_provider(_config=None):
        return MockProvider()

    monkeypatch.setattr(llm_provider, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(workflow, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(workflow_v2, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(routes, "get_llm_provider", _get_mock_provider)


def _query_run(run_id: str):
    async def _query():
        from models.ai_run import AiRun

        async with test_session_factory() as session:
            return await session.get(AiRun, uuid.UUID(run_id))

    return asyncio.new_event_loop().run_until_complete(_query())


def test_persisted_state_survives_application_restart(tmp_path, monkeypatch):
    """关闭并重启 FastAPI lifespan 后，同一 SQLite checkpoint 可继续 approve。"""
    from agents.checkpointer import ManagedAsyncCheckpointer
    import agents.workflow as workflow_mod
    import agents.workflow_v2 as workflow_v2_mod

    checkpoint_path = str(tmp_path / "checkpoints.sqlite")

    saver1 = ManagedAsyncCheckpointer(
        backend="sqlite",
        connection_string=checkpoint_path,
    )
    monkeypatch.setattr(workflow_mod, "_CHECKPOINTER", saver1)
    monkeypatch.setattr(workflow_v2_mod, "_CHECKPOINTER", saver1)

    headers = _auth_headers("restart_test", "restart_test")
    with TestClient(app) as first_process:
        response = first_process.post(
            "/api/projects",
            json={"title": "restart 测试", "mode": "novel"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        project_id = response.json()["id"]
        first_process.post(
            f"/api/projects/{project_id}/chapters",
            json={"title": "第一章", "sequence_number": 1},
            headers=headers,
        )
        response = first_process.post(
            f"/api/projects/{project_id}/chapters/generate",
            json={"chapter_num": 1, "mode": "full_pipeline", "target_words": 200},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        thread_match = re.search(r'"thread_id":\s*"([^"]+)"', response.text)
        run_match = re.search(r'"run_id":\s*"([^"]+)"', response.text)
        assert thread_match and run_match, response.text
        thread_id = thread_match.group(1)
        run_id = run_match.group(1)
        assert str(_query_run(run_id).status) == "WAITING_HUMAN"

    # 新进程使用新 saver 实例，但连接同一个 checkpoint 文件。
    saver2 = ManagedAsyncCheckpointer(
        backend="sqlite",
        connection_string=checkpoint_path,
    )
    monkeypatch.setattr(workflow_mod, "_CHECKPOINTER", saver2)
    monkeypatch.setattr(workflow_v2_mod, "_CHECKPOINTER", saver2)

    with TestClient(app) as second_process:
        response = second_process.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": thread_id, "action": "approve"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert "event: done" in response.text, response.text

    assert str(_query_run(run_id).status) == "COMPLETED"
