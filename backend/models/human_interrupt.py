"""Human Interrupt 模型 — 持久化人工审核等待状态（可恢复 HITL）。"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin
from .harness_enums import InterruptStatus


class HumanInterrupt(UUIDMixin, Base):
    __tablename__ = "human_interrupts"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    step_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=InterruptStatus.WAITING
    )
    payload: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
