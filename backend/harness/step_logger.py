"""StepLogger — AiRunStep 步骤记录。

纯 async 函数，接 AsyncSession。幂等：start_step 对同一 run+step_name+revision_round
返回既有 step（基于唯一索引），不重复创建。并发安全：critic 与 consistency_checker
step_name 不同 → 幂等键不同 → 可并存。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run_step import AiRunStep
from models.harness_enums import RunStepStatus


def _idempotency_key(run_id: str | uuid.UUID, step_name: str, revision_round: int) -> str:
    return f"{run_id}:{step_name}:{revision_round}"


async def start_step(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    step_order: int,
    step_name: str,
    agent_name: str | None = None,
    revision_round: int = 0,
    input_data: dict[str, Any] | None = None,
    max_retry: int = 0,
) -> AiRunStep:
    key = _idempotency_key(run_id, step_name, revision_round)
    # 幂等：若已存在同 key 的 step，直接返回
    existing = await db.execute(
        select(AiRunStep).where(AiRunStep.idempotency_key == key)
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    step = AiRunStep(
        run_id=run_id,
        step_order=step_order,
        step_name=step_name,
        agent_name=agent_name,
        status=RunStepStatus.RUNNING,
        input=input_data,
        max_retry=max_retry,
        idempotency_key=key,
        started_at=datetime.now(timezone.utc),
    )
    db.add(step)
    await db.flush()
    return step


async def finish_step(
    db: AsyncSession, step: AiRunStep, *, output: dict[str, Any] | None = None
) -> None:
    step.status = RunStepStatus.SUCCESS
    step.output = output
    step.ended_at = datetime.now(timezone.utc)
    await db.flush()


async def fail_step(
    db: AsyncSession, step: AiRunStep, *, error_message: str
) -> None:
    step.status = RunStepStatus.FAILED
    step.error_message = error_message
    step.ended_at = datetime.now(timezone.utc)
    await db.flush()


async def wait_human_step(
    db: AsyncSession, step: AiRunStep, *, payload: dict[str, Any] | None = None
) -> None:
    step.status = RunStepStatus.WAITING_HUMAN
    step.output = payload
    await db.flush()
