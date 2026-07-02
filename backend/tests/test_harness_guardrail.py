"""Phase G: 结构化 Guardrail 测试"""

import pytest


def test_parse_guardrail_result_valid_json():
    """完整 JSON 应解析为 GuardrailResult dict。"""
    from agents.guardrail import parse_guardrail_result

    raw = '{"issues": [{"type": "character", "description": "角色A性格矛盾", "severity": "high"}], "summary": "发现1个高优先级问题", "overall_severity": "high"}'
    result = parse_guardrail_result(raw)
    assert result["parse_error"] is False
    assert result["overall_severity"] == "high"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "high"
    assert result["summary"] == "发现1个高优先级问题"


def test_parse_guardrail_result_no_issues():
    """无问题的 JSON 应解析为空 issues 列表。"""
    from agents.guardrail import parse_guardrail_result

    raw = '{"issues": [], "summary": "未发现一致性问题", "overall_severity": "info"}'
    result = parse_guardrail_result(raw)
    assert result["parse_error"] is False
    assert result["overall_severity"] == "info"
    assert len(result["issues"]) == 0


def test_parse_guardrail_result_json_in_prose():
    """JSON 嵌在散文中应通过 regex 提取。"""
    from agents.guardrail import parse_guardrail_result

    raw = '以下是检查结果：\n{"issues": [{"type": "worldbuilding", "description": "地理矛盾", "severity": "medium"}], "summary": "1个中等问题", "overall_severity": "medium"}\n以上。'
    result = parse_guardrail_result(raw)
    assert result["parse_error"] is False
    assert result["overall_severity"] == "medium"
    assert len(result["issues"]) == 1


def test_parse_guardrail_result_parse_failure():
    """纯文本（非 JSON）应容错为 parse_error=True。"""
    from agents.guardrail import parse_guardrail_result

    raw = "文本与已知设定基本一致，未发现明显矛盾。"
    result = parse_guardrail_result(raw)
    assert result["parse_error"] is True
    assert result["overall_severity"] == "info"
    assert len(result["issues"]) == 0
    assert result["raw"] == raw


def test_parse_guardrail_result_missing_fields():
    """JSON 缺字段时应填默认值。"""
    from agents.guardrail import parse_guardrail_result

    raw = '{"issues": [{"description": "问题A"}]}'
    result = parse_guardrail_result(raw)
    assert result["parse_error"] is False
    assert result["overall_severity"] == "info"  # 默认
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "info"  # 默认
    assert result["issues"][0]["type"] == "unknown"  # 默认


def test_guardrail_to_text_valid():
    """guardrail_to_text 应把结构化结果转为可读文本摘要。"""
    from agents.guardrail import guardrail_to_text

    result = {
        "issues": [
            {"type": "character", "description": "角色A性格矛盾", "severity": "high"},
            {"type": "worldbuilding", "description": "地理矛盾", "severity": "medium"},
        ],
        "summary": "发现2个问题",
        "overall_severity": "high",
        "parse_error": False,
    }
    text = guardrail_to_text(result)
    assert "发现2个问题" in text
    assert "角色A性格矛盾" in text
    assert "地理矛盾" in text
    assert "high" in text


def test_guardrail_to_text_parse_error():
    """parse_error 时 guardrail_to_text 应返回 raw 文本。"""
    from agents.guardrail import guardrail_to_text

    result = {
        "issues": [],
        "summary": "",
        "overall_severity": "info",
        "parse_error": True,
        "raw": "纯文本报告",
    }
    text = guardrail_to_text(result)
    assert text == "纯文本报告"


def test_guardrail_to_text_empty():
    """空 issues 应返回 summary。"""
    from agents.guardrail import guardrail_to_text

    result = {
        "issues": [],
        "summary": "未发现一致性问题",
        "overall_severity": "info",
        "parse_error": False,
    }
    text = guardrail_to_text(result)
    assert "未发现一致性问题" in text


def test_guardrail_has_blocking_issues():
    """has_blocking_issues 应检测 high severity。"""
    from agents.guardrail import has_blocking_issues

    with_high = {
        "issues": [{"severity": "high", "description": "严重矛盾"}],
        "overall_severity": "high",
        "parse_error": False,
    }
    without_high = {
        "issues": [{"severity": "medium", "description": "中等问题"}],
        "overall_severity": "medium",
        "parse_error": False,
    }
    assert has_blocking_issues(with_high) is True
    assert has_blocking_issues(without_high) is False


