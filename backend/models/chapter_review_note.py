"""Chapter review notes."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class ChapterReviewNote(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chapter_review_notes"

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, default="manual", index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info", index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
