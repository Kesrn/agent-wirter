"""AI Run Step 模型 — 一次任务中的步骤执行记录。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin
from .harness_enums import RunStepStatus


class AiRunStep(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "ai_run_steps"
    __table_args__ = (
        Index("ix_ai_run_steps_run_order", "run_id", "step_order"),
        Index("ix_ai_run_steps_run_status", "run_id", "status"),
        # 部分唯一索引：idempotency_key 非空时唯一。
        # sqlite_where / postgresql_where 双方言声明，SQLite 现代版支持部分索引。
        Index(
            "ux_ai_run_steps_idempotency",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=RunStepStatus.PENDING)

    input: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Expert System v2 ── 本步骤对应的专家 / 节点快照
    node_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expert_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expert_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_dir: Mapped[str | None] = mapped_column(String(100), nullable=True)
