from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from langgraph.graph import END, StateGraph


def _clean_draft():
    return "清晨时分，旅人沿着河岸寻找渡口。"


def test_rules_disabled_workflow_node_outputs_match_pre_rules_shape(monkeypatch):
    from agents import workflow_v2

    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(CHAPTER_RULES_ENABLED=False))
    card = {"scenes": [{"must_include": ["星门开启"], "characters": ["林澈"]}]}
    state = {
        "draft": _clean_draft(),
        "target_words": 0,
        "writer_retry_count": 0,
        "writer_max_retries": 1,
        "editor_retry_count": 0,
        "editor_max_retries": 2,
        "writer_draft": _clean_draft(),
        "cycle_writer_draft": _clean_draft(),
        "structural_critique": {},
        "edit_effectiveness_report": {},
        "harness_run_id": "test",
        "chapter_task_card": card,
    }

    cases = [
        (workflow_v2.draft_validator_node, {**state}),
        (workflow_v2.edited_draft_validator_node, {**state}),
        (workflow_v2.final_quality_gate_node, {**state}),
    ]
    for node, node_state in cases:
        baseline_state = dict(node_state)
        baseline_state.pop("chapter_task_card")
        disabled_state = dict(node_state)
        before_rules = asyncio.run(node(baseline_state))
        with_rules_turned_off = asyncio.run(node(disabled_state))
        assert with_rules_turned_off == before_rules


def test_rules_enabled_can_be_overridden_per_state(monkeypatch):
    from agents import workflow_v2

    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(CHAPTER_RULES_ENABLED=True))
    draft = _clean_draft()
    shared = {
        "draft": draft,
        "target_words": 0,
        "chapter_task_card": {"scenes": [{"must_include": ["星门开启"]}]},
        "rules_enabled": False,
    }
    cases = [
        (workflow_v2.draft_validator_node, "draft_quality_report", {}),
        (workflow_v2.edited_draft_validator_node, "edited_quality_report", {
            "writer_draft": draft,
            "cycle_writer_draft": draft,
            "structural_critique": {},
        }),
        (workflow_v2.final_quality_gate_node, "final_quality_report", {"edit_effectiveness_report": {}}),
    ]
    for node, report_key, extra in cases:
        state = {**shared, **extra}
        without_card = dict(state)
        without_card.pop("chapter_task_card")
        without_card.pop("rules_enabled")
        result = asyncio.run(node(state))
        baseline = asyncio.run(node(without_card))
        assert result == baseline
        assert result[report_key]["passed"] is True
        assert not any(issue["code"].startswith("RULE_") for issue in result[report_key]["issues"])


def test_rule_errors_are_merged_into_all_three_quality_gates(monkeypatch):
    from agents import workflow_v2

    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(CHAPTER_RULES_ENABLED=True))
    card = {"scenes": [{"must_include": ["星门开启"]}]}
    draft = _clean_draft()
    states = [
        (workflow_v2.draft_validator_node, {"draft": draft, "target_words": 0}),
        (workflow_v2.edited_draft_validator_node, {
            "draft": draft,
            "writer_draft": draft,
            "cycle_writer_draft": draft,
            "target_words": 0,
            "structural_critique": {},
        }),
        (workflow_v2.final_quality_gate_node, {
            "draft": draft,
            "target_words": 0,
            "edit_effectiveness_report": {},
        }),
    ]
    report_keys = ("draft_quality_report", "edited_quality_report", "final_quality_report")
    for index, (node, state) in enumerate(states):
        result = asyncio.run(node({**state, "chapter_task_card": card}))
        report = result[report_keys[index]]
        assert report["passed"] is False
        assert any(issue["code"] == "RULE_MUST_INCLUDE_MISSING" for issue in report["issues"])


