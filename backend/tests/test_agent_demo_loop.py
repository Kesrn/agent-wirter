"""Offline loop, budget, recovery and scripted-trajectory tests."""

from __future__ import annotations

import argparse
import json

import pytest

from agents.llm_types import LLMResult, TokenUsage, ToolCall
from agent_demo.budget import Budget, StopReason
from agent_demo.demo import build_demo_store, build_offline_provider
from agent_demo.loop import run_agent
from agent_demo.scripted import ScriptedProvider


def _call(name="read_outline", arguments=None):
    return ToolCall(id=f"{name}-call", name=name, arguments=arguments or {"chapter_no": 1})


def _result(*calls, text="继续", tokens=10, usage=True):
    token_usage = TokenUsage(input_tokens=tokens, output_tokens=0, total_tokens=tokens) if usage else None
    return LLMResult(text=text, tool_calls=list(calls), usage=token_usage)


@pytest.mark.asyncio
async def test_normal_tool_path_returns_final_answer_and_json_serializable_trace():
    provider = ScriptedProvider([
        _result(_call()),
        _result(text="大纲已核对，可以开始写作。", tokens=7),
    ], [])
    result = await run_agent(provider, "读取第一章大纲。", build_demo_store())
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_answer == "大纲已核对，可以开始写作。"
    assert len(result.steps) == 2
    assert json.loads(json.dumps(result.to_dict(), ensure_ascii=False))["stop_reason"] == "final_answer"


@pytest.mark.asyncio
async def test_max_steps_stops_without_an_extra_provider_call():
    provider = ScriptedProvider([_result(_call()) for _ in range(4)], [])
    result = await run_agent(provider, "持续读取大纲。", build_demo_store(), Budget(max_steps=2))
    assert result.stop_reason is StopReason.MAX_STEPS
    assert provider.agent_calls == 2
    assert result.totals["steps"] == 2


@pytest.mark.asyncio
async def test_token_budget_and_missing_usage_estimation():
    over_budget = ScriptedProvider([_result(_call(), tokens=11)], [])
    result = await run_agent(over_budget, "读取大纲。", build_demo_store(), Budget(max_total_tokens=10))
    assert result.stop_reason is StopReason.TOKEN_BUDGET
    assert over_budget.agent_calls == 1

    estimated = ScriptedProvider([_result(text="就绪", tokens=0, usage=False)], [])
    result = await run_agent(estimated, "读取大纲。", build_demo_store(), Budget(max_steps=1))
    assert result.totals["estimated_tokens"] is True
    assert result.totals["total_tokens"] > 0


@pytest.mark.asyncio
async def test_tool_failure_is_observed_then_corrected_and_error_limit_stops():
    provider = ScriptedProvider([
        _result(_call(arguments={"chapter_no": 99})),
        _result(_call(arguments={"chapter_no": 4})),
        _result(text="已读取有效大纲。"),
    ], [])
    result = await run_agent(provider, "读取第四章。", build_demo_store())
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.totals["tool_errors"] == 1
    assert result.steps[0]["tool_calls"][0]["ok"] is False
    assert result.steps[1]["tool_calls"][0]["ok"] is True

    too_many = ScriptedProvider([_result(
        _call(arguments={"chapter_no": 99}), _call(arguments={"chapter_no": 98}),
    )], [])
    result = await run_agent(
        too_many, "读取不存在的大纲。", build_demo_store(), Budget(max_tool_errors=1),
    )
    assert result.stop_reason is StopReason.TOO_MANY_TOOL_ERRORS
    assert too_many.agent_calls == 1


@pytest.mark.asyncio
async def test_arguments_error_and_unknown_tool_are_counted_as_failures_not_exceptions():
    provider = ScriptedProvider([
        _result(
            ToolCall(id="bad-args", name="read_outline", arguments={}, arguments_error="invalid JSON"),
            ToolCall(id="unknown", name="no_such_tool", arguments={}),
        ),
        _result(text="已恢复。"),
    ], [])
    result = await run_agent(provider, "继续。", build_demo_store())
    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.totals["tool_errors"] == 2
    assert all(not item["ok"] for item in result.steps[0]["tool_calls"])


@pytest.mark.asyncio
async def test_repeated_call_guard_stops_before_executing_duplicate():
    provider = ScriptedProvider([_result(_call()), _result(_call()), _result(text="不应运行")], [])
    result = await run_agent(
        provider, "重复读取。", build_demo_store(), Budget(max_repeat_calls=1),
    )
    assert result.stop_reason is StopReason.REPEATED_CALL
    assert provider.agent_calls == 2
    assert result.steps[1]["tool_calls"][0]["ok"] is False


@pytest.mark.asyncio
async def test_scripted_offline_demo_recovers_after_validation_failure():
    result = await run_agent(build_offline_provider(), "执行离线演示。", build_demo_store())
    assert result.stop_reason is StopReason.FINAL_ANSWER
    validator_calls = [
        call
        for step in result.steps
        for call in step["tool_calls"]
        if call["name"] == "run_validators"
    ]
    assert len(validator_calls) == 2
    assert '"passed":false' in validator_calls[0]["summary"]
    assert '"passed":true' in validator_calls[1]["summary"]
    assert result.drafts["d1"] != result.drafts["d2"]


@pytest.mark.asyncio
async def test_real_mode_without_yes_never_constructs_provider(monkeypatch, capsys):
    from scripts import agent_demo as cli

    monkeypatch.setattr(cli, "_get_real_provider", lambda: pytest.fail("provider must not be constructed"))
    args = argparse.Namespace(
        goal="只检查确认门。", real=True, yes=False,
        max_steps=None, max_tokens=None, max_seconds=None,
    )
    assert await cli._run(args) == 2
    output = capsys.readouterr().out
    assert "max_total_tokens" in output
    assert "未执行" in output
