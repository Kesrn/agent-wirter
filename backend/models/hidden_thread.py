"""暗线模型

暗线是贯穿多个章节的隐藏线索。
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class HiddenThread(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "hidden_threads"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_nums: Mapped[list | None] = mapped_column("chapter_nums", JSONValue(), nullable=True)  # 涉及的章节序号列表