def test_fake_provider_retries_rule_error_and_prompt_contains_message(monkeypatch):
    from agents import workflow_v2

    prompts = []

    class ScriptedProvider:
        def __init__(self):
            self.outputs = [
                "清晨时分，旅人沿着河岸寻找渡口。",
                "清晨时分，旅人见证星门开启，随后沿着河岸寻找渡口。",
            ]

        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            prompts.append(user_prompt)
            return self.outputs.pop(0)

    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(CHAPTER_RULES_ENABLED=True))
    monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: ScriptedProvider())
    # Keep one provider instance across retries, as a real workflow does.
    provider = ScriptedProvider()
    monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: provider)

    graph = StateGraph(workflow_v2.CreativeStateV2)
    graph.add_node("writer", workflow_v2.chapter_writer_node)
    graph.add_node("validator", workflow_v2.draft_validator_node)
    graph.set_entry_point("writer")
    graph.add_edge("writer", "validator")
    graph.add_conditional_edges("validator", workflow_v2.route_after_draft_validator, {
        "chapter_writer": "writer",
        "structural_critic": END,
        END: END,
    })
    result = asyncio.run(graph.compile().ainvoke({
        "target_words": 0,
        "writer_retry_count": 0,
        "writer_max_retries": 1,
        "chapter_task_card": {"scenes": [{"must_include": ["星门开启"]}]},
        "context": "",
    }))

    assert len(prompts) == 2
    assert "必含事件" in prompts[1]
    assert result["draft_quality_report"]["passed"] is True
    assert result["writer_retry_count"] == 1


def test_persistent_rule_error_stops_at_existing_writer_retry_limit(monkeypatch):
    from agents import workflow_v2

    attempts = 0

    class AlwaysInvalidProvider:
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            nonlocal attempts
            attempts += 1
            return _clean_draft()

    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(CHAPTER_RULES_ENABLED=True))
    monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: AlwaysInvalidProvider())
    graph = StateGraph(workflow_v2.CreativeStateV2)
    graph.add_node("writer", workflow_v2.chapter_writer_node)
    graph.add_node("validator", workflow_v2.draft_validator_node)
    graph.set_entry_point("writer")
    graph.add_edge("writer", "validator")
    graph.add_conditional_edges("validator", workflow_v2.route_after_draft_validator, {
        "chapter_writer": "writer",
        "structural_critic": END,
        END: END,
    })
    result = asyncio.run(graph.compile().ainvoke({
        "target_words": 0,
        "writer_retry_count": 0,
        "writer_max_retries": 1,
        "chapter_task_card": {"scenes": [{"must_include": ["星门开启"]}]},
        "context": "",
    }))

    assert attempts == 2
    assert result["writer_retry_count"] == 2
    assert result["quality_gate_failed"] is True
    assert result["quality_failure_stage"] == "draft"


def test_validators_script_writes_summary_and_rules_off_comparison(tmp_path):
    backend_root = Path(__file__).parents[1]
    input_path = tmp_path / "samples.jsonl"
    samples = [
        {
            "id": f"sample-{index}",
            "text": text,
            "task_card": {"scenes": [{"must_include": ["星门开启"]}]},
            "characters": [],
        }
        for index, text in enumerate([
            "清晨，星门开启，旅人走进山谷。",
            "旅人沿着小路寻找渡口。",
            "夜色下星门开启，守卫点亮灯火。",
            "河岸边有一间小屋，窗户透出灯光。",
            "星门开启后，旅人听见远处钟声。",
            "村民把粮食装进木箱，准备出发。",
        ])
    ]
    input_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in samples), encoding="utf-8")
    import subprocess
    import sys

    enabled_dir = tmp_path / "enabled"
    subprocess.run([
        sys.executable, str(backend_root / "scripts" / "run_validators.py"),
        str(input_path), "--output-dir", str(enabled_dir),
    ], check=True, capture_output=True, text=True)
    disabled_dir = tmp_path / "disabled"
    subprocess.run([
        sys.executable, str(backend_root / "scripts" / "run_validators.py"),
        str(input_path), "--output-dir", str(disabled_dir), "--rules-off",
    ], check=True, capture_output=True, text=True)

    enabled = json.loads((enabled_dir / "summary.json").read_text())
    disabled = json.loads((disabled_dir / "summary.json").read_text())
    assert enabled["sample_count"] == 6
    assert "RULE_MUST_INCLUDE_MISSING" in enabled["checks"]
    assert enabled["checks"]["RULE_MUST_INCLUDE_MISSING"]["pass_rate"] < 1
    assert disabled["rules_enabled"] is False
    assert disabled["zero_error_pass_rate"] == 1
    assert (enabled_dir / "report.md").exists()
