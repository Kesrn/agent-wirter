"""世界观条目模型"""

from sqlalchemy import String, Text, Float, ARRAY
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDMixin, TimestampMixin


class WorldEntry(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "world_entries"

    project_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    embedding = mapped_column(ARRAY(Float), nullable=True)
