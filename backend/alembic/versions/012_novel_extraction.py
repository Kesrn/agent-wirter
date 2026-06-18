"""novel extraction pipeline + structured knowledge tables

Revision ID: 012_novel_extraction
Revises: 011_knowledge_library
Create Date: 2026-06-17

7 张表：
  流水线：project_source_chapters / extraction_jobs / extraction_staging
  结构化：character_profile / ability_profile / event_timeline / world_rule
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "012_novel_extraction"
down_revision = "011_knowledge_library"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 011 一致） ──────────────────

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


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    indexes = {idx["name"] for idx in inspect(_bind()).get_indexes(table_name)}
    if index_name in indexes:
        op.drop_index(index_name, table_name=table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _ts_columns():
    """created_at / updated_at 通用列。"""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def upgrade() -> None:
    # 1. project_source_chapters
    if not _has_table("project_source_chapters"):
        op.create_table(
            "project_source_chapters",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("chapter_no", sa.Integer, nullable=False),
            sa.Column("chapter_title", sa.String(500), nullable=True),
            sa.Column("content", sa.Text, nullable=False, server_default=""),
            sa.Column("split_type", sa.String(50), nullable=False, server_default="chapter_regex"),
            sa.Column("token_count", sa.Integer, nullable=True),
            sa.Column("char_count", sa.Integer, nullable=True),
            sa.Column("warning", sa.Text, nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_source_chapters_project_id ON project_source_chapters (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_source_chapters_source_id ON project_source_chapters (source_id)")

    # 2. extraction_jobs
    if not _has_table("extraction_jobs"):
        op.create_table(
            "extraction_jobs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("genre", sa.String(50), nullable=False),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
            sa.Column("chapter_no_start", sa.Integer, nullable=True),
            sa.Column("chapter_no_end", sa.Integer, nullable=True),
            sa.Column("max_chapters_per_run", sa.Integer, nullable=False, server_default="20"),
            sa.Column("force_reextract", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("total_chapters", sa.Integer, nullable=False, server_default="0"),
            sa.Column("extracted_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("validated_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("merged_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_jobs_project_id ON extraction_jobs (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_jobs_source_id ON extraction_jobs (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_jobs_status ON extraction_jobs (status)")

    # 3. extraction_staging（raw_output TEXT NOT NULL, raw_json nullable）
    if not _has_table("extraction_staging"):
        op.create_table(
            "extraction_staging",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("job_id", _uuid_type(), nullable=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("chapter_no", sa.Integer, nullable=True),
            sa.Column("chapter_title", sa.String(500), nullable=True),
            sa.Column("genre", sa.String(50), nullable=False),
            sa.Column("template_name", sa.String(100), nullable=False),
            sa.Column("schema_version", sa.String(50), nullable=False),
            sa.Column("raw_output", sa.Text, nullable=False, server_default=""),
            sa.Column("raw_json", _json_type(), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="EXTRACTED"),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_staging_job_id ON extraction_staging (job_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_staging_project_id ON extraction_staging (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_staging_source_id ON extraction_staging (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_staging_status ON extraction_staging (status)")

    # 4. character_profile
    if not _has_table("character_profile"):
        op.create_table(
            "character_profile",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("aliases", _json_type(), nullable=True),
            sa.Column("identity_desc", sa.Text, nullable=True),
            sa.Column("status_desc", sa.Text, nullable=True),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("source_priority", sa.Integer, nullable=False, server_default="60"),
            sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("evidence", _json_type(), nullable=True),
            *_ts_columns(),
            sa.UniqueConstraint("project_id", "name", name="uq_character_profile_project_name"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_character_profile_project_id ON character_profile (project_id)")

    # 5. ability_profile
    if not _has_table("ability_profile"):
        op.create_table(
            "ability_profile",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=True),
            sa.Column("character_name", sa.String(200), nullable=False),
            sa.Column("ability_type", sa.String(100), nullable=False),
            sa.Column("ability_name", sa.String(200), nullable=False),
            sa.Column("level_desc", sa.String(200), nullable=True),
            sa.Column("status", sa.String(100), nullable=True),
            sa.Column("first_seen_chapter", sa.Integer, nullable=True),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("source_priority", sa.Integer, nullable=False, server_default="60"),
            sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("evidence", _json_type(), nullable=True),
            *_ts_columns(),
            sa.UniqueConstraint("project_id", "character_name", "ability_type", "ability_name",
                                name="uq_ability_profile_char_type_name"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ability_profile_project_id ON ability_profile (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ability_profile_character_name ON ability_profile (character_name)")

    # 6. event_timeline
    if not _has_table("event_timeline"):
        op.create_table(
            "event_timeline",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=True),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("chapter_no", sa.Integer, nullable=True),
            sa.Column("event_title", sa.String(500), nullable=False),
            sa.Column("event_desc", sa.Text, nullable=True),
            sa.Column("characters", _json_type(), nullable=True),
            sa.Column("location_desc", sa.String(500), nullable=True),
            sa.Column("cause_desc", sa.Text, nullable=True),
            sa.Column("effect_desc", sa.Text, nullable=True),
            sa.Column("importance", sa.Integer, nullable=False, server_default="3"),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("source_priority", sa.Integer, nullable=False, server_default="60"),
            sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("evidence", _json_type(), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_timeline_project_id ON event_timeline (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_timeline_chapter_no ON event_timeline (chapter_no)")

    # 7. world_rule
    if not _has_table("world_rule"):
        op.create_table(
            "world_rule",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=True),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("chapter_no", sa.Integer, nullable=True),
            sa.Column("category", sa.String(200), nullable=False),
            sa.Column("rule_text", sa.Text, nullable=False),
            sa.Column("priority", sa.String(50), nullable=False, server_default="medium"),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("source_priority", sa.Integer, nullable=False, server_default="60"),
            sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("evidence", _json_type(), nullable=True),
            *_ts_columns(),
            sa.UniqueConstraint("project_id", "category", "rule_text",
                                name="uq_world_rule_project_cat_text"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_world_rule_project_id ON world_rule (project_id)")


def downgrade() -> None:
    for tbl in (
        "world_rule", "event_timeline", "ability_profile", "character_profile",
        "extraction_staging", "extraction_jobs", "project_source_chapters",
    ):
        _drop_table_if_exists(tbl)