# ==================== ConsistencyAgent node 产出 dict ====================

@pytest.mark.asyncio
async def test_consistency_checker_node_returns_dict():
    """consistency_checker_node 应返回 dict 类型的 consistency_report（不再是 str）。"""
    from agents.workflow import consistency_checker_node

    class MockLLM:
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            return '{"issues": [{"type": "character", "description": "角色矛盾", "severity": "high"}], "summary": "发现1个问题", "overall_severity": "high"}'

    state = {
        "context": "角色A是冷酷杀手",
        "draft": "角色A笑着拥抱了所有人",
        "consistency_prompt": "",
        "llm_config": None,
        "skill_packs": [],
    }

    import agents.workflow as wf
    original = wf.get_llm_provider
    wf.get_llm_provider = lambda config: MockLLM()
    try:
        result = await consistency_checker_node(state)
    finally:
        wf.get_llm_provider = original

    assert "consistency_report" in result
    report = result["consistency_report"]
    assert isinstance(report, dict), f"consistency_report should be dict, got {type(report)}"
    assert report["parse_error"] is False
    assert report["overall_severity"] == "high"
    assert len(report["issues"]) == 1


@pytest.mark.asyncio
async def test_consistency_checker_node_parse_failure_returns_dict():
    """LLM 返回纯文本时，consistency_report 仍应是 dict（parse_error=True）。"""
    from agents.workflow import consistency_checker_node

    class MockLLM:
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            return "文本与设定基本一致，未发现矛盾。"

    state = {
        "context": "设定",
        "draft": "草稿",
        "consistency_prompt": "",
        "llm_config": None,
        "skill_packs": [],
    }

    import agents.workflow as wf
    original = wf.get_llm_provider
    wf.get_llm_provider = lambda config: MockLLM()
    try:
        result = await consistency_checker_node(state)
    finally:
        wf.get_llm_provider = original

    report = result["consistency_report"]
    assert isinstance(report, dict)
    assert report["parse_error"] is True
    assert len(report["issues"]) == 0


# ==================== SSE payload 兼容性 ====================

def test_consistency_check_sse_payload_has_both_fields():
    """consistency_check SSE payload 应同时包含 report（文本）和 guardrail_result（结构化）。"""
    from agents.guardrail import parse_guardrail_result, guardrail_to_text

    raw = '{"issues": [{"type": "plot", "description": "剧情断裂", "severity": "high"}], "summary": "1个高优问题", "overall_severity": "high"}'
    guardrail = parse_guardrail_result(raw)
    report_text = guardrail_to_text(guardrail)

    import json
    payload = {"report": report_text, "guardrail_result": guardrail}
    serialized = json.dumps(payload, ensure_ascii=False)
    deserialized = json.loads(serialized)

    assert "report" in deserialized
    assert "guardrail_result" in deserialized
    assert deserialized["guardrail_result"]["overall_severity"] == "high"
    assert "剧情断裂" in deserialized["report"]


# ==================== blocking_issues 提取 ====================

def test_blocking_issues_extraction():
    """从 guardrail_result 提取 HIGH severity issues 作为 blocking_issues。"""
    from agents.guardrail import get_blocking_issues

    guardrail = {
        "issues": [
            {"type": "character", "description": "角色矛盾", "severity": "high"},
            {"type": "plot", "description": "剧情问题", "severity": "medium"},
            {"type": "worldbuilding", "description": "设定矛盾", "severity": "high"},
        ],
        "overall_severity": "high",
        "parse_error": False,
    }
    blocking = get_blocking_issues(guardrail)
    assert len(blocking) == 2
    assert all(b["severity"] == "high" for b in blocking)


def test_blocking_issues_empty():
    """无 HIGH issue 时 blocking_issues 为空。"""
    from agents.guardrail import get_blocking_issues

    guardrail = {
        "issues": [{"type": "plot", "description": "小问题", "severity": "low"}],
        "overall_severity": "low",
        "parse_error": False,
    }
    assert get_blocking_issues(guardrail) == []


def test_blocking_issues_parse_error():
    """parse_error 时 blocking_issues 为空。"""
    from agents.guardrail import get_blocking_issues

    guardrail = {"issues": [], "overall_severity": "info", "parse_error": True, "raw": "文本"}
    assert get_blocking_issues(guardrail) == []
