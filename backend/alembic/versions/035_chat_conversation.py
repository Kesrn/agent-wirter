"""对话式创作：chat_sessions / chat_messages / task_constraints / change_sets
    + generation_records 追加对话列（conversation_id / base_version_id / task_card_id）

Revision ID: 035_chat_conversation
Revises: 034_extraction_job_split_generation
Create Date: 2026-08-02

方案 §13 数据模型草案（双后端 SQLite + PostgreSQL 同构）：
1. 建四张新表（chat_sessions / chat_messages / task_constraints / change_sets）；
2. generation_records 追加三列（候选稿归属：所属会话 / 基于的正式版本 / 任务卡）。
   base_version_id 只指向章节正式版本（ChapterVersion），不指向候选稿/生成任务。

全部幂等：重复执行结果一致；全新库直接建表；旧库按列缺失补齐。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "035_chat_conversation"
down_revision = "034_extraction_job_split_generation"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _ts_columns():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def upgrade() -> None:
    bind = _bind()
    uuid_t = _uuid_type()

    if not _has_table("chat_sessions"):
        op.create_table(
            "chat_sessions",
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column("project_id", uuid_t, nullable=False),
            sa.Column("user_id", uuid_t, nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            # project / user 删除 → 会话级联删除
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"],
                                    name="fk_chat_sessions_project", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    name="fk_chat_sessions_user", ondelete="CASCADE"),
            # 每用户每项目至多一条（get-or-create）
            sa.UniqueConstraint("user_id", "project_id",
                                name="uq_chat_sessions_user_project"),
            *_ts_columns(),
        )
        op.create_index("ix_chat_sessions_project_id", "chat_sessions", ["project_id"])
        op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])

    if not _has_table("chat_messages"):
        op.create_table(
            "chat_messages",
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column("session_id", uuid_t, nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("intent", sa.String(30), nullable=True),
            sa.Column("plan", _json_type(), nullable=True),
            sa.Column("task_card", _json_type(), nullable=True),
            sa.Column("execution_id", uuid_t, nullable=True),
            sa.Column("idempotency_key", sa.String(64), nullable=True),
            sa.Column("reply_to_id", uuid_t, nullable=True),
            sa.Column("response_json", _json_type(), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"],
                                    name="fk_chat_messages_session", ondelete="CASCADE"),
            # 幂等：同一会话内同一请求幂等号只落一条
            sa.UniqueConstraint("session_id", "idempotency_key",
                                name="uq_chat_messages_session_idempotency"),
            *_ts_columns(),
        )
        op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
        op.create_index("ix_chat_messages_execution_id", "chat_messages", ["execution_id"])

    if not _has_table("task_constraints"):
        op.create_table(
            "task_constraints",
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column("session_id", uuid_t, nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source_message_id", uuid_t, nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"],
                                    name="fk_task_constraints_session", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_message_id"], ["chat_messages.id"],
                                    name="fk_task_constraints_source_msg", ondelete="SET NULL"),
            *_ts_columns(),
        )
        op.create_index("ix_task_constraints_session_id", "task_constraints", ["session_id"])

    if not _has_table("change_sets"):
        op.create_table(
            "change_sets",
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column("session_id", uuid_t, nullable=False),
            sa.Column("execution_id", uuid_t, nullable=True),
            sa.Column("operation_type", sa.String(30), nullable=False),
            sa.Column("intent", sa.String(30), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("objects", _json_type(), nullable=True),
            sa.Column("base_version_id", uuid_t, nullable=True),
            sa.Column("parent_change_set_id", uuid_t, nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"],
                                    name="fk_change_sets_session", ondelete="CASCADE"),
            *_ts_columns(),
        )
        op.create_index("ix_change_sets_session_id", "change_sets", ["session_id"])
        op.create_index("ix_change_sets_execution_id", "change_sets", ["execution_id"])

    # generation_records 追加对话列（候选稿归属；无 FK，仅追踪字段）
    if _has_table("generation_records"):
        existing = {c["name"] for c in inspect(bind).get_columns("generation_records")}
        for col_name in ("conversation_id", "base_version_id", "task_card_id"):
            if col_name not in existing:
                op.add_column("generation_records", sa.Column(col_name, uuid_t, nullable=True))


def downgrade() -> None:
    bind = _bind()

    if _has_table("change_sets"):
        op.drop_index("ix_change_sets_execution_id", table_name="change_sets")
        op.drop_index("ix_change_sets_session_id", table_name="change_sets")
        op.drop_table("change_sets")
    if _has_table("task_constraints"):
        op.drop_index("ix_task_constraints_session_id", table_name="task_constraints")
        op.drop_table("task_constraints")
    if _has_table("chat_messages"):
        op.drop_index("ix_chat_messages_execution_id", table_name="chat_messages")
        op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
        op.drop_table("chat_messages")
    if _has_table("chat_sessions"):
        op.drop_index("ix_chat_sessions_user_id", table_name="chat_sessions")
        op.drop_index("ix_chat_sessions_project_id", table_name="chat_sessions")
        op.drop_table("chat_sessions")

    if _has_table("generation_records"):
        existing = {c["name"] for c in inspect(bind).get_columns("generation_records")}
        for col_name in ("conversation_id", "base_version_id", "task_card_id"):
            if col_name in existing:
                with op.batch_alter_table("generation_records") as batch_op:
                    batch_op.drop_column(col_name)
