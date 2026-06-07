"""AI 生成历史记录模型"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class GenerationRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "generation_records"

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    expert_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("experts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    accepted_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    review_results: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    request_params: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
