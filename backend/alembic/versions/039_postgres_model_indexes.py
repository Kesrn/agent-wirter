"""Add ORM indexes missing from PostgreSQL Alembic-created schemas."""

from alembic import op


revision = "039_postgres_model_indexes"
down_revision = "038_ai_run_request_id"
branch_labels = None
depends_on = None


_INDEXES = (
    ("ix_ai_runs_generation_record_id", "ai_runs", ("generation_record_id",)),
    ("ix_generation_records_base_version_id", "generation_records", ("base_version_id",)),
    ("ix_generation_records_chapter_id", "generation_records", ("chapter_id",)),
    ("ix_generation_records_conversation_id", "generation_records", ("conversation_id",)),
    ("ix_generation_records_document_id", "generation_records", ("document_id",)),
    ("ix_generation_records_expert_id", "generation_records", ("expert_id",)),
    ("ix_generation_records_project_id", "generation_records", ("project_id",)),
    ("ix_generation_records_superseded_by_execution_id", "generation_records", ("superseded_by_execution_id",)),
    ("ix_generation_records_task_card_id", "generation_records", ("task_card_id",)),
    ("ix_llm_call_logs_chapter_id", "llm_call_logs", ("chapter_id",)),
    ("ix_project_source_chapters_split_generation_id", "project_source_chapters", ("split_generation_id",)),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Concurrent builds avoid blocking writes on a running server. IF NOT EXISTS also
    # handles PostgreSQL databases previously initialized with metadata.create_all().
    for name, table, columns in _INDEXES:
        with op.get_context().autocommit_block():
            op.create_index(
                name,
                table,
                list(columns),
                if_not_exists=True,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, table, _columns in reversed(_INDEXES):
        with op.get_context().autocommit_block():
            op.drop_index(
                name,
                table_name=table,
                if_exists=True,
                postgresql_concurrently=True,
            )
