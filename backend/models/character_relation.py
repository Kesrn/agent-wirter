"""角色关系模型"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, UUIDMixin, TimestampMixin


class CharacterRelation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "character_relations"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_character_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    target_character_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
