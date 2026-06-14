"""Character per-chapter event model."""

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class CharacterEvent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "character_events"
    __table_args__ = (
        UniqueConstraint("project_id", "character_id", "chapter_sequence_number", name="uq_character_event_chapter"),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chapter_sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    appearance_type: Mapped[str] = mapped_column(String(20), default="appeared")
    appeared: Mapped[bool] = mapped_column(Boolean, default=True)
    event_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    actions: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    state_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    emotion: Mapped[str | None] = mapped_column(String(100), nullable=True)
    importance: Mapped[int] = mapped_column(Integer, default=3)
