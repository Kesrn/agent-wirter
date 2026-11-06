"""意图路由严格模型单测（方案 v2.4 决策 5 / 补锁 1）。

覆盖：
- StrictModel extra="forbid"：LLM 多输出 requires_confirmation 等 → 拒绝
- Operation 白名单枚举：未知字符串 → 拒绝
- 字段范围：target_chapter_no ge=1、长度上限
- IntentPlan：依赖越界 / 自依赖 / 成环 → 拒绝；合法链通过
- to_conversation_plan：target 对象化、多前置依赖按 to_step 聚合、plan_id 生成
"""

import pytest
from pydantic import ValidationError

from services.intent_models import (
    Dependency,
    IntentPlan,
    IntentStep,
    LLMIntentOutput,
    Operation,
    to_conversation_plan,
)


def _valid_output_json() -> str:
    return """{
      "intent": "rewrite",
      "operation": "strengthen_character_portrayal",
      "target_scope": "current_chapter",
      "target_chapter_no": 1,
      "mentioned_entities": ["主角"],
      "requirements": ["加强冲突场景中的压迫感", "保持人物关系不变"],
      "plan": {
        "steps": [
          {"intent": "knowledge_query", "raw": "查穆白结局", "target_chapter_no": 10},
          {"intent": "rewrite", "raw": "修改第十章结尾"}
        ],
        "dependencies": [{"from_step": 1, "to_step": 2}]
      }
    }"""


def test_valid_llm_output_parses():
    out = LLMIntentOutput.model_validate_json(_valid_output_json())
    assert out.intent == "rewrite"
    assert out.operation == Operation.STRENGTHEN_CHARACTER_PORTRAYAL
    assert out.mentioned_entities == ["主角"]
    assert out.plan is not None
    assert [s.intent for s in out.plan.steps] == ["knowledge_query", "rewrite"]


def test_extra_field_requires_confirmation_rejected():
    # 补锁 1：LLM 多输出 requires_confirmation → extra="forbid" 拒绝 → 规则回退
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json(
            '{"intent": "rewrite", "requires_confirmation": false}'
        )


def test_unknown_operation_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json('{"intent": "rewrite", "operation": "hack_everything"}')


def test_target_chapter_no_zero_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json('{"intent": "rewrite", "target_chapter_no": 0}')


def test_target_chapter_no_negative_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json('{"intent": "rewrite", "target_chapter_no": -3}')


def test_requirements_too_many_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json(
            '{"intent": "rewrite", "requirements": ["1","2","3","4","5","6","7","8","9","10","11"]}'
        )


def test_requirements_too_long_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json(
            '{"intent": "rewrite", "requirements": ["' + "很" * 300 + '"]}'
        )


def test_clarification_question_too_long_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json(
            '{"intent": "discussion", "clarification_question": "' + "问" * 300 + '"}'
        )


def test_invalid_intent_rejected():
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json('{"intent": "hack"}')
    with pytest.raises(ValidationError):
        LLMIntentOutput.model_validate_json('{"intent": 42}')


# ─── IntentPlan 结构校验 ───


def _plan(steps: int, deps: list[tuple[int, int]]) -> IntentPlan:
    return IntentPlan(
        steps=[IntentStep(intent="rewrite", raw=f"step{i}") for i in range(1, steps + 1)],
        dependencies=[Dependency(from_step=f, to_step=t) for f, t in deps],
    )


def test_plan_dependency_out_of_range_rejected():
    with pytest.raises(ValidationError):
        _plan(1, [(1, 3)])
    with pytest.raises(ValidationError):
        _plan(1, [(2, 1)])


def test_plan_self_dependency_rejected():
    with pytest.raises(ValidationError):
        _plan(2, [(1, 1)])


def test_plan_cycle_rejected():
    with pytest.raises(ValidationError):
        _plan(2, [(1, 2), (2, 1)])
    with pytest.raises(ValidationError):
        _plan(3, [(1, 2), (2, 3), (3, 1)])


def test_plan_valid_chain_ok():
    p = _plan(3, [(1, 2), (2, 3)])
    assert len(p.steps) == 3


# ─── to_conversation_plan 转换（对外契约） ───


def test_to_conversation_plan_target_object():
    p = IntentPlan(
        steps=[
            IntentStep(intent="knowledge_query", raw="查", target_chapter_no=10),
            IntentStep(intent="rewrite", raw="改"),
        ],
        dependencies=[Dependency(from_step=1, to_step=2)],
    )
    cp = to_conversation_plan(p)
    assert cp["plan_id"]
    assert cp["steps"][0]["target"] == {"chapter_no": 10}
    assert cp["steps"][1]["target"] is None
    # knowledge 步骤 query 携带 raw
    assert cp["steps"][0]["query"] == "查"
    assert cp["dependencies"] == [{"step": 2, "depends_on": [1]}]


def test_to_conversation_plan_dependency_aggregation():
    p = IntentPlan(
        steps=[
            IntentStep(intent="knowledge_query", raw="a"),
            IntentStep(intent="knowledge_query", raw="b"),
            IntentStep(intent="rewrite", raw="c"),
        ],
        dependencies=[Dependency(from_step=1, to_step=3), Dependency(from_step=2, to_step=3)],
    )
    cp = to_conversation_plan(p)
    # 多前置依赖按 to_step 聚合为单条
    assert cp["dependencies"] == [{"step": 3, "depends_on": [1, 2]}]


def test_to_conversation_plan_empty_deps():
    p = IntentPlan(steps=[IntentStep(intent="rewrite", raw="改")])
    cp = to_conversation_plan(p)
    assert cp["dependencies"] == []
    assert cp["steps"][0]["intent"] == "rewrite"
