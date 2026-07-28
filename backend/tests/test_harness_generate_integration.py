"""generate_chapter full_pipeline 的 Harness 集成测试。

自包含：自带 TestClient + SQLite 内存库（复用 test_smoke 的 override 套路，
但独立建表，避免依赖 test_smoke 的 session 级 fixture 执行顺序）。
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.ext.compiler import compiles

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401 — 注册全部模型
import tests.test_smoke as smoke  # 复用其 TestClient/engine/override，避免双引擎冲突
from db.session import get_db
from main import app

# 复用 test_smoke 已建好的 SQLite 内存引擎、session factory 与 TestClient。
# test_smoke 在模块级 set_engine + override_get_db + app.dependency_overrides。
# 本文件不引入第二个引擎；表由下方 autouse fixture 保证存在（与 test_smoke 同一内存库）。
_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory
client = smoke.client


@pytest.fixture(autouse=True)
def _ensure_db():
    """保证表存在 + 全局 engine 指向测试 SQLite（LoggedLLMProvider 用 get_engine()）。"""
    from db.session import set_engine
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _parse_sse(raw: str) -> list[tuple[str, object]]:
    events = []
    for block in raw.split("\n\n"):
        ev = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if ev or data:
            try:
                events.append((ev, json.loads(data)))
            except json.JSONDecodeError:
                events.append((ev, data))
    return events


def _auth_headers():
    client.post("/api/auth/register", json={"username": "harnessuser", "password": "harnesspass"})
    resp = client.post("/api/auth/login", json={"username": "harnessuser", "password": "harnesspass"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_full_pipeline_leaves_run_and_steps():
    from models.ai_run import AiRun
    from models.ai_run_step import AiRunStep
    from models.generation_record import GenerationRecord
    from models.harness_enums import RunStatus

    headers = _auth_headers()
    proj = client.post("/api/projects", json={"title": "harness 测试", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]
    client.post(f"/api/projects/{pid}/chapters", json={"title": "第一章", "sequence_number": 1}, headers=headers)

    resp = client.post(
        f"/api/projects/{pid}/chapters/generate",
        json={"mode": "full_pipeline", "chapter_num": 1, "target_words": 200},
        headers=headers,
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    event_names = [e for e, _ in events]

    # 旧事件仍在
    assert "agent_start" in event_names
    assert "writer_output" in event_names
    # 新事件出现
    assert "run_created" in event_names

    async def _query():
        async with _test_session_factory() as s:
            runs = (await s.execute(select(AiRun).where(AiRun.project_id == pid))).scalars().all()
            assert len(runs) >= 1
            run = runs[0]
            assert run.run_type == "CHAPTER_DRAFT"
            assert run.status in (RunStatus.COMPLETED, RunStatus.WAITING_HUMAN, RunStatus.FAILED)
            steps = (await s.execute(
                select(AiRunStep).where(AiRunStep.run_id == run.id).order_by(AiRunStep.step_order)
            )).scalars().all()
            step_names = [s.step_name for s in steps]
            assert "build_context" in step_names
            assert "generate_draft" in step_names
            recs = (await s.execute(
                select(GenerationRecord).where(GenerationRecord.run_id == run.id)
            )).scalars().all()
            assert len(recs) >= 1
            # llm_call_logs：full_pipeline 至少有 writer 记录，run_id 关联，latency/prompt 非空
            from models.llm_call_log import LlmCallLog
            logs = (await s.execute(
                select(LlmCallLog).where(LlmCallLog.run_id == run.id)
            )).scalars().all()
            assert len(logs) >= 1
            log_agents = {l.agent_name for l in logs}
            assert "chapter_writer" in log_agents or "writer" in log_agents or any("expert" in a for a in log_agents), (
                f"writer/expert log missing: {log_agents}"
            )
            for l in logs:
                assert l.latency_ms is not None
                assert l.rendered_prompt_snapshot is not None
                assert l.run_id == run.id
                assert l.input_tokens is None  # 第一阶段允许 NULL

    asyncio.run(_query())
