"""Expert System v2 的确定性提示词选择。

新版章节工作流必须按稳定的 ``expert_key`` 绑定专家，不能再按 writer/critic
这类宽泛角色取数据库返回的第一条记录。否则旧版、已废弃专家会污染新版节点。
"""

from __future__ import annotations

from typing import Iterable

from models.expert import Expert


V2_PROMPT_KEYS = {
    "writer_prompt": "chapter-writer",
    "critic_prompt": "structural-critic",
    "editor_prompt": "narrative-editor",
    "consistency_prompt": "continuity-checker",
}


def _expert_rank(expert: Expert) -> tuple[int, str, str]:
    """给重复 expert_key 一个稳定选择顺序：高版本、较新记录、UUID。"""
    changed_at = getattr(expert, "updated_at", None) or getattr(expert, "created_at", None)
    return (
        int(getattr(expert, "version", 0) or 0),
        changed_at.isoformat() if changed_at is not None else "",
        str(getattr(expert, "id", "")),
    )


def select_active_v2_experts(experts: Iterable[Expert]) -> dict[str, Expert]:
    """按 expert_key 选择当前启用且未废弃的最高版本专家。"""
    selected: dict[str, Expert] = {}
    for expert in experts:
        expert_key = (getattr(expert, "expert_key", None) or "").strip()
        if (
            not expert_key
            or not bool(getattr(expert, "is_enabled", False))
            or bool(getattr(expert, "deprecated", False))
        ):
            continue
        previous = selected.get(expert_key)
        if previous is None or _expert_rank(expert) > _expert_rank(previous):
            selected[expert_key] = expert
    return selected


def resolve_v2_expert_prompts(experts: Iterable[Expert]) -> dict[str, str]:
    """返回 CreativeStateV2 的四个提示词字段。

    缺少某个专家时返回空字符串，由对应节点使用其内置安全提示词；绝不回退到
    同角色的旧专家，因为旧专家的输出契约可能与新版节点不兼容。
    """
    selected = select_active_v2_experts(experts)
    return {
        state_field: (selected.get(expert_key).system_prompt if selected.get(expert_key) else "")
        for state_field, expert_key in V2_PROMPT_KEYS.items()
    }
