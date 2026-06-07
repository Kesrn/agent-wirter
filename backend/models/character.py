"""角色模型"""

from sqlalchemy import String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, FloatList, GUID, JSONValue, UUIDMixin, TimestampMixin


class Character(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "characters"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role_type: Mapped[str] = mapped_column(String(20), default="supporting")
    profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    faction: Mapped[str | None] = mapped_column(String(100), nullable=True)
    appearance_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
    embedding = mapped_column(FloatList(), nullable=True)
