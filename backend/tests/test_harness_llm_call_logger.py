"""LoggedLLMProvider 单元测试 — SQLite 内存 + stub provider"""

import asyncio
import uuid

import pytest
import pytest_asyncio
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
from models.llm_call_log import LlmCallLog


@pytest_asyncio.fixture
async def async_db():
    from db.session import set_engine
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_engine(engine)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sf() as session:
        yield session
    await engine.dispose()


class _StubProvider:
    """最小 stub，模拟 LLMProvider.generate（返回固定串，sleep 模拟 latency）。"""
    model = "stub-model"

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        await asyncio.sleep(0.05)
        return "stub result"

    async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        for chunk in ["stub", " ", "result"]:
            yield chunk


class _FailingProvider:
    model = "fail-model"

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        raise RuntimeError("LLM boom")

    async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        raise RuntimeError("LLM boom stream")
        yield  # noqa: F821 — 使其为 async generator


@pytest.mark.asyncio
async def test_logged_provider_generate_writes_log(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    provider = LoggedLLMProvider(
        _StubProvider(),
        run_id=run_id,
        step_id=step_id,
        agent_name="writer",
        provider_name="stub",
    )
    result = await provider.generate("sys", "usr", temperature=0.8)
    assert result == "stub result"

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.run_id == run_id
    assert log.step_id == step_id
    assert log.agent_name == "writer"
    assert log.provider == "stub"
    assert log.model == "stub-model"
    assert log.latency_ms is not None and log.latency_ms >= 40
    assert "sys" in log.rendered_prompt_snapshot
    assert "usr" in log.rendered_prompt_snapshot
    assert log.error_message is None
    assert log.input_tokens == 3  # 没有 SDK 用量时按字符数估算
    assert log.output_tokens == 6
    assert log.total_tokens == 9
    assert log.request["usage_estimated"] is True


@pytest.mark.asyncio
async def test_logged_provider_generate_failure_logs_error(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _FailingProvider(),
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="critic",
        provider_name="stub",
    )
    with pytest.raises(RuntimeError, match="LLM boom"):
        await provider.generate("sys", "usr")

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.agent_name == "critic"
    assert log.error_message == "LLM boom"
    assert log.latency_ms is not None
    assert log.rendered_prompt_snapshot is not None


@pytest.mark.asyncio
async def test_logged_provider_context_snapshot(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _StubProvider(),
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="consistency_checker",
        provider_name="stub",
        context_snapshot={"context_len": 1234, "stats": {"chars": 100}},
    )
    await provider.generate("sys", "usr")

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.context_package_snapshot == {"context_len": 1234, "stats": {"chars": 100}}


@pytest.mark.asyncio
async def test_logged_provider_generate_stream_passes_through(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _StubProvider(),
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="writer",
        provider_name="stub",
    )
    chunks = []
    async for c in provider.generate_stream("sys", "usr"):
        chunks.append(c)
    assert chunks == ["stub", " ", "result"]
    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.agent_name == "writer"
    assert log.latency_ms is not None


@pytest.mark.asyncio
async def test_logged_provider_prompt_snapshot_truncation(async_db):
    """超长 prompt 截断到 MAX_PROMPT_SNAPSHOT_CHARS，并在 request 记 truncated 标记。"""
    from harness.llm_call_logger import LoggedLLMProvider, MAX_PROMPT_SNAPSHOT_CHARS

    long_sys = "S" * (MAX_PROMPT_SNAPSHOT_CHARS + 500)
    long_usr = "U" * (MAX_PROMPT_SNAPSHOT_CHARS + 500)
    provider = LoggedLLMProvider(
        _StubProvider(),
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="writer",
        provider_name="stub",
    )
    await provider.generate(long_sys, long_usr)

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert len(log.rendered_prompt_snapshot) == MAX_PROMPT_SNAPSHOT_CHARS
    assert log.request is not None
    assert log.request.get("prompt_snapshot_truncated") is True


@pytest.mark.asyncio
async def test_logged_provider_prompt_snapshot_no_truncation(async_db):
    """短 prompt 不截断，无 truncated 标记。"""
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _StubProvider(),
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="writer",
        provider_name="stub",
    )
    await provider.generate("short sys", "short usr")

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert "short sys" in log.rendered_prompt_snapshot
    assert "short usr" in log.rendered_prompt_snapshot
    assert log.request is None or log.request.get("prompt_snapshot_truncated") is not True


@pytest.mark.asyncio
async def test_logged_provider_records_request_id(async_db):
    from api.request_id import request_id_var
    from harness.llm_call_logger import LoggedLLMProvider

    token = request_id_var.set("trace-request-789")
    try:
        await LoggedLLMProvider(
            _StubProvider(), run_id=uuid.uuid4(), agent_name="writer", provider_name="stub",
        ).generate("sys", "usr")
    finally:
        request_id_var.reset(token)

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.request["request_id"] == "trace-request-789"


@pytest.mark.asyncio
async def test_parallel_node_calls_use_their_own_step_context(async_db):
    from harness.llm_call_logger import LoggedLLMProvider
    from harness.step_logger import current_step_id_var
    from models.ai_run import AiRun
    from models.ai_run_step import AiRunStep

    run = AiRun(
        project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline", status="RUNNING",
    )
    steps = [
        AiRunStep(run_id=run.id, step_order=1, step_name="critic_parallel", agent_name="critic", status="RUNNING"),
        AiRunStep(run_id=run.id, step_order=2, step_name="consistency_parallel", agent_name="critic", status="RUNNING"),
    ]
    async_db.add(run)
    await async_db.flush()
    for step in steps:
        step.run_id = run.id
    async_db.add_all(steps)
    await async_db.commit()

    async def call(step_id, marker):
        token = current_step_id_var.set(str(step_id))
        try:
            provider = LoggedLLMProvider(
                _StubProvider(),
                run_id=run.id,
                step_id=uuid.uuid4(),  # 必须优先使用当前并行任务的上下文 ID。
                agent_name="critic",
                provider_name="stub",
            )
            await provider.generate("sys", marker)
        finally:
            current_step_id_var.reset(token)

    await asyncio.gather(call(steps[0].id, "parallel-left"), call(steps[1].id, "parallel-right"))
    logs = list((await async_db.execute(select(LlmCallLog).where(LlmCallLog.run_id == run.id))).scalars())
    by_marker = {"parallel-left": None, "parallel-right": None}
    for log in logs:
        for marker in by_marker:
            if marker in log.rendered_prompt_snapshot:
                by_marker[marker] = str(log.step_id)
    assert by_marker == {
        "parallel-left": str(steps[0].id),
        "parallel-right": str(steps[1].id),
    }


@pytest.mark.asyncio
async def test_logger_falls_back_to_running_step_heuristic(async_db, monkeypatch):
    from harness.llm_call_logger import LoggedLLMProvider

    expected_step = uuid.uuid4()
    provider = LoggedLLMProvider(
        _StubProvider(), run_id=uuid.uuid4(), step_id=None, agent_name="critic", provider_name="stub",
    )

    async def resolve():
        return str(expected_step)

    monkeypatch.setattr(provider, "_resolve_running_step_id", resolve)
    await provider.generate("sys", "fallback-call")
    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert str(log.step_id) == str(expected_step)
