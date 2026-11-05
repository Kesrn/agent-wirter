"""结构化 LLM 输出的修复、迁移与审计覆盖。"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


class _Payload(BaseModel):
    name: str
    count: int


class _OutputProvider:
    model = "structured-test-model"

    def __init__(self, outputs, *, usage=None):
        self.outputs = list(outputs)
        self.calls = []
        self.usage = usage

    async def complete(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt, kwargs))
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        from agents.llm_types import LLMResult

        return LLMResult(
            text=value,
            usage=self.usage,
            tool_calls=[],
            finish_reason="stop",
            model=self.model,
            retries=0,
            latency_ms=2,
        )


def test_generate_structured_accepts_plain_fenced_control_char_and_surrounded_json():
    from agents.structured import generate_structured

    samples = [
        '{"name":"plain","count":1}',
        '```json\n{"name":"fenced","count":2}\n```',
        '{"name":"line one\nline two","count":3}',
        '结果如下：\n{"name":"surrounded","count":4}\n以上。',
    ]
    expected_names = ["plain", "fenced", "line one\nline two", "surrounded"]

    async def run():
        for sample, expected in zip(samples, expected_names):
            result = await generate_structured(
                _OutputProvider([sample]), _Payload, "system", "user", max_repairs=0
            )
            assert result.value.name == expected
            assert result.repairs == 0
            assert result.json_mode_used is True

    asyncio.run(run())


def test_generate_structured_repairs_missing_fields_and_accumulates_usage():
    from agents.llm_types import TokenUsage
    from agents.structured import generate_structured

    provider = _OutputProvider(
        ['{"name":"first"}', '{"name":"fixed","count":7}'],
        usage=TokenUsage(2, 3, 5),
    )

    result = asyncio.run(
        generate_structured(provider, _Payload, "system", "user", max_repairs=1)
    )

    assert result.value == _Payload(name="fixed", count=7)
    assert result.repairs == 1
    assert result.usage == TokenUsage(4, 6, 10)
    assert "count" in provider.calls[1][1]
    assert "上一次输出" in provider.calls[1][1]


def test_generate_structured_fails_after_repairs_with_field_errors():
    from agents.structured import StructuredOutputError, generate_structured

    provider = _OutputProvider(['{"name":"missing count"}', '{"name":"still missing"}'])

    with pytest.raises(StructuredOutputError) as raised:
        asyncio.run(generate_structured(provider, _Payload, "system", "user"))

    assert raised.value.schema_name == "_Payload"
    assert raised.value.last_errors
    assert any("count" in error for error in raised.value.last_errors)
    assert raised.value.raw_text == '{"name":"still missing"}'
    assert len(provider.calls) == 2


def test_generate_structured_max_repairs_zero_does_not_retry():
    from agents.structured import StructuredOutputError, generate_structured

    provider = _OutputProvider(['{"name":"missing count"}', '{"name":"unused","count":1}'])

    with pytest.raises(StructuredOutputError):
        asyncio.run(generate_structured(provider, _Payload, "system", "user", max_repairs=0))

    assert len(provider.calls) == 1


def test_json_mode_rejection_falls_back_and_is_remembered_per_provider():
    from agents.structured import generate_structured

    class _RejectJsonMode(Exception):
        status_code = 400

        def __str__(self):
            return "response_format JSON mode is not supported"

    provider = _OutputProvider([_RejectJsonMode(), '{"name":"fallback","count":1}', '{"name":"remembered","count":2}'])

    async def run():
        first = await generate_structured(provider, _Payload, "system", "user", max_repairs=0)
        second = await generate_structured(provider, _Payload, "system", "user", max_repairs=0)
        return first, second

    first, second = asyncio.run(run())

    assert first.json_mode_used is False
    assert second.json_mode_used is False
    assert [call[2].get("response_format") for call in provider.calls] == [
        {"type": "json_object"}, None, None,
    ]


def test_extract_json_object_escapes_control_characters_without_changing_text():
    from agents.structured import extract_json_object

    extracted = extract_json_object('```json\n{"name":"a\nb","count":1}\n``` trailing')

    assert extracted is not None
    assert json.loads(extracted) == {"name": "a\nb", "count": 1}


def test_judge_schema_keeps_score_clamping_and_rubric_filtering():
    from services.evaluation import parse_judge_response

    parsed = parse_judge_response(
        '{"overall_score":9,"scores":{"wanted":0,"extra":4},"passed":"unknown","feedback":" "}',
        {"wanted": "目标维度"},
    )

    assert parsed["score"] == 5.0
    assert parsed["scores"] == {"wanted": 1.0}
    assert parsed["passed"] is True
    assert parsed["feedback"] == "评测模型未返回明确反馈。"


def test_critic_failure_keeps_workflow_error_metadata(monkeypatch):
    from agents import workflow_v2

    class _InvalidProvider:
        async def complete(self, system_prompt, user_prompt, **kwargs):
            from agents.llm_types import LLMResult

            return LLMResult("not json", None, [], None, "fake", 0, 0)

    monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: _InvalidProvider())
    monkeypatch.setattr(workflow_v2, "_maybe_wrap_llm", lambda llm, *_args, **_kwargs: llm)
    state = {
        "llm_config": None,
        "context": "",
        "draft": "测试正文",
        "chapter_task_card": {},
    }

    with pytest.raises(workflow_v2.WorkflowGenerationError) as raised:
        asyncio.run(workflow_v2.structural_critic_node(state))

    assert raised.value.step_name == "critique"
    assert raised.value.agent_name == "structural_critic"


def test_malformed_json_samples_old_vs_structured_repair_success_counts(capsys):
    from agents.structured import generate_structured
    from agents.workflow_v2 import _parse_json_response

    samples = json.loads(
        (Path(__file__).parent / "data" / "malformed_json_samples.json").read_text()
    )
    old_successes = sum(bool(_parse_json_response(sample, {})) for sample in samples)

    async def run():
        successes = 0
        for sample in samples:
            provider = _OutputProvider([sample, '{"name":"修复成功","count":1}'])
            try:
                await generate_structured(provider, _Payload, "system", "user", max_repairs=1)
                successes += 1
            except Exception:
                pass
        return successes

    new_successes = asyncio.run(run())
    print(
        f"JSON畸形样本对比：旧解析路径成功 {old_successes}/{len(samples)}；"
        f"新路径（含一次修复）成功 {new_successes}/{len(samples)}"
    )
    assert len(samples) >= 20
    assert new_successes >= old_successes
    assert new_successes == len(samples)


SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest_asyncio.fixture
async def structured_db():
    from db.session import set_engine
    from models.base import Base
    import models  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_engine(engine)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_logged_structured_call_records_final_schema_repair_status(structured_db):
    from agents.llm_types import TokenUsage
    from agents.structured import generate_structured
    from harness.llm_call_logger import LoggedLLMProvider
    from models.llm_call_log import LlmCallLog

    provider = LoggedLLMProvider(
        _OutputProvider(
            ['{"name":"missing count"}', '{"name":"logged","count":3}'],
            usage=TokenUsage(1, 2, 3),
        ),
        run_id=uuid.uuid4(),
        agent_name="structured-test",
        provider_name="fake",
    )

    result = await generate_structured(provider, _Payload, "system", "user")

    logs = list((await structured_db.execute(select(LlmCallLog).order_by(LlmCallLog.created_at))).scalars())
    assert result.value.name == "logged"
    assert len(logs) == 2
    assert all(log.input_tokens == 1 and log.output_tokens == 2 for log in logs)
    assert all(
        log.request["structured"] == {"schema": "_Payload", "repairs": 1, "ok": True}
        for log in logs
    )
