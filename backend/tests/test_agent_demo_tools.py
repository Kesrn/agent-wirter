"""Offline tool-level tests for the standalone agent demo."""

from __future__ import annotations

import json

import pytest

from agents.llm_types import LLMResult, TokenUsage
from agent_demo.demo import build_demo_store
from agent_demo.scripted import ScriptedProvider
from agent_demo.tools import ToolRegistry, ToolResult, ToolRuntime


@pytest.mark.asyncio
async def test_read_outline_and_character_lookup_validate_inputs():
    registry = ToolRegistry(ToolRuntime(build_demo_store(), ScriptedProvider([], [])))

    outline = await registry.execute("read_outline", {"chapter_no": 4})
    assert outline.ok
    assert "不得揭示寄信人身份" in outline.observation()
    missing = await registry.execute("read_outline", {"chapter_no": 99})
    assert not missing.ok

    character = await registry.execute("get_character", {"name": "晚晚"})
    assert character.ok
    assert "林晚" in character.observation()
    unknown = await registry.execute("get_character", {"name": "不存在的人"})
    assert not unknown.ok
    assert "林晚" in unknown.observation()


@pytest.mark.asyncio
async def test_search_chapters_finds_continuity_clue_and_caps_snippet():
    registry = ToolRegistry(ToolRuntime(build_demo_store(), ScriptedProvider([], [])))
    found = await registry.execute("search_chapters", {"query": "灯塔的钥匙", "top_k": 2})
    assert found.ok
    data = json.loads(found.observation())["data"]
    assert data
    assert data[0]["chapter_no"] == 2
    assert len(data[0]["snippet"]) <= 120
    empty = await registry.execute("search_chapters", {"query": "完全不存在的词"})
    assert json.loads(empty.observation())["data"] == []
    invalid = await registry.execute("search_chapters", {"query": "钥匙", "top_k": 6})
    assert not invalid.ok


@pytest.mark.asyncio
async def test_write_scene_stores_full_draft_and_returns_only_id_preview_and_counts_usage():
    scene = "林晚握着钥匙，听见潮声。" * 10
    provider = ScriptedProvider([], [scene])
    usage = []

    def record(result, system, user):
        usage.append((result.text, system, user))
        return result.usage.total_tokens if result.usage else 0

    runtime = ToolRuntime(build_demo_store(), provider, record_usage=record)
    registry = ToolRegistry(runtime)
    result = await registry.execute("write_scene", {
        "scene_brief": "林晚在灯塔读信。",
        "character_names": ["林晚"],
        "target_chars": 200,
        "must_include": ["钥匙", "潮声"],
    })
    assert result.ok
    output = json.loads(result.observation())["data"]
    assert output["draft_id"] == "d1"
    assert output["preview"] == scene[:80]
    assert scene not in result.observation()
    assert runtime.drafts["d1"] == scene
    assert provider.scene_calls == 1
    assert len(usage) == 1


@pytest.mark.asyncio
async def test_validators_report_forbidden_content_and_missing_draft():
    runtime = ToolRuntime(build_demo_store(), ScriptedProvider([], []), drafts={"d1": "林晚读到寄信人是顾舟，钥匙落在桌上。"})
    registry = ToolRegistry(runtime)
    result = await registry.execute("run_validators", {
        "draft_id": "d1",
        "must_include": ["钥匙"],
        "must_not_include": ["顾舟"],
    })
    assert result.ok
    details = json.loads(result.observation())["data"]
    assert details["passed"] is False
    assert any(issue["severity"] == "ERROR" for issue in details["issues"])
    assert not (await registry.execute("run_validators", {"draft_id": "missing"})).ok


@pytest.mark.asyncio
async def test_tool_observation_is_truncated_with_marker():
    result = ToolResult(True, data={"text": "长" * 200})
    rendered = result.observation(60)
    assert len(rendered) == 60
    assert rendered.endswith("…[已截断]")
