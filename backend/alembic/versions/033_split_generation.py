"""切分代次：split_generations / source_split_state / split_exclusions + 章节切分代列

Revision ID: 033_split_generation
Revises: 032_retrieval_index_job_pending_unique
Create Date: 2026-08-02

P-1 阶段四（方案 v3 §3.5）：
1. 建三张切分代表（split_generations / source_split_state / split_exclusions）；
2. project_source_chapters 追加切分代列（split_generation_id / source_ordinal /
   display_chapter_no / original_label / start_offset / end_offset / content_hash）
   + 唯一索引 uq_source_chapters_generation_ordinal（代内 ordinal 唯一，display 可重复）；
3. legacy 回填：为每个已有章节数据的 (project_id, source_id) 建 legacy 占位代
   （status='legacy'，source_ordinal=旧 chapter_no，display_chapter_no=NULL，
   旧数据无可靠展示号），并插入活动指针（仅当该 source 尚无指针）。

全部幂等：重复执行结果一致；全新库跳过回填（无数据）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "033_split_generation"
down_revision = "032_retrieval_index_job_pending_unique"
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

    # 1. split_generations
    if not _has_table("split_generations"):
        op.create_table(
            "split_generations",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("source_content_hash", sa.String(64), nullable=False),
            sa.Column("parser_version", sa.String(50), nullable=False),
            sa.Column("decisions_hash", sa.String(64), nullable=True),
            sa.Column("decision_ledger", _json_type(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="building"),
            sa.Column("validation_report", _json_type(), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            # source 删除 → 该 source 全部切分代级联删除
            sa.ForeignKeyConstraint(["source_id"], ["project_sources.id"],
                                    name="fk_split_generations_source",
                                    ondelete="CASCADE"),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_split_generations_project_id ON split_generations (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_split_generations_source_id ON split_generations (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_split_generations_status ON split_generations (status)")

    # 2. source_split_state（联合主键；source 删除级联，代删除时指针置空）
    if not _has_table("source_split_state"):
        op.create_table(
            "source_split_state",
            sa.Column("project_id", _uuid_type(), primary_key=True),
            sa.Column("source_id", _uuid_type(), primary_key=True),
            sa.Column("active_split_generation_id", _uuid_type(), nullable=True),
            sa.ForeignKeyConstraint(["source_id"], ["project_sources.id"],
                                    name="fk_source_split_state_source",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["active_split_generation_id"], ["split_generations.id"],
                name="fk_source_split_state_active", ondelete="SET NULL"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_source_split_state_active ON source_split_state (active_split_generation_id)")

    # 3. split_exclusions（代删除 → 排除区间级联删除）
    if not _has_table("split_exclusions"):
        op.create_table(
            "split_exclusions",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("split_generation_id", _uuid_type(), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("start_offset", sa.Integer, nullable=False),
            sa.Column("end_offset", sa.Integer, nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("reason", sa.String(500), nullable=True),
            sa.Column("ordinal_ref", sa.Integer, nullable=True),
            sa.ForeignKeyConstraint(
                ["split_generation_id"], ["split_generations.id"],
                name="fk_split_exclusions_generation", ondelete="CASCADE"),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_split_exclusions_split_generation_id ON split_exclusions (split_generation_id)")

    # 4. project_source_chapters 切分代列（FK：代删除 → 章节归属置空，行保留）
    if not _has_table("project_source_chapters"):
        return
    existing = {c["name"] for c in inspect(bind).get_columns("project_source_chapters")}
    additions = [
        ("split_generation_id", _uuid_type(), True),
        ("source_ordinal", sa.Integer(), True),
        ("display_chapter_no", sa.Integer(), True),
        ("original_label", sa.String(500), True),
        ("start_offset", sa.Integer(), True),
        ("end_offset", sa.Integer(), True),
        ("content_hash", sa.String(64), True),
    ]
    with op.batch_alter_table("project_source_chapters") as batch_op:
        for name, col_type, nullable in additions:
            if name not in existing:
                batch_op.add_column(sa.Column(name, col_type, nullable=nullable))
        batch_op.create_unique_constraint(
            "uq_source_chapters_generation_ordinal",
            ["split_generation_id", "source_ordinal"],
        )
        batch_op.create_foreign_key(
            "fk_source_chapters_generation",
            "split_generations",
            ["split_generation_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 5. legacy 回填（幂等：存在 legacy 则跳过；仅回填 split_generation_id IS NULL 的行）
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT project_id, source_id FROM project_source_chapters"
        )
    ).fetchall()
    for project_id, source_id in rows:
        legacy = bind.execute(
            sa.text(
                "SELECT id FROM split_generations WHERE project_id=:pid "
                "AND source_id=:sid AND status='legacy' ORDER BY created_at LIMIT 1"
            ),
            {"pid": project_id, "sid": source_id},
        ).fetchone()
        if legacy is None:
            src = bind.execute(
                sa.text(
                    "SELECT content FROM project_sources "
                    "WHERE id=:sid AND project_id=:pid"
                ),
                {"pid": project_id, "sid": source_id},
            ).fetchone()
            content_hash = ""
            if src is not None:
                import hashlib
                content_hash = hashlib.sha256(
                    (src[0] or "").encode("utf-8")
                ).hexdigest()
            legacy_id = str(uuid4())
            bind.execute(
                sa.text(
                    "INSERT INTO split_generations "
                    "(id, project_id, source_id, source_content_hash, parser_version, "
                    " decisions_hash, decision_ledger, status, validation_report, "
                    " activated_at) "
                    "VALUES (:id, :pid, :sid, :hash, 'legacy', NULL, NULL, "
                    " 'legacy', NULL, NULL)"
                ),
                {"id": legacy_id, "pid": project_id, "sid": source_id,
                 "hash": content_hash},
            )
        else:
            legacy_id = legacy[0]
        bind.execute(
            sa.text(
                "UPDATE project_source_chapters "
                "SET split_generation_id=:gid, source_ordinal=chapter_no, "
                "    display_chapter_no=NULL, original_label=chapter_title "
                "WHERE project_id=:pid AND source_id=:sid "
                "AND split_generation_id IS NULL"
            ),
            {"gid": legacy_id, "pid": project_id, "sid": source_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO source_split_state "
                "(project_id, source_id, active_split_generation_id) "
                "VALUES (:pid, :sid, :gid) "
                "ON CONFLICT (project_id, source_id) DO NOTHING"
            ),
            {"pid": project_id, "sid": source_id, "gid": legacy_id},
        )


def uuid4() -> str:
    import uuid
    return uuid.uuid4()


def downgrade() -> None:
    bind = _bind()
    if _has_table("project_source_chapters"):
        existing = {c["name"] for c in inspect(bind).get_columns("project_source_chapters")}
        with op.batch_alter_table("project_source_chapters") as batch_op:
            fk_names = {fk["name"] for fk in inspect(bind).get_foreign_keys("project_source_chapters")}
            if "fk_source_chapters_generation" in fk_names:
                batch_op.drop_constraint("fk_source_chapters_generation", type_="foreignkey")
            for name in (
                "content_hash", "end_offset", "start_offset", "original_label",
                "display_chapter_no", "source_ordinal", "split_generation_id",
            ):
                if name in existing:
                    batch_op.drop_column(name)
            uq_names = {c["name"] for c in inspect(bind).get_unique_constraints("project_source_chapters")}
            if "uq_source_chapters_generation_ordinal" in uq_names:
                batch_op.drop_constraint(
                    "uq_source_chapters_generation_ordinal", type_="unique"
                )
    # 先删引用代次的外键子表，再删代次表本身
    for tbl in ("split_exclusions", "source_split_state", "split_generations"):
        if _has_table(tbl):
            op.drop_table(tbl)
