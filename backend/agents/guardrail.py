"""结构化 Guardrail — 一致性检查结果的结构化解析。

GuardrailResult schema:
  {
    "issues": [GuardrailIssue],
    "summary": str,
    "overall_severity": "info" | "low" | "medium" | "high",
    "parse_error": bool,
    "raw": str | None  # 仅 parse_error=True 时有值
  }

GuardrailIssue:
  {
    "type": str,        # "character" | "worldbuilding" | "plot" | "timeline" | "other" | "unknown"
    "description": str,
    "severity": "info" | "low" | "medium" | "high"
  }
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VALID_SEVERITIES = {"info", "low", "medium", "high"}
VALID_ISSUE_TYPES = {"character", "worldbuilding", "plot", "timeline", "other"}


def _coerce_severity(value: Any) -> str:
    if isinstance(value, str) and value.lower() in VALID_SEVERITIES:
        return value.lower()
    return "info"


def _coerce_issue_type(value: Any) -> str:
    if isinstance(value, str) and value.lower() in VALID_ISSUE_TYPES:
        return value.lower()
    return "unknown"


def _coerce_issues(raw_issues: Any) -> list[dict[str, str]]:
    if not isinstance(raw_issues, list):
        return []
    issues: list[dict[str, str]] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        description = item.get("description", "")
        if not isinstance(description, str) or not description.strip():
            continue
        issues.append({
            "type": _coerce_issue_type(item.get("type")),
            "description": description.strip(),
            "severity": _coerce_severity(item.get("severity")),
        })
    return issues


def _derive_overall_severity(issues: list[dict[str, str]], parsed_severity: Any) -> str:
    """从 issues 中推断最高 severity，fallback 到 parsed_severity。"""
    if issues:
        for sev in ("high", "medium", "low", "info"):
            if any(i["severity"] == sev for i in issues):
                return sev
    return _coerce_severity(parsed_severity)


def parse_guardrail_result(raw: str) -> dict[str, Any]:
    """容错解析 ConsistencyAgent 的 LLM 输出为 GuardrailResult dict。

    解析策略（与 article_review._parse_review_json 一致）：
    1. regex 提取 {...}
    2. json.loads
    3. 失败时返回 {"parse_error": True, "raw": <原文>, "issues": [], ...}

    Args:
        raw: LLM 返回的原始文本

    Returns:
        GuardrailResult dict（永远非 None，永远有 issues/summary/overall_severity/parse_error）
    """
    raw = raw or ""
    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                issues = _coerce_issues(parsed.get("issues"))
                summary = parsed.get("summary", "")
                if not isinstance(summary, str):
                    summary = str(summary) if summary else ""
                overall = _derive_overall_severity(issues, parsed.get("overall_severity"))
                return {
                    "issues": issues,
                    "summary": summary,
                    "overall_severity": overall,
                    "parse_error": False,
                }
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("guardrail: LLM output JSON parse failed: %s", e)

    return {
        "issues": [],
        "summary": "",
        "overall_severity": "info",
        "parse_error": True,
        "raw": raw.strip(),
    }


def guardrail_to_text(result: dict[str, Any]) -> str:
    """把 GuardrailResult dict 转为可读文本摘要（用于向后兼容的 report 字段）。

    Args:
        result: parse_guardrail_result 返回的 dict

    Returns:
        可读文本字符串
    """
    if result.get("parse_error"):
        return result.get("raw", "")

    summary = result.get("summary", "")
    issues = result.get("issues", [])
    overall = result.get("overall_severity", "info")

    lines: list[str] = []
    if summary:
        lines.append(summary)
    if issues:
        lines.append(f"（严重度: {overall}）")
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. [{issue.get('severity', 'info')}] {issue.get('type', 'unknown')}: {issue.get('description', '')}")

    return "\n".join(lines) if lines else "未发现一致性问题"


def has_blocking_issues(result: dict[str, Any]) -> bool:
    """判断 GuardrailResult 是否包含 HIGH severity 问题（应阻断自动提交）。

    Args:
        result: parse_guardrail_result 返回的 dict

    Returns:
        True 如果有 high severity issue
    """
    if result.get("parse_error"):
        return False
    return result.get("overall_severity") == "high"


def get_blocking_issues(result: dict[str, Any]) -> list[dict[str, str]]:
    """提取 GuardrailResult 中所有 HIGH severity 的 issues。

    用于在 human interrupt payload 中标记 blocking reason。

    Args:
        result: parse_guardrail_result 返回的 dict

    Returns:
        HIGH severity issues 列表（可能为空）
    """
    if result.get("parse_error"):
        return []
    issues = result.get("issues", [])
    if not isinstance(issues, list):
        return []
    return [i for i in issues if isinstance(i, dict) and i.get("severity") == "high"]
