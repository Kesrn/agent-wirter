"""项目资料库 —— 知识源切片模型"""

from sqlalchemy import String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, FloatList, GUID, JSONValue, UUIDMixin, TimestampMixin


class ProjectSourceChunk(UUIDMixin, TimestampMixin, Base):
    """资料条目的切片片段。

    每个 chunk 对应资料原文的一段，附带 AI 生成的压缩索引。
    embedding 字段预留给向量检索，当前检索走关键词匹配（ILIKE），暂不填充。
    """

    __tablename__ = "project_source_chunks"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    constraints: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding = mapped_column(FloatList(), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
