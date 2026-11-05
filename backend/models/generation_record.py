"""AI 生成历史记录模型"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class GenerationRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "generation_records"
    __table_args__ = (
        # 每 run_id 最多一条非 NULL 记录（候选/applied 互斥）。
        # partial unique index 在 SQLite/PostgreSQL 都支持；run_id 为 NULL 的普通历史不受约束。
        Index(
            "uq_generation_records_run_id",
            "run_id",
            unique=True,
            sqlite_where=text("run_id IS NOT NULL"),
            postgresql_where=text("run_id IS NOT NULL"),
        ),
    )

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    expert_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("experts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    accepted_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    # Read-time cache derived from the accepted chapter version and later manual versions.
    human_edit_metrics: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    review_results: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    request_params: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    # 035 对话式创作：候选稿归属（复用 generation_records 存候选稿，不新建表）。
    # base_version_id 只指向章节正式版本（ChapterVersion），不指向候选稿/生成任务。
    conversation_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    base_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    task_card_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    # 重新生成后旧候选稿被标记 superseded 时，记录替代它的执行（change_set）id。
    superseded_by_execution_id: Mapped[str | None] = mapped_column(
        GUID(), nullable=True, index=True
    )
