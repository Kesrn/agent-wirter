"""操作记账（change_set）—— 跨对象撤销；第一版只记单对象正文，objects 数组预留。

方案 §5：应用执行工作流时记录每个受影响对象的前/后引用；撤销整轮 =
按时间逆序回滚 change_set 链。流程控制意图（撤销/采用/定稿）映射到
change_set 状态流转，不新增平行机制。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class ChangeSet(UUIDMixin, TimestampMixin, Base):
    """一次已确认操作的记账。

    status: pending（候选已生成未采用） / applied（已写正式数据） /
            reverted（已回滚/已放弃） / failed。
    objects: [{object_kind: chapter/outline/character, object_id,
               before_ref, after_ref}] —— 第一版只记单个 chapter 对象。
    base_version_id 只指向章节正式版本（ChapterVersion），不指向候选稿/生成任务。
    """

    __tablename__ = "change_sets"

    session_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    operation_type: Mapped[str] = mapped_column(String(30), nullable=False)  # chapter_rewrite
    intent: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    objects: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    base_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    parent_change_set_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
