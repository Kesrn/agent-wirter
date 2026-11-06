"""结构审校输出兼容与 v2 专家绑定的回归测试。"""

from types import SimpleNamespace

from agents.critic_schema import repair_json_user_prompt, validate_critic_payload
from services.expert_prompt_resolver import (
    resolve_v2_expert_prompts,
    select_active_v2_experts,
)


def _expert(
    key: str | None,
    prompt: str,
    *,
    role: str = "critic",
    enabled: bool = True,
    deprecated: bool = False,
    version: int = 1,
):
    return SimpleNamespace(
        id=f"{key}-{version}-{prompt}",
        expert_key=key,
        system_prompt=prompt,
        role_type=role,
        is_enabled=enabled,
        deprecated=deprecated,
        version=version,
        created_at=None,
        updated_at=None,
    )


def test_nested_structural_critique_is_normalized_to_strict_schema():
    payload = {
        "structuralCritique": {
            "chapterTitle": "第一章",
            "verdict": "冲突有效，但人物动机需要补强",
            "issues": [
                {
                    "severity": "high",
                    "dimension": "人物动机",
                    "location": "中段",
                    "diagnosis": "主角突然改变计划",
                    "suggestion": "补一段触发主角改变决定的细节",
                },
                {
                    "severity": "low",
                    "description": "结尾可再收紧",
                },
            ],
            "strengths": ["开场有张力"],
        }
    }

    result = validate_critic_payload(payload)

    assert result is not None
    assert result.summary == "冲突有效，但人物动机需要补强"
    assert "人物动机" in result.p0[0]
    assert result.p2 == ["结尾可再收紧"]
    assert result.must_keep == ["开场有张力"]
    assert result.requires_revision is True
    assert result.edit_instructions.rewrite == ["补一段触发主角改变决定的细节"]


def test_deepseek_capitalized_detailed_problems_are_normalized_locally():
    """复现真实 DeepSeek 返回：合法 JSON 不应再调用模型做第二次格式修复。"""
    payload = {
        "StructuralCritique": {
            "critiqueSummary": "章节完成核心任务，但人物动机与节奏仍需调整。",
            "detailedProblems": [
                {
                    "dimension": "人物动机",
                    "location": "推荐小说前",
                    "severity": "高",
                    "problem": "主角从无聊直接转向阅读，动机链缺失。",
                    "instruction": "补充他观察室友沉浸阅读的细节，再提出要书。",
                },
                {
                    "dimension": "节奏",
                    "location": "开场",
                    "severity": "低",
                    "problem": "环境细节略多。",
                    "instruction": "保留两个核心感官细节，其余压缩。",
                },
            ],
            "executionNote": "优先处理人物动机，再压缩环境描写。",
        }
    }

    result = validate_critic_payload(payload)

    assert result is not None
    assert result.summary == "章节完成核心任务，但人物动机与节奏仍需调整。"
    assert "主角从无聊直接转向阅读" in result.p0[0]
    assert "环境细节略多" in result.p2[0]
    assert result.requires_revision is True
    assert result.edit_instructions.rewrite == [
        "补充他观察室友沉浸阅读的细节，再提出要书。",
        "保留两个核心感官细节，其余压缩。",
        "优先处理人物动机，再压缩环境描写。",
    ]


def test_repair_prompt_requests_exact_field_conversion_and_keeps_longer_source():
    raw = '{"structuralCritique":{"issues":[]}}' + ("甲" * 1500)

    prompt = repair_json_user_prompt(raw)

    assert "不得保留 structuralCritique" in prompt
    assert '"move_forward"' in prompt
    assert "最多 6000 字" in prompt
    assert "甲" * 1200 in prompt


def test_v2_prompt_resolution_ignores_legacy_and_uses_exact_expert_keys():
    experts = [
        _expert(None, "旧版转折大师", role="critic"),
        _expert("structural-critic", "新版结构审校 v1", version=1),
        _expert("structural-critic", "已废弃 v9", deprecated=True, version=9),
        _expert("structural-critic", "新版结构审校 v2", version=2),
        _expert("chapter-writer", "新版写手", role="writer"),
        _expert("narrative-editor", "新版编辑", role="editor"),
        _expert("continuity-checker", "新版一致性", role="critic"),
    ]

    selected = select_active_v2_experts(experts)
    prompts = resolve_v2_expert_prompts(experts)

    assert set(selected) == {
        "structural-critic",
        "chapter-writer",
        "narrative-editor",
        "continuity-checker",
    }
    assert prompts == {
        "writer_prompt": "新版写手",
        "critic_prompt": "新版结构审校 v2",
        "editor_prompt": "新版编辑",
        "consistency_prompt": "新版一致性",
    }


def test_missing_v2_expert_uses_node_default_instead_of_legacy_role_prompt():
    prompts = resolve_v2_expert_prompts([_expert(None, "旧版残酷大师")])

    assert prompts["critic_prompt"] == ""
    assert prompts["consistency_prompt"] == ""
