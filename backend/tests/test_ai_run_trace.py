from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from api.auth import get_current_user
from api.request_id import request_id_var
from db.session import get_db, get_engine, set_engine
from models import AiRun, AiRunStep, Base, LlmCallLog, Project
from schemas.api import AuthUser


@pytest_asyncio.fixture
async def trace_client(tmp_path):
    from api.routes import router

    old_engine = get_engine()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    set_engine(engine)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    auth_state = {"user_id": "owner-1"}

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return AuthUser(
            id=auth_state["user_id"], username="trace-user", display_name="Trace User",
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        yield client, factory, auth_state
    app.dependency_overrides.clear()
    set_engine(old_engine)
    await engine.dispose()


class _FailingCriticProvider:
    model = "fake-critic"

    async def generate(self, *args, **kwargs):
        raise RuntimeError("critic LLM boom")

    async def generate_stream(self, *args, **kwargs):
        raise RuntimeError("critic LLM boom")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_trace_endpoint_sums_calls_hides_prompts_and_locates_critic_failure(trace_client):
    from harness.llm_call_logger import LoggedLLMProvider
    from harness.run_manager import create_run, mark_failed, mark_running
    from harness.step_logger import current_step_id_var, fail_step, finish_step, start_step

    client, factory, auth_state = trace_client
    async with factory() as db:
        project = Project(title="Trace test", owner_id="owner-1")
        db.add(project)
        await db.flush()

        request_token = request_id_var.set("trace-request-001")
        run = await create_run(
            db, project_id=project.id, run_type="CHAPTER_DRAFT", mode="full_pipeline",
            workflow_key="workflow-v2",
        )
        await mark_running(db, run)
        run.started_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        critic_step = await start_step(
            db, run_id=run.id, step_order=1, step_name="critique", agent_name="critic",
        )
        critic_step.started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        await db.commit()  # The independent LLM log session must see the run and step.

        try:
            with pytest.raises(RuntimeError, match="critic LLM boom"):
                await LoggedLLMProvider(
                    _FailingCriticProvider(), run_id=run.id, agent_name="critic", provider_name="fake",
                ).generate("system", "critic request")
        finally:
            await fail_step(db, critic_step, error_message="critic LLM boom")
        await db.commit()

        writer_step = await start_step(
            db, run_id=run.id, step_order=2, step_name="chapter_writer", agent_name="writer",
        )
        writer_step.started_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        structured_request = {
            "retries": 1,
            "usage_estimated": False,
            "structured": {"schema": "Draft", "repairs": 1, "ok": True},
            "structured_context_id": "structured-call-1",
            "prompt_snapshot_truncated": True,
        }
        first_log_created_at = datetime.now(timezone.utc) - timedelta(milliseconds=10)
        db.add_all([
            LlmCallLog(
                run_id=run.id, step_id=writer_step.id, agent_name="writer", model="fake-writer",
                latency_ms=40, input_tokens=100, output_tokens=50, total_tokens=150,
                cost=Decimal("0.010000"), request=structured_request,
                rendered_prompt_snapshot="P" * 9000,
                created_at=first_log_created_at,
            ),
            LlmCallLog(
                run_id=run.id, step_id=writer_step.id, agent_name="writer", model="fake-writer",
                latency_ms=20, input_tokens=30, output_tokens=10, total_tokens=40,
                cost=Decimal("0.002000"), request=structured_request,
                rendered_prompt_snapshot="repair prompt",
                created_at=first_log_created_at + timedelta(milliseconds=1),
            ),
        ])
        writer_step.output = {
            "draft_quality_report": {
                "stage": "draft", "passed": False,
                "issues": [{"code": "RULE_MUST_INCLUDE_MISSING", "severity": "ERROR"}],
            },
        }
        await finish_step(db, writer_step, output=writer_step.output)
        await mark_failed(db, run, error_message="critic failed")
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        run_id = str(run.id)
        request_id_var.reset(request_token)

    response = await client.get(f"/api/ai-runs/{run_id}/trace")
    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["request_id"] == "trace-request-001"
    assert payload["failed_step"] == {
        "step_order": 1,
        "step_name": "critique",
        "agent_name": "critic",
        "error_message": "critic LLM boom",
    }
    assert payload["steps"][0]["llm_calls"][0]["error_message"] == "critic LLM boom"
    assert payload["steps"][0]["llm_calls"][0]["rendered_prompt_snapshot"] is None
    assert payload["steps"][1]["status"] == "COMPLETED"
    assert payload["steps"][1]["quality"] == {
        "stage": "draft", "passed": False, "error_codes": ["RULE_MUST_INCLUDE_MISSING"],
    }
    calls = [call for step in payload["steps"] for call in step["llm_calls"]]
    totals = payload["totals"]
    assert totals["llm_calls"] == len(calls) == 3
    assert totals["input_tokens"] == sum(call["input_tokens"] or 0 for call in calls)
    assert totals["output_tokens"] == sum(call["output_tokens"] or 0 for call in calls)
    assert totals["total_tokens"] == sum(call["total_tokens"] or 0 for call in calls)
    assert totals["cost"] == pytest.approx(sum(call["cost"] or 0 for call in calls))
    assert totals["retries"] == sum(call["retries"] for call in calls)
    assert totals["estimated_calls"] == sum(call["usage_estimated"] for call in calls)
    assert totals["structured_repairs"] == 1

    prompt_response = await client.get(f"/api/ai-runs/{run_id}/trace?include_prompts=true")
    prompted = prompt_response.json()
    long_prompt = prompted["steps"][1]["llm_calls"][0]
    assert len(long_prompt["rendered_prompt_snapshot"]) == 8000
    assert long_prompt["prompt_truncated"] is True

    auth_state["user_id"] = "other-user"
    other_owner = await client.get(f"/api/ai-runs/{run_id}/trace")
    assert other_owner.status_code == 404
    auth_state["user_id"] = "owner-1"
    missing = await client.get(f"/api/ai-runs/{uuid.uuid4()}/trace")
    assert missing.status_code == 404


def test_trace_builder_caps_steps_and_calls_and_marks_truncated():
    from services.ai_run_trace import build_ai_run_trace

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id=uuid.uuid4(), request_id="r", status="COMPLETED", run_type="test", mode="full_pipeline",
        workflow_key=None, started_at=now, finished_at=now, error_message=None,
    )
    steps = [SimpleNamespace(
        id=uuid.uuid4(), step_order=index, step_name=f"step-{index}", node_key=None,
        agent_name=None, status="SUCCESS", started_at=now, ended_at=now,
        created_at=now, retry_count=0, max_retry=0, error_message=None, output=None,
    ) for index in range(501)]
    logs = [SimpleNamespace(
        id=uuid.uuid4(), step_id=steps[0].id, created_at=now, request={},
        rendered_prompt_snapshot=None, input_tokens=None, output_tokens=None,
        total_tokens=None, cost=None, latency_ms=None, agent_name=None, model=None,
        error_message=None,
    ) for _ in range(51)]

    result = build_ai_run_trace(run, steps, logs)
    assert len(result["steps"]) == 500
    assert len(result["steps"][0]["llm_calls"]) == 50
    assert result["totals"]["llm_calls"] == 50
    assert result["truncated"] is True
