"""项目资料库 —— QA 消息模型"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class KnowledgeQaMessage(UUIDMixin, TimestampMixin, Base):
    """资料问答单条消息。"""

    __tablename__ = "knowledge_qa_messages"

    session_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    citations: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
