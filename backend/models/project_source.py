"""项目资料库 —— 知识源模型"""

from sqlalchemy import String, Text, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class ProjectSource(UUIDMixin, TimestampMixin, Base):
    """用户上传的资料条目。

    source_type:
      - upload: 用户上传的 TXT/MD 文件
      - fanfic_rule: 同人创作规则（默认 always_inject=True）
      - timeline: 时间线文本
      - note: 用户笔记
      - reference: 外部参考资料
    """

    __tablename__ = "project_sources"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, default="upload", index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_facts: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    constraints: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    characters: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    always_inject: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
