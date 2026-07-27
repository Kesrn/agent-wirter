"""Human Interrupt 服务层 — 持久化人工审核等待状态。

LangGraph 的 MemorySaver 能保存运行时 state，但它是内存级 checkpoint。
HumanInterrupt 表额外记录“这次 run 正在等用户做什么决定”，用于前端展示、
幂等保护和审计。
"""

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
    # 同一 run 在同一个暂停点重新规划时（例如 L-2 回答澄清后重出任务卡），
    # 复用仍在等待的 interrupt，避免前端和审计表积累多个“待处理”任务卡。
    if thread_id and step_name:
        existing = await db.execute(
            select(HumanInterrupt)
            .where(
                HumanInterrupt.run_id == run.id,
                HumanInterrupt.thread_id == thread_id,
                HumanInterrupt.step_name == step_name,
                HumanInterrupt.resolved == False,  # noqa: E712
            )
            .order_by(HumanInterrupt.created_at.desc())
            .limit(1)
        )
        interrupt = existing.scalar_one_or_none()
        if interrupt:
            interrupt.payload = payload or {}
            await db.flush()
            return interrupt

    # payload 保存暂停点需要给前端展示的信息，例如任务卡、澄清问题、候选正文摘要等。
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
        # 幂等：已解析的 interrupt 不再修改。
        # 防止用户重复点击批准/拒绝造成重复版本或状态回退。
        return

    # 映射决策到状态。decision 记录用户动作，status 便于列表/筛选展示。
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


async def get_unresolved_interrupt_by_thread_step(
    db: AsyncSession,
    thread_id: str,
    step_name: str,
) -> HumanInterrupt | None:
    """查找指定线程和暂停点仍在等待的 interrupt。

    任务卡刷新会在同一个逻辑审核点重复规划，因此不能用“线程最新记录”
    代替精确匹配。按 step_name 限定还能避免误解析旧的澄清或人工终审记录。
    """
    result = await db.execute(
        select(HumanInterrupt)
        .where(
            HumanInterrupt.thread_id == thread_id,
            HumanInterrupt.step_name == step_name,
            HumanInterrupt.resolved == False,  # noqa: E712
        )
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
