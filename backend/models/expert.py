"""专家/Agent 模型

内置 6 个默认专家 + 用户自定义 Agent。
"""

from sqlalchemy import String, Text, Integer, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class Expert(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "experts"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role_type: Mapped[str] = mapped_column(String(20), nullable=False)  # writer | critic | editor | researcher | custom
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    workflow_position: Mapped[str] = mapped_column(String(30), nullable=False)  # replace_writer | post_critic etc.
    context_scope: Mapped[dict] = mapped_column(JSONValue(), nullable=False, default=dict)
    trigger: Mapped[str] = mapped_column(String(30), default="manual")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str] = mapped_column(String(20), default="blue")
