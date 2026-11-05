from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _FakeProvider:
    def __init__(self):
        self.prompts = []

    async def generate(self, system_prompt, user_prompt, **kwargs):
        self.prompts.append((system_prompt, user_prompt))
        return "测试正文足够完整。"


def _state(branch="first"):
    state = {
        "context": "测试上下文",
        "target_words": 100,
        "chapter_task_card": {},
        "revision_count": 0,
        "writer_retry_count": 0,
    }
    if branch == "retry":
        state.update({
            "writer_retry_count": 1,
            "writer_validation_errors": [{"message": "缺少必要转折"}],
        })
    elif branch == "revision":
        state.update({
            "revision_count": 1,
            "draft": "这是待修改的候选正文。",
            "critiques": ["补足人物动机"],
        })
    return state


def _expected_old_prompt(branch):
    if branch == "revision":
        prompt = (
            "## 上下文\n测试上下文\n\n"
            "## 修改方向\n补足人物动机\n\n"
            "## 当前候选稿（只能修改这份稿件，不得续写）\n这是待修改的候选正文。\n\n"
            "\n\n"
            "请输出完整修改后的章节正文。不要输出说明、标题或修改清单。"
        )
    elif branch == "retry":
        feedback = (
            "上一次输出未通过系统验收：\n"
            "1. 缺少必要转折\n"
            "\n目标字数约 100 字，请严格控制篇幅。\n"
            "禁止输出场景标题、增强标签、写作说明和修改说明。\n"
            "请重新输出完整纯正文，不要解释。"
        )
        prompt = (
            "## 章节任务卡\n\n\n"
            "## 上下文\n测试上下文\n\n"
            f"\n\n## 重写要求\n{feedback}\n\n"
            "请根据章节任务卡和上下文生成本章完整正文。\n"
            "硬性要求：\n"
            "1. 严格遵循任务卡的场景划分和信息揭示边界，不得偏离本章大纲。\n"
            "2. 任务卡和本章大纲是最高优先级，执行重点和用户补充仅作参考，不得引入大纲之外的关键事件、人物或设定。\n"
            "3. 不修改任务卡，不新增任务卡之外的长期设定。\n"
            "4. 只输出章节正文，不要输出说明、标题或项目符号。\n"
            "5. 禁止在正文内部署【增强开始/结束】等规划标记、场景标题和修改说明。"
        )
    else:
        prompt = (
            "## 章节任务卡\n\n\n"
            "## 上下文\n测试上下文\n\n"
            "请根据章节任务卡和上下文生成本章完整正文。\n"
            "硬性要求：\n"
            "1. 严格遵循任务卡的场景划分和信息揭示边界，不得偏离本章大纲。\n"
            "2. 任务卡和本章大纲是最高优先级，执行重点和用户补充仅作参考，不得引入大纲之外的关键事件、人物或设定。\n"
            "3. 不修改任务卡，不新增任务卡之外的长期设定。\n"
            "4. 只输出章节正文，不要输出说明、标题或项目符号。\n"
            "5. 禁止在正文内部署【增强开始/结束】等规划标记、场景标题和修改说明。"
        )
    return prompt + "\n\n**目标字数：约100字，请控制篇幅，并以完整句子自然结束。**"


def _patch_writer(monkeypatch, provider):
    from agents import workflow_v2

    class _Pack:
        has_content = False
        warnings = []

    monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(workflow_v2, "_maybe_wrap_llm", lambda llm, *_args, **_kwargs: llm)
    monkeypatch.setattr(workflow_v2, "_build_workflow_skill_pack", lambda *_args, **_kwargs: (_Pack(), {}))
    monkeypatch.setattr(workflow_v2, "build_expert_system_prompt", lambda *_args, **_kwargs: "system")
    return workflow_v2


def test_fewshot_global_setting_defaults_closed():
    from config.settings import settings

    assert settings.FEWSHOT_ENABLED is False


@pytest.mark.parametrize("branch", ["first", "retry", "revision"])
def test_fewshot_disabled_preserves_writer_prompt_byte_for_byte(monkeypatch, branch):
    provider = _FakeProvider()
    workflow_v2 = _patch_writer(monkeypatch, provider)
    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(FEWSHOT_ENABLED=False))

    asyncio.run(workflow_v2.chapter_writer_node(_state(branch)))

    assert provider.prompts[0][1] == _expected_old_prompt(branch)


def test_fewshot_state_overrides_global_and_records_example_ids(monkeypatch):
    from agents import workflow_v2

    provider = _FakeProvider()
    workflow_v2 = _patch_writer(monkeypatch, provider)
    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(FEWSHOT_ENABLED=False))

    async def examples(_state):
        return "【过往人工修改示例】\n示例1\n修改前：测试旧句\n修改后：测试新句", ["record-123"]

    monkeypatch.setattr(workflow_v2, "_load_writer_edit_examples", examples, raising=False)
    state = _state()
    state["fewshot_enabled"] = True

    result = asyncio.run(workflow_v2.chapter_writer_node(state))

    prompt = provider.prompts[0][1]
    assert "【过往人工修改示例】" in prompt
    assert prompt.index("## 上下文") < prompt.index("【过往人工修改示例】")
    assert result["fewshot_example_ids"] == ["record-123"]


def test_fewshot_state_false_overrides_enabled_global(monkeypatch):
    from agents import workflow_v2

    provider = _FakeProvider()
    workflow_v2 = _patch_writer(monkeypatch, provider)
    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(FEWSHOT_ENABLED=True))
    called = False

    async def examples(_state):
        nonlocal called
        called = True
        return "示例块", ["record-123"]

    monkeypatch.setattr(workflow_v2, "_load_writer_edit_examples", examples, raising=False)
    state = _state()
    state["fewshot_enabled"] = False

    result = asyncio.run(workflow_v2.chapter_writer_node(state))

    assert called is False
    assert provider.prompts[0][1] == _expected_old_prompt("first")
    assert result["fewshot_example_ids"] == []


def test_fewshot_query_failure_does_not_fail_generation(monkeypatch):
    from agents import workflow_v2

    provider = _FakeProvider()
    workflow_v2 = _patch_writer(monkeypatch, provider)
    monkeypatch.setattr(workflow_v2, "settings", SimpleNamespace(FEWSHOT_ENABLED=True))

    async def broken(_state):
        raise RuntimeError("temporary query failure")

    monkeypatch.setattr(workflow_v2, "_load_writer_edit_examples", broken, raising=False)

    result = asyncio.run(workflow_v2.chapter_writer_node(_state()))

    assert result["draft"] == "测试正文足够完整。"
    assert result["fewshot_example_ids"] == []


def test_step_input_can_record_fewshot_example_ids():
    from harness.step_logger import record_fewshot_example_ids

    class Step:
        input = {"existing": "snapshot"}

    step = Step()
    record_fewshot_example_ids(step, ["a", "b"])
    assert step.input == {"existing": "snapshot", "fewshot_example_ids": ["a", "b"]}
