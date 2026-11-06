"""generation_records.superseded_by_execution_id：重新生成后旧候选稿标记替代执行

Revision ID: 036_generation_record_superseded
Revises: 035_chat_conversation
Create Date: 2026-08-05

对话式创作：regenerate 成功后，未采用的旧候选稿标 status='superseded' 并记录
被哪个新执行（change_set）替代。superseded 是只读终态，区别于用户主动丢弃的
discarded。双后端（PG UUID / SQLite CHAR(36)）兼容，幂等。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "036_generation_record_superseded"
down_revision = "035_chat_conversation"
branch_labels = None
depends_on = None


def _uuid_type():
    if inspect(op.get_bind()).dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("generation_records"):
        return
    # 列与索引分别检测、分别补齐：列已存在但索引缺失的库也必须补索引
    existing = {c["name"] for c in inspect(bind).get_columns("generation_records")}
    if "superseded_by_execution_id" not in existing:
        with op.batch_alter_table("generation_records") as batch_op:
            batch_op.add_column(sa.Column(
                "superseded_by_execution_id", _uuid_type(), nullable=True,
            ))
    index_names = {i["name"] for i in inspect(bind).get_indexes("generation_records")}
    if "ix_generation_records_superseded_by" not in index_names:
        op.execute("CREATE INDEX ix_generation_records_superseded_by "
                   "ON generation_records (superseded_by_execution_id)")


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("generation_records"):
        return
    existing = {c["name"] for c in inspect(op.get_bind()).get_columns("generation_records")}
    if "superseded_by_execution_id" not in existing:
        return
    with op.batch_alter_table("generation_records") as batch_op:
        batch_op.drop_column("superseded_by_execution_id")
