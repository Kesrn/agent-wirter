"""LLM 结构化抽取的 Pydantic Schema 与 Prompt（文档 §8、§9）。

Schema 用于校验 LLM 返回的 JSON：
- 所有数组字段必须存在（无内容返回空数组）
- 每条必须有 evidence
- confidence ∈ [0,1]，importance ∈ [1,5]
- ability_type / status / priority 枚举校验
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field, field_validator


class AbilityType(str, Enum):
    magic_element = "magic_element"
    spell = "spell"
    cultivation_level = "cultivation_level"
    martial_art = "martial_art"
    item = "item"
    bloodline = "bloodline"
    skill = "skill"
    unknown = "unknown"


class AbilityStatus(str, Enum):
    new = "new"
    used = "used"
    upgraded = "upgraded"
    mentioned = "mentioned"
    lost = "lost"
    unknown = "unknown"


class RulePriority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CharacterItem(BaseModel):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    identity: str = ""
    status: str = ""
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)  # 必须有 evidence


class AbilityItem(BaseModel):
    character: str = Field(min_length=1)
    ability_type: AbilityType
    ability_name: str = Field(min_length=1)
    level: str = ""
    status: AbilityStatus = AbilityStatus.unknown
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


class EventItem(BaseModel):
    event_title: str = Field(min_length=1)
    event_desc: str = ""
    characters: list[str] = Field(default_factory=list)
    location: str = ""
    cause: str = ""
    effect: str = ""
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


class WorldRuleItem(BaseModel):
    category: str = Field(min_length=1)
    rule_text: str = Field(min_length=1)
    priority: RulePriority = RulePriority.medium
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


class ChapterExtraction(BaseModel):
    """单章抽取结果的顶层 Schema。"""
    chapter_no: int
    chapter_title: str = ""
    chapter_summary: str = ""
    characters: list[CharacterItem] = Field(default_factory=list)
    abilities: list[AbilityItem] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)
    world_rules: list[WorldRuleItem] = Field(default_factory=list)


# ── Prompt（文档 §9） ────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """你是一个小说知识库结构化抽取引擎。
你的任务是从给定章节中抽取对后续问答、剧情理解、人物一致性、世界观设定有价值的信息。
重要规则：
1. 只允许根据当前章节文本抽取。
2. 不允许使用外部知识、原著外知识或模型记忆补充。
3. 不允许编造。
4. 每条信息必须提供 evidence。
5. evidence 必须来自当前章节，可以是原文短句或准确摘要。
6. 如果不确定，降低 confidence。
7. 普通气氛描写、重复动作、无后续影响的闲聊不要抽。
8. 输出必须是合法 JSON。
9. 字段没有内容时返回空数组，不要省略字段。
10. 不要输出 Markdown，不要解释，只输出 JSON。
11. 保持输出紧凑：chapter_summary 不超过 120 字；event_desc、rule_text、evidence 不超过 80 字。
12. 单章最多输出 characters 8 条、abilities 12 条、events 8 条、world_rules 8 条，只保留明确且重要的信息。

输出 JSON Schema（严格按此结构，数组无内容时返回 []）：
{
  "chapter_no": 数字,
  "chapter_title": "字符串",
  "chapter_summary": "字符串",
  "characters": [
    {"name": "人物名", "aliases": ["别名"], "identity": "身份", "status": "状态",
     "importance": 1到5的整数, "confidence": 0到1的小数, "evidence": "原文证据"}
  ],
  "abilities": [
    {"character": "人物名", "ability_type": "magic_element|spell|cultivation_level|martial_art|item|bloodline|skill|unknown",
     "ability_name": "能力名", "level": "等级", "status": "new|used|upgraded|mentioned|lost|unknown",
     "importance": 1到5, "confidence": 0到1, "evidence": "原文证据"}
  ],
  "events": [
    {"event_title": "事件标题", "event_desc": "描述", "characters": ["人物名"],
     "location": "地点", "cause": "起因", "effect": "影响",
     "importance": 1到5, "confidence": 0到1, "evidence": "原文证据"}
  ],
  "world_rules": [
    {"category": "类别", "rule_text": "规则内容", "priority": "low|medium|high",
     "confidence": 0到1, "evidence": "原文证据"}
  ]
}"""

EXTRACTION_USER_TEMPLATE = """小说类型：{genre}
当前抽取重点：
{template_focus}
章节编号：{chapter_no}
章节标题：{chapter_title}
章节正文：
{chapter_content}
请严格按指定 JSON Schema 输出结构化抽取结果。"""

# 类型模板的抽取重点（文档 §7）
TEMPLATE_FOCUS = {
    "magic_fantasy": (
        "人物（身份、状态）\n"
        "能力（魔法系别、技能、境界）\n"
        "组织（学府、世家、妖魔）\n"
        "重要事件\n"
        "世界规则"
    ),
    "historical": (
        "人物（身份、官职、爵位）\n"
        "家族、朝代、阵营\n"
        "战争、政变\n"
        "地理地点\n"
        "制度规则\n"
        "重要事件"
    ),
}

SCHEMA_VERSION = "1.0"
TEMPLATE_NAME = "chapter_extraction_v1"
MAX_EXTRACT_CHARS = 16000  # 文档 §5.4，单章最大提交字符数


def build_extraction_user_prompt(
    genre: str, chapter_no: int, chapter_title: str, chapter_content: str
) -> str:
    focus = TEMPLATE_FOCUS.get(genre, TEMPLATE_FOCUS["magic_fantasy"])
    return EXTRACTION_USER_TEMPLATE.format(
        genre=genre,
        template_focus=focus,
        chapter_no=chapter_no,
        chapter_title=chapter_title or "",
        chapter_content=chapter_content,
    )
