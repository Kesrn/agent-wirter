"""Rule-based planner for choosing direct-route expert skill packs.

This module keeps skill-pack selection decisions out of the API route. It is
intentionally deterministic and narrow: current direct generation branches only
inject skill packs for novel projects, while article projects keep their
domain-specific prompts until article skill strategy is defined.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillPackPlan:
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
    """Choose a skill pack for non-LangGraph direct generation branches."""
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
    """
    return SkillPackPlan(
        role_type=role_type,
        skill_dir=skill_dir or None,
        event_expert=expert_name or role_type,
        reason=f"workflow:{node_name}",
    )
