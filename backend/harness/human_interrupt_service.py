"""Human Interrupt 服务层 — 持久化人工审核等待状态。"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run import AiRun
from models.ai_run_step import AiRunStep
from models.harness_enums import InterruptDecision, InterruptStatus
from models.human_interrupt import HumanInterrupt


async def create_interrupt(
    db: AsyncSession,
    *,
    run: AiRun,
    step_id: str | None = None,
    thread_id: str | None = None,
    step_name: str | None = None,
    payload: dict | None = None,
) -> HumanInterrupt:
    """创建一个新的 human interrupt 记录。

    Args:
        db: 数据库会话
        run: 关联的 AI Run
        step_id: 关联的 step ID（可选）
        thread_id: LangGraph thread_id（可选）
        step_name: 步骤名称（可选）
        payload: 额外的上下文数据（可选）

    Returns:
        创建的 HumanInterrupt 对象
    """
    interrupt = HumanInterrupt(
        run_id=run.id,
        step_id=step_id,
        thread_id=thread_id,
        step_name=step_name,
        status=InterruptStatus.WAITING,
        payload=payload or {},
        resolved=False,
    )
    db.add(interrupt)
    await db.flush()
    return interrupt


async def resolve_interrupt(
    db: AsyncSession,
    interrupt: HumanInterrupt,
    *,
    decision: InterruptDecision,
    feedback: str | None = None,
) -> None:
    """解析一个 interrupt，标记为已处理。

    Args:
        db: 数据库会话
        interrupt: 要解析的 interrupt
        decision: 用户决策
        feedback: 用户反馈（可选）
    """
    if interrupt.resolved:
        # 幂等：已解析的 interrupt 不再修改
        return

    # 映射决策到状态
    decision_to_status = {
        InterruptDecision.APPROVE: InterruptStatus.APPROVED,
        InterruptDecision.REJECT: InterruptStatus.REJECTED,
        InterruptDecision.EDIT: InterruptStatus.EDITED,
        InterruptDecision.REGENERATE: InterruptStatus.REGENERATE,
        InterruptDecision.SUBMIT_CLARIFICATION: InterruptStatus.ANSWERED,
        InterruptDecision.SKIP_CLARIFICATION: InterruptStatus.SKIPPED,
    }

    interrupt.status = decision_to_status.get(decision, InterruptStatus.WAITING)
    interrupt.decision = decision
    interrupt.feedback = feedback
    interrupt.resolved = True
    interrupt.resolved_at = datetime.now(timezone.utc)
    await db.flush()


async def get_interrupt_by_run(
    db: AsyncSession,
    run_id: str,
) -> HumanInterrupt | None:
    """根据 run_id 查找最新的 interrupt。

    Args:
        db: 数据库会话
        run_id: Run ID

    Returns:
        最新的 HumanInterrupt 或 None
    """
    result = await db.execute(
        select(HumanInterrupt)
        .where(HumanInterrupt.run_id == run_id)
        .order_by(HumanInterrupt.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_interrupt_by_thread(
    db: AsyncSession,
    thread_id: str,
) -> HumanInterrupt | None:
    """根据 thread_id 查找最新的 interrupt。

    Args:
        db: 数据库会话
        thread_id: LangGraph thread ID

    Returns:
        最新的 HumanInterrupt 或 None
    """
    result = await db.execute(
        select(HumanInterrupt)
        .where(HumanInterrupt.thread_id == thread_id)
        .order_by(HumanInterrupt.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def check_interrupt_resolved(
    db: AsyncSession,
    run_id: str,
) -> bool:
    """检查指定 run 的最新 interrupt 是否已解析。

    用于幂等性检查：防止重复 approve/reject。

    Args:
        db: 数据库会话
        run_id: Run ID

    Returns:
        True 如果最新 interrupt 已解析，False 否则
    """
    interrupt = await get_interrupt_by_run(db, run_id)
    return interrupt.resolved if interrupt else False
