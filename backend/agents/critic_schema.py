"""StructuralCritique Pydantic schema — critic 输出的严格校验。

用于 critic 节点：解析失败时第一次允许 LLM 重新输出纯格式修复版，第二次仍
失败才抛 WorkflowGenerationError，避免 fallback 默认成“没有问题”。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError


class EditInstructions(BaseModel):
    delete: list[str] = Field(default_factory=list)
    merge: list[str] = Field(default_factory=list)
    move_forward: list[str] = Field(default_factory=list)
    move_later: list[str] = Field(default_factory=list)
    rewrite: list[str] = Field(default_factory=list)
    keep: list[str] = Field(default_factory=list)


class StructuralCritique(BaseModel):
    """Critic 输出结构。

    兼容旧 fallback：p0/p1/p2 缺失时默认空列表。requires_revision 缺失时
    默认 False（但评论者会再据此决定是否需要 editor 改）。
    """
    summary: str
    p0: list[str] = Field(default_factory=list)
    p1: list[str] = Field(default_factory=list)
    p2: list[str] = Field(default_factory=list)
    must_keep: list[str] = Field(default_factory=list)
    requires_revision: bool = False
    edit_instructions: EditInstructions = Field(default_factory=EditInstructions)


def _normalized_field_name(name: Any) -> str:
    return "".join(char for char in str(name).lower() if char.isalnum())


def _get_field(mapping: dict, *names: str) -> Any:
    """大小写、下划线不敏感地读取模型字段。"""
    for name in names:
        if name in mapping:
            return mapping[name]
    normalized = {
        _normalized_field_name(key): value
        for key, value in mapping.items()
    }
    for name in names:
        key = _normalized_field_name(name)
        if key in normalized:
            return normalized[key]
    return None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            parts = [
                item.get("location"),
                item.get("dimension") or item.get("category") or item.get("type"),
                item.get("diagnosis") or item.get("problem") or item.get("description") or item.get("issue"),
                item.get("suggestion") or item.get("recommendation") or item.get("instruction"),
            ]
            text = "；".join(str(part).strip() for part in parts if str(part or "").strip())
            if text:
                result.append(text)
    return result


def _issue_priority(issue: dict) -> str:
    severity = str(
        issue.get("severity")
        or issue.get("priority")
        or issue.get("level")
        or ""
    ).strip().lower()
    if severity in {"p0", "critical", "fatal", "error", "high", "严重", "高"}:
        return "p0"
    if severity in {"p1", "warning", "warn", "medium", "中", "警告"}:
        return "p1"
    return "p2"


def normalize_critic_payload(payload: dict | None) -> dict | None:
    """把模型常见的等价结构转换成项目的 StructuralCritique 契约。

    只做字段映射，不替模型重新审稿；转换后仍必须通过 Pydantic 严格校验。
    """
    if not isinstance(payload, dict):
        return None

    nested = _get_field(
        payload,
        "structuralCritique",
        "structural_critique",
        "StructuralCritique",
    )
    source = nested if isinstance(nested, dict) else payload

    summary = (
        _get_field(
            source,
            "summary",
            "critiqueSummary",
            "critique_summary",
            "overallAssessment",
            "overall_assessment",
            "verdict",
        )
    )
    if not isinstance(summary, str) or not summary.strip():
        return payload

    normalized: dict[str, Any] = {
        "summary": summary.strip(),
        "p0": _as_string_list(_get_field(source, "p0")),
        "p1": _as_string_list(_get_field(source, "p1")),
        "p2": _as_string_list(_get_field(source, "p2")),
        "must_keep": _as_string_list(
            _get_field(source, "must_keep", "mustKeep", "strengths")
        ),
    }

    issues = _get_field(
        source,
        "issues",
        "detailedProblems",
        "detailed_problems",
        "problems",
    )
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict):
                normalized[_issue_priority(issue)].extend(_as_string_list(issue))
            elif isinstance(issue, str) and issue.strip():
                normalized["p1"].append(issue.strip())

    raw_instructions = _get_field(source, "edit_instructions", "editInstructions")
    raw_instructions = raw_instructions if isinstance(raw_instructions, dict) else {}
    instructions = {
        "delete": _as_string_list(raw_instructions.get("delete")),
        "merge": _as_string_list(raw_instructions.get("merge")),
        "move_forward": _as_string_list(
            raw_instructions.get("move_forward") or raw_instructions.get("moveForward")
        ),
        "move_later": _as_string_list(
            raw_instructions.get("move_later") or raw_instructions.get("moveLater")
        ),
        "rewrite": _as_string_list(raw_instructions.get("rewrite")),
        "keep": _as_string_list(raw_instructions.get("keep")),
    }
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            suggestion = (
                issue.get("suggestion")
                or issue.get("recommendation")
                or issue.get("instruction")
            )
            instructions["rewrite"].extend(_as_string_list(suggestion))
    execution_note = _get_field(source, "executionNote", "execution_note")
    instructions["rewrite"].extend(_as_string_list(execution_note))
    for action, values in instructions.items():
        instructions[action] = _dedupe(values)
    normalized["edit_instructions"] = instructions

    explicit_requires_revision = _get_field(
        source,
        "requires_revision",
        "requiresRevision",
    )
    if isinstance(explicit_requires_revision, str):
        explicit_requires_revision = explicit_requires_revision.strip().lower() in {
            "true", "1", "yes", "是", "需要",
        }
    normalized["requires_revision"] = (
        bool(explicit_requires_revision)
        if explicit_requires_revision is not None
        else bool(normalized["p0"] or normalized["p1"] or normalized["p2"])
    )
    return normalized


def validate_critic_payload(payload: dict | None) -> StructuralCritique | None:
    """规范化并校验 _parse_json_response 的输出；失败返回 None。"""
    normalized = normalize_critic_payload(payload)
    if not isinstance(normalized, dict):
        return None
    try:
        return StructuralCritique.model_validate(normalized)
    except (ValidationError, TypeError, ValueError):
        return None


def repair_json_user_prompt(raw: str) -> str:
    """第二次调 LLM 用的 user prompt：修复语法并转换为精确字段契约。"""
    return (
        "你上一次输出无法满足 StructuralCritique 的字段契约。不要重新审校，"
        "只把上次结果修复语法并按语义映射到下面的精确 JSON 结构。"
        "不得保留 structuralCritique、issues、verdict 等其他外层字段，"
        "不得输出 Markdown 或解释文字。\n"
        "请诊断并输出 StructuralCritique JSON；本次只做上次结果的结构修复。\n"
        "输出必须精简：每个问题不超过160字，每个优先级最多8项，确保完整闭合 JSON。\n"
        "{\n"
        '  "summary": "总体结论",\n'
        '  "p0": ["必须修复的问题"],\n'
        '  "p1": ["重要问题"],\n'
        '  "p2": ["优化建议"],\n'
        '  "must_keep": ["应保留的优点"],\n'
        '  "requires_revision": true,\n'
        '  "edit_instructions": {\n'
        '    "delete": [], "merge": [], "move_forward": [], "move_later": [],\n'
        '    "rewrite": ["明确的改写指令"], "keep": []\n'
        "  }\n"
        "}\n\n"
        f"上次输出（最多 6000 字）：\n{(raw or '')[:6000]}"
    )
