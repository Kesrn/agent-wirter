"""RunManager — AI Run 业务生命周期管理。

纯 async 函数，接 AsyncSession。不管理事务（commit 由调用方负责），
只做对象状态变更与 flush，保证主流程可读 run.id。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
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
    """创建一次 AI Run。

    Run 是一次生成/续写/润色任务的顶层记录，用来串联：
    - 当前项目、章节/文档；
    - 用户目标和模型配置快照；
    - workflow/expert 快照；
    - 后续 steps、llm_call_logs、human_interrupts。
    """
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
    """把 run 标记为 RUNNING，并记录首次开始时间。"""
    run.status = RunStatus.RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_waiting_human(
    db: AsyncSession, run: AiRun, *, step_name: str, thread_id: str | None = None
) -> None:
    """把 run 标记为等待人工处理。

    thread_id 会写回 run，resume 接口可据此找到同一次 LangGraph checkpoint。
    """
    run.status = RunStatus.WAITING_HUMAN
    run.current_step = step_name
    if thread_id:
        run.thread_id = thread_id
    await db.flush()


async def mark_completed(db: AsyncSession, run: AiRun) -> None:
    """把 run 标记为完成。"""
    run.status = RunStatus.COMPLETED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_failed(db: AsyncSession, run: AiRun, error_message: str) -> None:
    """把 run 标记为失败，并保存错误信息，便于前端展示和排查。"""
    run.status = RunStatus.FAILED
    run.error_message = error_message
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_cancelled(db: AsyncSession, run: AiRun) -> None:
    """把 run 标记为用户取消。"""
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def discard_run_artifacts(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
) -> None:
    """删除一次未完成生成留下的全部持久化痕迹。

    流式生成必须在节点间提交运行状态，才能让 LLM 审计日志和人工暂停点跨 session
    可见；因此单纯依赖请求事务不足以处理模型异常或客户端断连。失败时按 run_id
    显式删除相关数据，避免候选稿、步骤和提示词快照以“半截任务”的形式留在库中。

    正式章节/文档内容不会由生成阶段写入，只有人工批准后才会保存，故这里不触碰
    正式内容和已批准版本。
    """
    # LlmCallLog 的外键删除策略是 SET NULL，不能只删 AiRun，否则会留下无归属的
    # 提示词快照；GenerationRecord / WritingMemoryStaging 没有 run 外键，也需显式删。
    from models.generation_record import GenerationRecord
    from models.human_interrupt import HumanInterrupt
    from models.llm_call_log import LlmCallLog
    from models.writing_memory_staging import WritingMemoryStaging
    from models.ai_run_step import AiRunStep

    await db.execute(delete(LlmCallLog).where(LlmCallLog.run_id == run_id))
    await db.execute(delete(GenerationRecord).where(GenerationRecord.run_id == run_id))
    await db.execute(delete(WritingMemoryStaging).where(WritingMemoryStaging.run_id == run_id))
    await db.execute(delete(HumanInterrupt).where(HumanInterrupt.run_id == run_id))
    await db.execute(delete(AiRunStep).where(AiRunStep.run_id == run_id))
    await db.execute(delete(AiRun).where(AiRun.id == run_id))
    await db.flush()


async def discard_incomplete_runs(db: AsyncSession) -> list[str]:
    """清理上一个进程崩溃时遗留的 CREATED / RUNNING 任务。

    返回清理到的 LangGraph thread_id，调用方随后删除内存 checkpoint。正常等待人工
    确认的 WAITING_HUMAN 不属于异常中断，不能误删。
    """
    rows = await db.execute(
        select(AiRun.id, AiRun.thread_id).where(
            AiRun.status.in_((RunStatus.CREATED, RunStatus.RUNNING))
        )
    )
    stale_runs = list(rows.all())
    thread_ids = [row.thread_id for row in stale_runs if row.thread_id]
    for row in stale_runs:
        await discard_run_artifacts(db, run_id=row.id)
    return thread_ids


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
