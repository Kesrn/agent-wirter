"""小说资料库 —— 结构化知识表（LLM 抽取合并目标）

4 张表对应文档 §4.4-4.7：
character_profile  人物档案（身份/状态/别名）
ability_profile    人物能力（法系/技能/境界，按 ability_type 枚举）
event_timeline     事件时间线
world_rule         世界规则/设定

优先级体系（canon_level / origin / source_priority）解决来源冲突：
  manual(100) > fanfic(80) > original(60)
  origin 区分 manual / llm_extracted / rule_extracted
高优先级来源可覆盖低优先级字段。

与规则事实索引 project_knowledge_facts 的关系：
  ability_profile 是结构化主库（LLM 抽取，覆盖面广）；
  project_knowledge_facts 是高精度规则补充（人物→法系，可交叉校验）。
  structured QA 查询优先级：manual > fanfic > ability_profile > facts > RAG。
"""

from sqlalchemy import String, Text, Integer, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class CharacterProfile(UUIDMixin, TimestampMixin, Base):
    """人物档案。UNIQUE(project_id, name) 防止同人/原著人物重复。"""

    __tablename__ = "character_profile"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_character_profile_project_name"),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)
    identity_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)
    # 人物统计字段（方案 §3.1）
    first_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appearance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CharacterAppearance(UUIDMixin, TimestampMixin, Base):
    """章节出场记录：每章每人物一条，避免 character_profile.evidence 无限膨胀。"""
    __tablename__ = "character_appearance"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    chapter_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    character_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)

    character_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    chapter_title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    role_in_chapter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)

    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_id", "chapter_no", "canonical_name",
            name="uq_character_appearance_per_chapter",
        ),
    )

class AbilityProfile(UUIDMixin, TimestampMixin, Base):
    """人物能力。

    ability_type 枚举：magic_element|spell|cultivation_level|martial_art|item|bloodline|skill|unknown
    UNIQUE(project_id, character_name, ability_type, ability_name) 防重复。
    """

    __tablename__ = "ability_profile"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "character_name", "ability_type", "ability_name",
            name="uq_ability_profile_char_type_name",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    character_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ability_type: Mapped[str] = mapped_column(String(100), nullable=False)
    ability_name: Mapped[str] = mapped_column(String(200), nullable=False)
    level_desc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)


class EventTimeline(UUIDMixin, TimestampMixin, Base):
    """事件时间线。MVP 阶段不做复杂去重，每章重要事件可直接新增。"""

    __tablename__ = "event_timeline"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_title: Mapped[str] = mapped_column(String(500), nullable=False)
    event_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    characters: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)
    location_desc: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cause_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)


class WorldRule(UUIDMixin, TimestampMixin, Base):
    """世界规则/设定。查重键 project_id + category + rule_text 完全一致。"""

    __tablename__ = "world_rule"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "category", "rule_text",
            name="uq_world_rule_project_cat_text",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(50), nullable=False, default="medium")
    canon_level: Mapped[str] = mapped_column(String(50), nullable=False, default="original")
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="llm_extracted")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[list | None] = mapped_column(JSONValue(), nullable=True, default=list)


class CharacterAliasCluster(UUIDMixin, TimestampMixin, Base):
    """项目级人物别名归一簇。

    一个簇记录一个规范名 + 它的全部异体写法。
    merge / QA 阶段据本项目簇把异体名归一到 canonical_name，避免同人被拆成多档。
    候选探测只给建议，必须人工确认后才入此表——不自动合并。
    UNIQUE(project_id, canonical_name) 防止同项目重复规范名。
    """

    __tablename__ = "character_alias_clusters"
    __table_args__ = (
        UniqueConstraint("project_id", "canonical_name",
                         name="uq_character_alias_project_canonical"),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONValue(), nullable=True, default=list)
    # manual=人工确认；detected=候选探测建议但已人工确认；imported=外部导入
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
