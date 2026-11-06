"""Rule-based planner for choosing direct-route expert skill packs.

This module keeps skill-pack selection decisions out of the API route. It is
intentionally deterministic and narrow: direct generation branches use
mode-specific skill packs for novel and article projects.
"""

from __future__ import annotations

from dataclasses import dataclass


ARTICLE_ROLE_TO_SKILL: dict[str, str] = {
    "writer": "article-copywriter",
    "editor": "article-editor",
    "summarizer": "article-summarizer",
    "critic": "article-editor",
}


@dataclass(frozen=True)
class SkillPackPlan:
    """一次 skill pack 选择决策。

    role_type 决定 prompt 构建时按哪类专家处理；skill_dir 决定读取哪个 Skill.md；
    event_expert 用于 SSE 事件展示；reason 记录为什么选择这套 skill。
    """
    role_type: str
    event_expert: str
    skill_dir: str | None = None
    planner: str = "rule"
    reason: str = ""


def plan_direct_skill_pack(
    *,
    project_mode: str,
    generate_mode: str,
    action: str,
    expert_role_type: str | None = None,
    expert_skill_dir: str | None = None,
    expert_name: str | None = None,
) -> SkillPackPlan | None:
    """Choose a skill pack for non-LangGraph direct generation branches.

    直连分支包括 continue/enhance/summarize 的部分两阶段流程，它们不进入完整
    LangGraph，但仍需要选择合适技能包。这里用确定性规则，避免在 routes.py
    中散落大量 if/else。
    """
    if project_mode == "article":
        if expert_name:
            return SkillPackPlan(
                role_type=expert_role_type or "writer",
                skill_dir=expert_skill_dir or ARTICLE_ROLE_TO_SKILL.get(expert_role_type or "writer"),
                event_expert=expert_name,
                reason="explicit_expert",
            )
        if generate_mode == "enhance":
            return SkillPackPlan(
                role_type="editor",
                skill_dir="article-editor",
                event_expert="article_editor",
                reason=action if action in {"enhance_suggest", "enhance_apply"} else "enhance",
            )
        if generate_mode == "continue":
            if action == "continue_suggest":
                return SkillPackPlan(
                    role_type="writer",
                    skill_dir="article-strategist",
                    event_expert="article_writer",
                    reason="continue_suggest",
                )
            if action == "continue_generate":
                return SkillPackPlan(
                    role_type="writer",
                    skill_dir="article-copywriter",
                    event_expert="article_writer",
                    reason="continue_generate",
                )
        if generate_mode == "summarize":
            return SkillPackPlan(
                role_type="summarizer",
                skill_dir="article-summarizer",
                event_expert="article_reader",
                reason="summarize_feedback",
            )
        if generate_mode == "full_pipeline":
            return SkillPackPlan(
                role_type="writer",
                skill_dir="article-copywriter",
                event_expert="article_writer",
                reason="full_pipeline_writer",
            )
        return None

    if project_mode != "novel":
        return None

    if expert_name:
        return SkillPackPlan(
            role_type=expert_role_type or "writer",
            skill_dir=expert_skill_dir or None,
            event_expert=expert_name,
            reason="explicit_expert",
        )

    if generate_mode == "enhance":
        return SkillPackPlan(
            role_type="editor",
            event_expert="editor",
            reason=action if action in {"enhance_suggest", "enhance_apply"} else "enhance",
        )

    if generate_mode == "continue":
        if action == "continue_suggest":
            return SkillPackPlan(
                role_type="twister",
                event_expert="writer",
                reason="continue_suggest",
            )
        return SkillPackPlan(
            role_type="writer",
            event_expert="writer",
            reason="continue_generate",
        )

    if generate_mode == "summarize":
        return SkillPackPlan(
            role_type="summarizer",
            event_expert="reader",
            reason="summarize_feedback",
        )

    return None


def plan_workflow_skill_pack(
    *,
    node_name: str,
    role_type: str,
    skill_dir: str | None = None,
    expert_name: str | None = None,
) -> SkillPackPlan:
    """Choose a skill pack for LangGraph workflow nodes.

    Workflow nodes are currently only used for novel full-pipeline generation.
    The function exists so direct routes and graph nodes annotate skill-pack
    metadata consistently.

    workflow 节点通常已经知道自己的 role_type 和可选 skill_dir，因此这里主要
    负责生成统一的 SkillPackPlan 元数据。
    """
    return SkillPackPlan(
        role_type=role_type,
        skill_dir=skill_dir or None,
        event_expert=expert_name or role_type,
        reason=f"workflow:{node_name}",
    )
