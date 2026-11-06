"""Clarification Interrupt 服务层 — 管理 clarification 类型的 HumanInterrupt。

复用 human_interrupts 表，payload.type="clarification_questions"。
不新增表，不改旧 human-decisions 语义。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harness.human_interrupt_service import resolve_interrupt
from models.ai_run import AiRun
from models.harness_enums import InterruptDecision, InterruptStatus
from models.human_interrupt import HumanInterrupt

logger = logging.getLogger(__name__)

CLARIFICATION_PAYLOAD_TYPE = "clarification_questions"


async def create_clarification_interrupt(
    db: AsyncSession,
    *,
    run: AiRun,
    thread_id: str | None = None,
    step_id: str | uuid.UUID | None = None,
    round_num: int = 1,
    max_rounds: int = 3,
    questions: list[dict[str, Any]] | None = None,
    assumptions_if_skipped: list[str] | None = None,
    existing_answers: dict[str, str] | None = None,
    clarification_summary: str = "",
    chapter_id: str | None = None,
    chapter_sequence_number: int | None = None,
) -> HumanInterrupt:
    """创建一个 clarification 类型的 HumanInterrupt。

    payload 结构：
    {
        "type": "clarification_questions",
        "workflow_key": ...,
        "checkpoint_name": "clarification_review",
        "chapter_id": ...,
        "chapter_sequence_number": ...,
        "round": 1,
        "max_rounds": 3,
        "questions": [...],
        "assumptions_if_skipped": [...],
        "existing_answers": {...},
        "clarification_summary": ""
    }
    """
    payload = {
        "type": CLARIFICATION_PAYLOAD_TYPE,
        "checkpoint_name": "clarification_review",
        "chapter_id": chapter_id,
        "chapter_sequence_number": chapter_sequence_number,
        "round": round_num,
        "max_rounds": max_rounds,
        "questions": questions or [],
        "assumptions_if_skipped": assumptions_if_skipped or [],
        "existing_answers": existing_answers or {},
        "clarification_summary": clarification_summary,
    }

    interrupt = HumanInterrupt(
        run_id=run.id,
        step_id=step_id if isinstance(step_id, uuid.UUID) else (uuid.UUID(str(step_id)) if step_id else None),
        thread_id=thread_id,
        step_name="human_clarification",
        status=InterruptStatus.WAITING,
        payload=payload,
        resolved=False,
    )
    db.add(interrupt)
    await db.flush()
    return interrupt


async def get_latest_clarification_interrupt(
    db: AsyncSession,
    run_id: str | uuid.UUID,
) -> HumanInterrupt | None:
    """获取指定 run 最新的 clarification interrupt。

    只返回 payload.type == "clarification_questions" 的 interrupt。
    """
    result = await db.execute(
        select(HumanInterrupt)
        .where(HumanInterrupt.run_id == run_id)
        .order_by(HumanInterrupt.created_at.desc())
    )
    for interrupt in result.scalars().all():
        if interrupt.payload and interrupt.payload.get("type") == CLARIFICATION_PAYLOAD_TYPE:
            return interrupt
    return None


async def submit_clarification_answers(
    db: AsyncSession,
    interrupt: HumanInterrupt,
    *,
    answers: dict[str, str],
) -> HumanInterrupt:
    """提交澄清回答，resolve interrupt。

    Args:
        db: 数据库会话
        interrupt: 要解析的 clarification interrupt
        answers: {question_id: answer_value} 映射

    Returns:
        更新后的 interrupt

    Raises:
        ValueError: 如果 interrupt 已 resolved
    """
    if interrupt.resolved:
        raise ValueError(f"该 clarification interrupt 已于 {interrupt.resolved_at} 解析为 {interrupt.decision}")

    # 把 answers 写入 payload
    payload = interrupt.payload or {}
    payload["submitted_answers"] = answers
    # 合并 existing_answers + submitted_answers
    existing = payload.get("existing_answers", {})
    existing.update(answers)
    payload["existing_answers"] = existing
    interrupt.payload = payload
    # JSON 列无 mutability tracking，需显式标记脏
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(interrupt, "payload")

    await resolve_interrupt(
        db,
        interrupt,
        decision=InterruptDecision.SUBMIT_CLARIFICATION,
        feedback=None,
    )
    await db.flush()
    return interrupt


async def skip_clarification(
    db: AsyncSession,
    interrupt: HumanInterrupt,
) -> HumanInterrupt:
    """跳过澄清，resolve interrupt（使用默认假设）。

    Args:
        db: 数据库会话
        interrupt: 要解析的 clarification interrupt

    Returns:
        更新后的 interrupt

    Raises:
        ValueError: 如果 interrupt 已 resolved
    """
    if interrupt.resolved:
        raise ValueError(f"该 clarification interrupt 已于 {interrupt.resolved_at} 解析为 {interrupt.decision}")

    # 标记跳过
    payload = interrupt.payload or {}
    payload["skipped"] = True
    interrupt.payload = payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(interrupt, "payload")

    await resolve_interrupt(
        db,
        interrupt,
        decision=InterruptDecision.SKIP_CLARIFICATION,
        feedback=None,
    )
    await db.flush()
    return interrupt


def format_clarification_response(interrupt: HumanInterrupt) -> dict[str, Any]:
    """把 clarification interrupt 格式化为 API 响应。"""
    payload = interrupt.payload or {}
    return {
        "run_id": str(interrupt.run_id),
        "interrupt_id": str(interrupt.id),
        "status": interrupt.status,
        "round": payload.get("round", 1),
        "max_rounds": payload.get("max_rounds", 3),
        "questions": payload.get("questions", []),
        "assumptions_if_skipped": payload.get("assumptions_if_skipped", []),
        "existing_answers": payload.get("existing_answers", {}),
        "clarification_summary": payload.get("clarification_summary", ""),
        "resolved": interrupt.resolved,
    }
