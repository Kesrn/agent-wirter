"""用户可切换的 LLM 配置档案。

一个用户可以保存多条供应商配置；同一时刻最多一条 ``is_active`` 配置参与生成。
API Key 仍只以 Fernet 密文存储。
"""

from sqlalchemy import Boolean, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, UUIDMixin, TimestampMixin


class LLMConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "llm_configs"
    __table_args__ = (
        Index(
            "uq_llm_configs_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    user_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="默认配置")
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="openai")
    encrypted_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
