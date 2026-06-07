"""章节版本历史模型"""

from sqlalchemy import String, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, UUIDMixin, TimestampMixin


class ChapterVersion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chapter_versions"

    chapter_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
