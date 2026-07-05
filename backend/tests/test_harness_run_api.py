"""Run 查询 API 路由测试 — SQLite 内存 + TestClient"""

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.ext.compiler import compiles

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401
import tests.test_smoke as smoke
from db.session import get_db, set_engine
from main import app

_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory
client = smoke.client


@pytest.fixture(autouse=True)
def _ensure_db():
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_cached_auth: dict[str, str] = {}


def _auth_headers(username="runapiuser", password="runapipass"):
    """注册+登录并返回 Authorization headers。Token 按用户名缓存，避免触发速率限制。"""
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _create_project_and_run(headers, mode="full_pipeline"):
    """建项目 + 章节 + 发起生成，返回 (project_id, run_id)。"""
    proj = client.post("/api/projects", json={"title": "run api 测试", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]
    client.post(f"/api/projects/{pid}/chapters", json={"title": "ch1", "sequence_number": 1}, headers=headers)
    resp = client.post(f"/api/projects/{pid}/chapters/generate",
                       json={"mode": mode, "chapter_num": 1, "target_words": 200}, headers=headers)
    run_id = None
    for block in resp.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: ") and "run_id" in line:
                try:
                    d = json.loads(line[6:])
                    run_id = d.get("run_id")
                except Exception:
                    pass
    return pid, run_id


def test_ai_run_response_schema():
    """AiRunResponse 可从 ORM 对象构造（flush 后 id/created_at 有值）。"""
    from schemas.api import AiRunResponse
    from models.ai_run import AiRun
    from models.harness_enums import RunStatus

    async def _go():
        async with _test_session_factory() as s:
            run = AiRun(project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
            s.add(run)
            await s.flush()
            resp = AiRunResponse.model_validate(run)
            assert resp.run_type == "CHAPTER_DRAFT"
            assert resp.status == RunStatus.CREATED
            assert resp.token_usage is None
            await s.rollback()
    asyncio.run(_go())


# ==================== GET /api/ai-runs/{run_id} ====================

def test_get_run_by_id_ok():
    """项目内 run 可查，返回完整字段。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/ai-runs/{run_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert data["project_id"] == pid
    assert data["run_type"] == "CHAPTER_DRAFT"
    assert data["status"] in ("COMPLETED", "WAITING_HUMAN", "FAILED", "CANCELLED")


def test_get_run_by_id_cross_user_forbidden():
    """跨用户查 run 返回 404（不泄露存在性）。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    client.post("/api/auth/register", json={"username": "otheruser", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}

    resp = client.get(f"/api/ai-runs/{run_id}", headers=other_headers)
    assert resp.status_code == 404


def test_get_run_by_id_not_found():
    """不存在的 run_id 返回 404。"""
    headers = _auth_headers()
    resp = client.get(f"/api/ai-runs/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


# ==================== GET /api/ai-runs/{run_id}/steps ====================

def test_get_run_steps_ordered():
    """steps 按 step_order 排序，含 llm_call_count。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/ai-runs/{run_id}/steps", headers=headers)
    assert resp.status_code == 200
    steps = resp.json()
    assert len(steps) >= 1
    # 按 step_order 升序
    orders = [s["step_order"] for s in steps]
    assert orders == sorted(orders)
    # 每个 step 有 llm_call_count 字段
    for s in steps:
        assert "llm_call_count" in s
        assert isinstance(s["llm_call_count"], int)
    # 至少有 build_context
    step_names = [s["step_name"] for s in steps]
    assert "build_context" in step_names
    # 不暴露大字段 input/output
    for s in steps:
        assert "input" not in s
        assert "output" not in s


def test_get_run_steps_cross_user_forbidden():
    """跨用户查 steps 返回 404。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    client.post("/api/auth/register", json={"username": "otheruser2", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser2", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}
    resp = client.get(f"/api/ai-runs/{run_id}/steps", headers=other_headers)
    assert resp.status_code == 404


# ==================== GET /api/ai-runs/{run_id}/context ====================

def test_get_run_context_extracts_context_snapshot():
    """run context API 返回每次 LLM 调用的上下文段与快照摘要。"""
    headers = _auth_headers("runctxuser", "runctxpass")
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    async def _insert_log():
        from models.llm_call_log import LlmCallLog

        async with _test_session_factory() as s:
            log = LlmCallLog(
                run_id=uuid.UUID(run_id),
                project_id=uuid.UUID(pid),
                agent_name="chapter_writer",
                provider="mock",
                model="mock-1",
                rendered_prompt_snapshot=(
                    "[system]\n系统提示\n\n"
                    "[user]\n## 上下文\n"
                    "## 本章大纲\n主角醒来发现世界规则异常。\n\n"
                    "## 角色资料\n- 程旌：穿越者。\n\n"
                    "## 章节信息\n第1章，目标字数200\n"
                ),
                context_package_snapshot={"context_len": 42, "source": "test"},
                request={"prompt_snapshot_truncated": True},
            )
            s.add(log)
            await s.commit()

    asyncio.run(_insert_log())

    resp = client.get(f"/api/ai-runs/{run_id}/context", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    calls = [c for c in data["calls"] if c["agent_name"] == "chapter_writer" and c["context_snapshot"]]
    assert calls
    call = calls[-1]
    assert call["context_snapshot"] == {"context_len": 42, "source": "test"}
    assert "## 本章大纲" in call["context_text"]
    assert "## 角色资料" in call["context_text"]
    assert "## 章节信息" not in call["context_text"]
    assert call["prompt_truncated"] is True


def test_get_run_context_cross_user_forbidden():
    """跨用户查 context 返回 404。"""
    headers = _auth_headers("runctxowner", "runctxpass")
    _pid, run_id = _create_project_and_run(headers)

    other_headers = _auth_headers("runctxother", "runctxpass")
    resp = client.get(f"/api/ai-runs/{run_id}/context", headers=other_headers)
    assert resp.status_code == 404


# ==================== GET /api/projects/{project_id}/ai-runs ====================

def test_list_project_runs():
    """项目 run 列表，按 created_at 降序。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/projects/{pid}/ai-runs", headers=headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) >= 1
    assert any(r["id"] == run_id for r in runs)
    # 降序
    created = [r["created_at"] for r in runs]
    assert created == sorted(created, reverse=True)


def test_list_project_runs_limit():
    """limit 参数生效。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)

    resp = client.get(f"/api/projects/{pid}/ai-runs?limit=1", headers=headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) <= 1


def test_list_project_runs_cross_user_forbidden():
    """跨用户查项目 run 列表返回 404。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    client.post("/api/auth/register", json={"username": "otheruser3", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser3", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}
    resp = client.get(f"/api/projects/{pid}/ai-runs", headers=other_headers)
    assert resp.status_code == 404
