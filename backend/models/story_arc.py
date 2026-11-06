"""长线结构模型

StoryArc 表示小说的长线结构层级：Volume（卷）→ Act（幕）→ Arc（弧）。
通过 start_chapter / end_chapter 标记覆盖范围，或通过 Outline.story_arc_id 直接关联。
"""

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class StoryArc(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "story_arcs"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    parent_arc_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    arc_type: Mapped[str] = mapped_column(String(20), nullable=False)  # VOLUME / ACT / ARC
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_conflict: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    end_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PLANNED")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
