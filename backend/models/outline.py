"""大纲模型

每个章节对应一条大纲条目，用于规划章节走向。
"""

from sqlalchemy import String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class Outline(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "outlines"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 对应章节序号
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    turning_point: Mapped[str | None] = mapped_column(Text, nullable=True)  # 转折点
    hidden_thread_ids: Mapped[list | None] = mapped_column("hidden_thread_ids", JSONValue(), nullable=True)  # 关联暗线ID列表
