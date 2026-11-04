"""对话式创作 —— 会话 / 消息 / 已确认任务约束

第一版（方案 §13 数据模型草案）：
- chat_sessions     会话（每用户每项目至多一个，get-or-create）
- chat_messages     消息（追加写不覆盖；user 消息按 (session_id, idempotency_key) 幂等）
- task_constraints  已确认任务约束（如"不能删除伏笔"，确认后固化）

设计约束：
- UUID 主键，GUID 兼容 SQLite。
- 聊天内容不直接写正式数据；意图 / 计划 / 任务卡作为结构化 JSON 随消息落库。
- response_json 保存分类接口的首次完整响应，幂等重放时直接返回该响应。
- reply_to_id 让 assistant 消息指向其回应的 user 消息，confirm 时可定位任务卡。
"""

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class ChatSession(UUIDMixin, TimestampMixin, Base):
    """一次对话会话。每用户每项目一条（get-or-create），摘要单独存不混入原文。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_chat_sessions_user_project"),
    )

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChatMessage(UUIDMixin, TimestampMixin, Base):
    """会话消息。追加写不覆盖；idempotency_key 在会话内唯一。

    assistant 消息保存：intent / plan / task_card（结构化），以及首次分类响应的
    完整 JSON（response_json，用于幂等重放）。execution_id 在任务卡确认后回填。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "idempotency_key",
            name="uq_chat_messages_session_idempotency",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant / system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(30), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    task_card: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # assistant 消息指向其回应的 user 消息 id（便于 confirm 定位任务卡）
    reply_to_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    # 分类接口的首次完整响应 JSON（幂等重放：重复请求直接返回该响应）
    response_json: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)


class TaskConstraint(UUIDMixin, TimestampMixin, Base):
    """已确认的任务约束（"不能删除伏笔"等）。确认后固化，用于后续任务上下文。"""

    __tablename__ = "task_constraints"

    session_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
