"""写作记忆 staging 模型 — 章节确认后抽取的写作记忆，待用户确认后入正式设定库。

不复用 extraction_staging（那是资料源抽取 staging，keyed on source_id）。
本表 keyed on chapter_id + run_id + chapter_version_id，是"作者确认章节后产生的写作记忆"。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin
from .harness_enums import MemoryStagingStatus, MemoryType


class WritingMemoryStaging(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "writing_memory_staging"

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    chapter_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    chapter_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    memory_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONValue(), nullable=False, default=dict)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemoryStagingStatus.GENERATED, index=True
    )
    confirmed_target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confirmed_target_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
