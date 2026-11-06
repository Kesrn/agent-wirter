"""小说资料库 —— 章节切分 / 抽取任务 / 抽取暂存（流水线表）

这三张表构成 LLM 结构化抽取流水线：
project_source_chapters  资料源拆出的章节
extraction_jobs           一次抽取任务的状态流
extraction_staging        每章 LLM 原始输出与解析结果（支持非法 JSON 落库）

设计约束：
- UUID 主键，GUID 兼容 SQLite。
- extraction_staging.raw_output 为 TEXT NOT NULL，raw_json 为 nullable JSON，
  保证 LLM 返回任何内容都能落库，便于排查与重试。
- 抽取任务用 job 表 + 轮询推进，不依赖 BackgroundTask（桌面端关进程即丢任务）。
"""

from datetime import datetime
from sqlalchemy import String, Text, Integer, Float, Boolean, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class ProjectSourceChapter(UUIDMixin, TimestampMixin, Base):
    """资料源章节（不是用户正在创作的章节，避免污染 writing_units）。

    P-1 阶段四（方案 v3 §3.5）：章节行按切分代隔离。
      split_generation_id    所属切分代（legacy 或新代）
      source_ordinal         代内顺序（1..N，代内唯一）
      display_chapter_no     原文标题号（允许重复：8 组 keep_duplicate；legacy 为 NULL）
      original_label         原始标题标签（"第613章 肉丘障碍"）
      start_offset/end_offset 章节区间在原文中的字符偏移 [start, end)
      content_hash           完整 SHA-256（64 hex）
    旧 chapter_no 保留兼容（= source_ordinal），逐步废弃。
    """

    __tablename__ = "project_source_chapters"
    __table_args__ = (
        UniqueConstraint(
            "split_generation_id", "source_ordinal",
            name="uq_source_chapters_generation_ordinal",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    split_type: Mapped[str] = mapped_column(String(50), nullable=False, default="chapter_regex")
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── P-1 阶段四：切分代列 ──
    split_generation_id: Mapped[str | None] = mapped_column(
        GUID(), nullable=True, index=True
    )
    source_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ExtractionJob(UUIDMixin, TimestampMixin, Base):
    """一次 LLM 结构化抽取任务的状态记录。

    采用 job 表 + 轮询推进模式：每次接口调用推进若干章，状态落库，
    失败可续跑，不依赖 BackgroundTask（避免桌面端关进程丢任务）。
    """

    __tablename__ = "extraction_jobs"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    genre: Mapped[str] = mapped_column(String(50), nullable=False)
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING", index=True)

    # 本次抽取使用的 LLM provider（mock / openai / ...），用于区分模拟数据与真实抽取数据。
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="mock")

    # 任务启动时锁定的切分代（方案 v3 §3.5 规则 4：抽取任务固定引用
    # split_generation_id，运行中不随活动指针漂移——抽取基于的章节定义中途不得变化）。
    # NULL = 迁移前创建的旧任务（读取当前活动代，保持旧行为）。
    split_generation_id: Mapped[str | None] = mapped_column(
        GUID(), nullable=True, index=True
    )

    chapter_no_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_no_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_chapters_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    force_reextract: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    total_chapters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extracted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    merged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 任务体验控制字段（方案 §4.2）
    current_chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtractionStaging(UUIDMixin, TimestampMixin, Base):
    """每章 LLM 原始输出与解析结果。

    关键：raw_output TEXT NOT NULL 保证 LLM 返回任何内容都能落库；
    raw_json nullable，parse 失败时为 null。便于排查与重试。
    """

    __tablename__ = "extraction_staging"

    job_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chapter_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    genre: Mapped[str] = mapped_column(String(50), nullable=False)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)

    raw_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_json: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="EXTRACTED", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本条 staging 使用的 LLM provider（与 job.provider 一致）。
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="mock")
