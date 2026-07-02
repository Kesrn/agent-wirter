"""AI Run 模型 — 一次 AI 任务的业务生命周期。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin
from .harness_enums import RunStatus


class AiRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "ai_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    generation_record_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True, index=True
    )

    run_type: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=RunStatus.CREATED)
    current_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_goal: Mapped[str | None] = mapped_column(Text, nullable=True)

    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    model_config_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    cost_usage: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
