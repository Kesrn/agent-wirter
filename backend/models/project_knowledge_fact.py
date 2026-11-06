"""项目资料库 —— 规则事实索引模型

预计算的"人物 -> 法系"等结构化事实，由 reindex 阶段的规则扫描器写入。
QA 时优先查事实表，命中则用确定性答案，引用指回原文 chunk。
本表是索引而非原文，任何答案都必须能追溯到 source/chunk。
"""

from sqlalchemy import String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class ProjectKnowledgeFact(UUIDMixin, TimestampMixin, Base):
    """规则预计算的结构化事实。

    第一阶段只写 character_system 类型：
    fact_type = "character_system", predicate = "has_magic_system",
    subject = 人物名, object = 法系。
    """

    __tablename__ = "project_knowledge_facts"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chunk_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)

    fact_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    object: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    confidence: Mapped[str] = mapped_column(String(30), nullable=False, default="explicit", index=True)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    extractor: Mapped[str] = mapped_column(String(80), nullable=False, default="rule.character_system.v1")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONValue(), nullable=True)
