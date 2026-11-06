"""项目资料库 —— QA 会话模型"""

from sqlalchemy import String, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class KnowledgeQaSession(UUIDMixin, TimestampMixin, Base):
    """资料问答会话。

    每个项目可有多个会话，前端默认使用最近的 active session。
    summary 由对话压缩触发后更新。
    """

    __tablename__ = "knowledge_qa_sessions"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="资料问答")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_preferences: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions: Mapped[str | None] = mapped_column(Text, nullable=True)
    important_citations: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
