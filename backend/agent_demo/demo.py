"""Fixture and pre-scripted trajectory for the offline walkthrough."""

from __future__ import annotations

import json
from pathlib import Path

from agents.llm_types import LLMResult, ToolCall, TokenUsage

from .store import InMemoryProjectStore
from .scripted import ScriptedProvider


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "demo_project.json"


def build_demo_store() -> InMemoryProjectStore:
    return InMemoryProjectStore(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def build_offline_provider() -> ScriptedProvider:
    def action(name: str, arguments: dict) -> LLMResult:
        return LLMResult(
            text=f"我将调用 {name}。",
            usage=TokenUsage(input_tokens=80, output_tokens=20, total_tokens=100),
            tool_calls=[ToolCall(id=f"call-{name}", name=name, arguments=arguments)],
            model="offline-script",
        )

    final = LLMResult(
        text="第4章场景已完成：林晚在灯塔读第三封信，保留寄信人身份悬念，并通过规则校验。",
        usage=TokenUsage(input_tokens=90, output_tokens=32, total_tokens=122),
        model="offline-script",
        finish_reason="stop",
    )
    bad_draft = "林晚握着灯塔的钥匙，听见潮声。信末写着：寄信人是顾舟。她把第三封信折好。"
    clean_draft = "林晚握着灯塔的钥匙，听见潮声一遍遍漫过礁石。她读完第三封信，把落款留在灯影之外。"
    return ScriptedProvider(
        agent_results=[
            action("read_outline", {"chapter_no": 4}),
            action("get_character", {"name": "林晚"}),
            action("search_chapters", {"query": "钥匙", "top_k": 3}),
            action("write_scene", {
                "scene_brief": "第4章第2场：林晚在灯塔读第三封信，必须提到钥匙与潮声。",
                "character_names": ["林晚"],
                "target_chars": 600,
                "must_include": ["钥匙", "潮声"],
            }),
            action("run_validators", {
                "draft_id": "d1",
                "must_include": ["钥匙", "潮声"],
                "must_not_include": ["顾舟"],
            }),
            action("write_scene", {
                "scene_brief": "改写场景，保留钥匙与潮声，不揭示寄信人身份。",
                "character_names": ["林晚"],
                "target_chars": 600,
                "must_include": ["钥匙", "潮声"],
            }),
            action("run_validators", {
                "draft_id": "d2",
                "must_include": ["钥匙", "潮声"],
                "must_not_include": ["顾舟"],
            }),
            final,
        ],
        scene_texts=[bad_draft, clean_draft],
    )
