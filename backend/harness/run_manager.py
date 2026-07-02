"""RunManager — AI Run 业务生命周期管理。

纯 async 函数，接 AsyncSession。不管理事务（commit 由调用方负责），
只做对象状态变更与 flush，保证主流程可读 run.id。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run import AiRun
from models.harness_enums import RunStatus


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
