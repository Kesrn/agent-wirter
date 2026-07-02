"""LLM Call Log 模型 — 每次 LLM 调用的本地审计记录（不可变，只有 created_at）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin


class LlmCallLog(UUIDMixin, Base):
    __tablename__ = "llm_call_logs"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    prompt_template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    rendered_prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_package_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    model_config_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    request: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
