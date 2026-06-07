"""LLM 配置模型

每个用户一条配置记录，存储加密后的 API Key。
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, UUIDMixin, TimestampMixin


class LLMConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "llm_configs"

    user_id: Mapped[str] = mapped_column(GUID(), nullable=False, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")
    encrypted_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
