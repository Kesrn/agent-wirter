"""世界观条目模型"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, FloatList, GUID, JSONValue, UUIDMixin, TimestampMixin


class WorldEntry(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "world_entries"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    scope_type: Mapped[str] = mapped_column(String(20), default="global")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rules: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    embedding = mapped_column(FloatList(), nullable=True)
