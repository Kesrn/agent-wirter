"""Novel Orchestrator — 确定性规则调度引擎。

把 (project_mode, mode, action) 映射到 WorkflowDefinition。
不调用 LLM，纯确定性。第一版只是查表，不做自由编排。
"""

from __future__ import annotations

import logging

from services.workflow_definitions import WorkflowDefinition, WORKFLOW_DEFINITIONS

logger = logging.getLogger(__name__)


def resolve_workflow(
    *,
    project_mode: str,
    mode: str,
    action: str | None = None,
) -> WorkflowDefinition | None:
    """规则调度：project_mode + mode + action → WorkflowDefinition。

    查表顺序：
    1. 若有 action，先精确匹配 "{project_mode}:{mode}:{action}"
    2. 再 fallback 到 "{project_mode}:{mode}"
    3. 都没命中返回 None（不影响现有流程）

    纯确定性，同一输入始终返回同一结果，不调用 LLM。
    """
    # resume 的 action（approve/review/revise/reject）直接作为 mode 查表
    if action and action in ("approve", "review", "revise", "reject"):
        key = f"{project_mode}:{action}"
        wf = WORKFLOW_DEFINITIONS.get(key)
        if wf:
            return wf

    # 优先精确匹配 mode:action
    if action:
        precise_key = f"{project_mode}:{mode}:{action}"
        wf = WORKFLOW_DEFINITIONS.get(precise_key)
        if wf:
            return wf

    # fallback 到 mode 级别
    key = f"{project_mode}:{mode}"
    wf = WORKFLOW_DEFINITIONS.get(key)
    if wf:
        return wf

    logger.debug("resolve_workflow: no match for project_mode=%s mode=%s action=%s", project_mode, mode, action)
    return None
