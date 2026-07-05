"""RunManager — AI Run 业务生命周期管理。

纯 async 函数，接 AsyncSession。不管理事务（commit 由调用方负责），
只做对象状态变更与 flush，保证主流程可读 run.id。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run import AiRun
from models.harness_enums import RunStatus

if TYPE_CHECKING:
    from models.expert import Expert
    from services.workflow_definitions import WorkflowDefinition


async def create_run(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    run_type: str,
    mode: str,
    chapter_id: str | uuid.UUID | None = None,
    document_id: str | uuid.UUID | None = None,
    user_goal: str | None = None,
    model_config_snapshot: dict[str, Any] | None = None,
    thread_id: str | None = None,
    # ── Expert System v2: workflow / expert 快照 ──
    workflow_key: str | None = None,
    workflow_version: str | None = None,
    workflow_snapshot: dict[str, Any] | None = None,
    expert_snapshot: list[dict[str, Any]] | None = None,
) -> AiRun:
    run = AiRun(
        project_id=project_id,
        chapter_id=chapter_id,
        document_id=document_id,
        run_type=run_type,
        mode=mode,
        status=RunStatus.CREATED,
        user_goal=user_goal,
        model_config_snapshot=model_config_snapshot,
        thread_id=thread_id,
        workflow_key=workflow_key,
        workflow_version=workflow_version,
        workflow_snapshot=workflow_snapshot,
        expert_snapshot=expert_snapshot,
    )
    db.add(run)
    await db.flush()
    return run


async def mark_running(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_waiting_human(
    db: AsyncSession, run: AiRun, *, step_name: str, thread_id: str | None = None
) -> None:
    run.status = RunStatus.WAITING_HUMAN
    run.current_step = step_name
    if thread_id:
        run.thread_id = thread_id
    await db.flush()


async def mark_completed(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.COMPLETED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_failed(db: AsyncSession, run: AiRun, error_message: str) -> None:
    run.status = RunStatus.FAILED
    run.error_message = error_message
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_cancelled(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


# ── Expert System v2: 快照辅助函数 ────────────────────


def build_expert_snapshot(experts: list[Expert]) -> list[dict[str, Any]]:
    """从 enabled_experts 提取 v2 专家快照（只含 expert_key 非空的）。

    旧 deprecated 专家（expert_key 为空）不进入快照。
    """
    return [
        {
            "expert_key": e.expert_key,
            "version": e.version,
            "skill_dir": e.skill_dir,
            "name": e.name,
        }
        for e in experts
        if e.expert_key
    ]


def workflow_to_snapshot(wf: WorkflowDefinition | None) -> dict[str, Any] | None:
    """WorkflowDefinition → 可序列化 dict，写入 AiRun.workflow_snapshot。"""
    if wf is None:
        return None
    return wf.to_dict()
