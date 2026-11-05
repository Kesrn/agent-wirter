"""回归测试：LLM 结构、重试、流式用量与调用审计。"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agents.llm_provider import LLMProvider, MockProvider, OpenAIProvider
from agents.llm_types import LLMResult, TokenUsage, ToolCall, ToolSpec
from config.settings import settings
from harness.llm_call_logger import LoggedLLMProvider
from models.ai_run import AiRun
from models.llm_call_log import LlmCallLog
from models.project import Project


def _completion(
    *,
    content: str = "hello",
    usage: dict | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "unit-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _usage(input_tokens: int = 11, output_tokens: int = 3) -> dict:
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


async def _provider(handler, monkeypatch) -> tuple[OpenAIProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        api_key="test-key-not-real",
        base_url="https://llm.test/v1",
        model="unit-model",
        http_client=client,
    )
    return provider, client


def _tool_call(arguments: str, *, name: str = "lookup") -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_complete_parses_usage_finish_reason_and_tool_call(monkeypatch):
    captured = {}

    def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return _completion(
            content="",
            usage=_usage(11, 3),
            tool_calls=[_tool_call('{"query":"wind"}')],
            finish_reason="tool_calls",
        )

    provider, client = await _provider(handler, monkeypatch)
    try:
        result = await provider.complete(
            "system", "user", tools=[ToolSpec("lookup", "find", {"type": "object"})],
            response_format={"type": "json_object"},
        )
    finally:
        await client.aclose()

    assert result.text == ""
    assert result.usage == TokenUsage(11, 3, 14, estimated=False)
    assert result.finish_reason == "tool_calls"
    assert result.model == "unit-model"
    assert result.retries == 0
    assert result.latency_ms >= 0
    assert result.tool_calls == [ToolCall("call-1", "lookup", {"query": "wind"})]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["tools"] == [{
        "type": "function",
        "function": {"name": "lookup", "description": "find", "parameters": {"type": "object"}},
    }]


@pytest.mark.asyncio
async def test_complete_keeps_invalid_tool_arguments_as_parse_error(monkeypatch):
    def handler(request: httpx.Request):
        return _completion(tool_calls=[_tool_call("{bad-json")], finish_reason="tool_calls")

    provider, client = await _provider(handler, monkeypatch)
    try:
        result = await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert result.tool_calls[0].arguments == {}
    assert result.tool_calls[0].arguments_error


@pytest.mark.asyncio
async def test_base_complete_preserves_legacy_provider_contract():
    class LegacyProvider(LLMProvider):
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            return "legacy text"

        async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            yield "legacy text"

    result = await LegacyProvider().complete("system", "user")
    assert result.text == "legacy text"
    assert result.usage is None
    assert result.tool_calls == []
    assert result.retries == 0


@pytest.mark.asyncio
async def test_mock_complete_estimates_tokens_from_characters():
    class FastMock(MockProvider):
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            return "abcde"

    result = await FastMock().complete("abc", "de")
    assert result.usage == TokenUsage(3, 3, 6, estimated=True)


@pytest.fixture
def patched_settings(request):
    original = {}

    def apply(**values):
        for name, value in values.items():
            if name not in original:
                original[name] = getattr(settings, name)
            object.__setattr__(settings, name, value)

    def restore():
        for name, value in original.items():
            object.__setattr__(settings, name, value)

    request.addfinalizer(restore)
    return apply


def _set_retry_settings(patched_settings, **values):
    for name, value in {
        "LLM_MAX_RETRIES": 2,
        "LLM_RETRY_BASE_SECONDS": 0.2,
        "LLM_RETRY_MAX_SECONDS": 1.0,
        "LLM_TIMEOUT_SECONDS": 120.0,
    }.items():
        values.setdefault(name, value)
    patched_settings(**values)


@pytest.mark.asyncio
async def test_complete_retries_429_and_honors_capped_retry_after(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings, LLM_RETRY_MAX_SECONDS=0.25)
    calls = 0
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("agents.llm_provider.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "5"}, json={"error": {"message": "busy"}})
        return _completion(usage=_usage())

    provider, client = await _provider(handler, monkeypatch)
    try:
        result = await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 2
    assert result.retries == 1
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_complete_retries_5xx_with_jittered_backoff_and_reraises_last_error(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings, LLM_MAX_RETRIES=2, LLM_RETRY_BASE_SECONDS=0.4, LLM_RETRY_MAX_SECONDS=0.5)
    calls = 0
    sleeps = []
    jitter_ranges = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    def fake_uniform(low, high):
        jitter_ranges.append((low, high))
        return 0.5

    monkeypatch.setattr("agents.llm_provider.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("agents.llm_provider.random.uniform", fake_uniform)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider, client = await _provider(handler, monkeypatch)
    try:
        from openai import APIStatusError
        with pytest.raises(APIStatusError):
            await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 3
    assert sleeps == [0.2, 0.25]
    assert jitter_ranges == [(0.5, 1.0), (0.5, 1.0)]


@pytest.mark.asyncio
async def test_complete_does_not_retry_401(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    provider, client = await _provider(handler, monkeypatch)
    try:
        from openai import AuthenticationError
        with pytest.raises(AuthenticationError):
            await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 1


@pytest.mark.parametrize("status", [400, 403, 404, 422])
@pytest.mark.asyncio
async def test_complete_does_not_retry_nonretryable_client_errors(status, monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "client error"}})

    provider, client = await _provider(handler, monkeypatch)
    try:
        from openai import APIStatusError
        with pytest.raises(APIStatusError):
            await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_complete_retries_timeout(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("agents.llm_provider.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return _completion(usage=_usage())

    provider, client = await _provider(handler, monkeypatch)
    try:
        result = await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 2
    assert result.retries == 1


@pytest.mark.asyncio
async def test_complete_retries_connection_error(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("agents.llm_provider.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _completion(usage=_usage())

    provider, client = await _provider(handler, monkeypatch)
    try:
        result = await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 2
    assert result.retries == 1


@pytest.mark.asyncio
async def test_complete_cancellation_is_not_swallowed(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    provider, client = await _provider(handler, monkeypatch)
    try:
        with pytest.raises(asyncio.CancelledError):
            await provider.complete("s", "u")
    finally:
        await client.aclose()
    assert calls == 1


def _sse_response(*events: dict) -> httpx.Response:
    body = b"".join(
        b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
        for event in events
    ) + b"data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def _sse_chunk(delta: dict, *, finish_reason=None, usage=None) -> dict:
    chunk = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "unit-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["choices"] = []
        chunk["usage"] = usage
    return chunk


@pytest.mark.asyncio
async def test_stream_retries_before_first_output_and_sinks_usage(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("agents.llm_provider.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return _sse_response(
            _sse_chunk({"content": "ok"}),
            _sse_chunk({}, finish_reason="stop"),
            _sse_chunk({}, usage=_usage(4, 1)),
        )

    provider, client = await _provider(handler, monkeypatch)
    sink = []
    try:
        chunks = [chunk async for chunk in provider.generate_stream("s", "u", usage_sink=sink)]
    finally:
        await client.aclose()
    assert chunks == ["ok"]
    assert calls == 2
    assert sink == [TokenUsage(4, 1, 5, estimated=False)]


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_output_was_yielded(monkeypatch, patched_settings):
    _set_retry_settings(patched_settings)
    calls = 0
    first = _sse_chunk({"content": "partial"})
    first_line = b"data: " + json.dumps(first, separators=(",", ":")).encode() + b"\n\n"

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first_line
            raise httpx.ReadError("stream broke")

        async def aclose(self):
            return None

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())

    provider, client = await _provider(handler, monkeypatch)
    got = []
    try:
        with pytest.raises(Exception):
            async for chunk in provider.generate_stream("s", "u"):
                got.append(chunk)
    finally:
        await client.aclose()
    assert got == ["partial"]
    assert calls == 1


@pytest.mark.asyncio
async def test_stream_options_unsupported_falls_back_and_is_remembered(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: stream_options"}})
        return _sse_response(_sse_chunk({"content": "ok"}), _sse_chunk({}, finish_reason="stop"))

    provider, client = await _provider(handler, monkeypatch)
    try:
        first = [chunk async for chunk in provider.generate_stream("s", "u")]
        second = [chunk async for chunk in provider.generate_stream("s", "u")]
    finally:
        await client.aclose()
    assert first == second == ["ok"]
    assert ["stream_options" in body for body in seen] == [True, False, False]


class _ResultProvider(LLMProvider):
    model = "priced-model"

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        return "legacy"

    async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        yield "legacy"

    async def complete(self, system_prompt, user_prompt, **kwargs):
        return LLMResult(
            text="result text",
            usage=TokenUsage(1000, 500, 1500),
            tool_calls=[],
            finish_reason="stop",
            model=self.model,
            retries=1,
            latency_ms=7,
        )


@pytest_asyncio.fixture
async def log_db(monkeypatch):
    from db import session as db_session
    from models.ai_run_step import AiRunStep

    engine = create_async_engine("sqlite+aiosqlite://")
    original_engine = db_session.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Project.__table__.create)
        await conn.run_sync(AiRun.__table__.create)
        await conn.run_sync(AiRunStep.__table__.create)
        await conn.run_sync(LlmCallLog.__table__.create)
    db_session.set_engine(engine)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as db:
        db.add(Project(id=project_id, title="log test"))
        run = AiRun(
            id=run_id,
            project_id=project_id,
            run_type="TEST",
            mode="test",
            status="RUNNING",
        )
        db.add(run)
        await db.commit()
        yield db, run
    db_session.set_engine(original_engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_logged_provider_persists_usage_retry_cost_and_run_aggregate(log_db, patched_settings):
    db, run = log_db
    patched_settings(LLM_PRICE_TABLE=json.dumps({
        "priced-model": {"input_per_1k": 0.001, "output_per_1k": 0.002},
    }))
    wrapped = LoggedLLMProvider(_ResultProvider(), run_id=run.id, agent_name="test-agent")
    assert await wrapped.generate("system text", "user text") == "result text"
    logs = list((await db.execute(select(LlmCallLog).where(LlmCallLog.run_id == run.id))).scalars())
    assert len(logs) == 1
    assert (logs[0].input_tokens, logs[0].output_tokens, logs[0].total_tokens) == (1000, 500, 1500)
    assert float(logs[0].cost) == pytest.approx(0.002)
    assert logs[0].latency_ms >= 0
    assert logs[0].request["retries"] == 1
    assert logs[0].request["finish_reason"] == "stop"
    assert logs[0].request["usage_estimated"] is False

    from harness.run_manager import mark_completed
    await mark_completed(db, run)
    assert run.token_usage == {
        "input": 1000, "output": 500, "total": 1500,
        "calls": 1, "retries": 1, "estimated_calls": 0,
    }
    assert run.cost_usage == {"total": pytest.approx(0.002), "currency_unit": "USD"}


@pytest.mark.asyncio
async def test_logged_provider_estimates_usage_and_leaves_unknown_cost_null(log_db, patched_settings):
    db, run = log_db
    patched_settings(LLM_PRICE_TABLE="")

    class NoUsageProvider(_ResultProvider):
        async def complete(self, system_prompt, user_prompt, **kwargs):
            return LLMResult("abcde", None, [], None, self.model, 0, 1)

    wrapped = LoggedLLMProvider(NoUsageProvider(), run_id=run.id)
    assert await wrapped.generate("abc", "de") == "abcde"
    log = (await db.execute(select(LlmCallLog).where(LlmCallLog.run_id == run.id))).scalar_one()
    assert (log.input_tokens, log.output_tokens, log.total_tokens) == (3, 3, 6)
    assert log.cost is None
    assert log.request["usage_estimated"] is True


def test_mock_provider_compatibility_import_path():
    from agents.llm_provider import MockProvider as CompatMockProvider
    assert CompatMockProvider is MockProvider
