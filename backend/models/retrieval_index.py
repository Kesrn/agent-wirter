"""检索投影模型层 —— RAG 检索索引（阶段一）

三张表构成"检索投影"：RAG 检索只读这里的投影数据，不直接扫业务表。
投影可按源随时重建（source_kind + source_id + index_generation_id 定位版本），
因此本层不是业务事实来源，任何命中都必须能回溯到源内容。

retrieval_chunks                检索块：内容 + 分词后的检索文本 + 内容哈希
retrieval_index_generation      一次索引构建记录（building -> active/superseded/failed）
retrieval_index_source_state    每个源当前活动构建版本的指针（取状态为 active 的生成）

source_kind 枚举（retrieval_chunks / generation / source_state 共用）：
  chapter_version | source_chunk | character_profile | ability_profile
  | timeline_event | world_entry

status 枚举（retrieval_index_generation）：
  building | active | superseded | failed
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class RetrievalChunk(UUIDMixin, TimestampMixin, Base):
    """检索投影块：被切分、规范化后的可检索文本单元。

    content 保留源片段原文；search_text 是分词/规范化后的检索文本，供关键词
    检索使用。content_hash 用于幂等重建（同一内容不重复写块）。
    """

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        Index(
            "ix_retrieval_chunks_project_source",
            "project_id", "source_kind", "source_id",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    index_generation_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)


class RetrievalIndexGeneration(UUIDMixin, TimestampMixin, Base):
    """一次索引构建记录：某源某次重建产生的全部块共用同一个 generation_id。"""

    __tablename__ = "retrieval_index_generation"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    generation_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="building")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetrievalIndexSourceState(UUIDMixin, TimestampMixin, Base):
    """活动版本指针：每个源当前对外生效的构建版本。

    重建完成后把 active_generation_id 指向新 generation；旧 generation 标记
    superseded。UNIQUE(project_id, source_kind, source_id) 保证一个源只有一条指针。
    """

    __tablename__ = "retrieval_index_source_state"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_kind", "source_id",
            name="uq_retrieval_source_state_project_kind_source",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    active_generation_id: Mapped[str] = mapped_column(String(100), nullable=False)
