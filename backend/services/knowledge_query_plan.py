"""知识库查询计划器 —— 确定性规则实现，不依赖 LLM。

将用户自然语言问题转化为结构化的检索计划。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── 意图识别规则 ──────────────────────────────────────

CHARACTER_PROFILE_HINTS = ("个人资料", "个人信息", "人物资料", "角色资料", "是谁", "介绍", "简介", "情况", "身份", "背景")
RELATION_HINTS = ("关系", "和谁", "跟谁", "感情", "朋友", "敌人", "家族")
ABILITY_HINTS = ("能力", "技能", "魔法", "修为", "等级", "职业", "天赋", "系")
CHARACTER_BY_ABILITY_HINTS = ("谁是", "谁有", "哪些人", "哪些角色", "人物有哪些", "角色有哪些", "有哪些人", "有哪些角色")
TIMELINE_HINTS = ("经历", "出场", "剧情", "发生", "时间线", "章节")
WORLDBUILDING_HINTS = ("设定", "世界观", "规则", "禁忌", "体系", "组织", "势力")
PLOT_HINTS = ("灾难", "事件", "战役", "大战", "冲突", "变故")

INTENT_PRIORITY = [
    "relationship",
    "character_by_ability",  # 新增：反向属性检索，优先级高于character_profile和ability
    "character_profile",
    "ability",
    "timeline",
    "worldbuilding",
    "plot",
    "general",
]

INTENT_HINTS_MAP = {
    "character_profile": CHARACTER_PROFILE_HINTS,
    "relationship": RELATION_HINTS,
    "ability": ABILITY_HINTS,
    "character_by_ability": CHARACTER_BY_ABILITY_HINTS,
    "timeline": TIMELINE_HINTS,
    "worldbuilding": WORLDBUILDING_HINTS,
    "plot": PLOT_HINTS,
}

# ── 停用词 ──────────────────────────────────────────

QUESTION_STOP_WORDS = (
    "的个人资料", "个人资料", "个人信息", "人物资料", "角色资料", "人物信息",
    "的情况", "情况", "说一下", "介绍一下", "讲一下", "是什么", "是谁",
    "怎么样", "如何", "一下", "请问", "关于", "简介", "资料", "信息",
    "帮我", "请", "给我", "能不能", "告诉我", "可以",
    "的态度变化", "的态度", "的看法", "发生了什么", "有什么", "什么系的", "什么系",
    "拥有哪些系", "有哪些系", "有什么系", "有几个系", "多少个系", "几个系", "哪些系",
    "有哪些技能", "有什么技能", "有哪些魔法", "有什么魔法", "有哪些能力", "有什么能力",
    "是啥样的", "是什么样的", "啥样的", "什么样的", "是啥样", "是什么样", "啥样", "什么样",
)

# ── 实体抽取正则 ──────────────────────────────────────
# 关系正则必须前置，避免被通用正则先匹配整句
# 特殊后缀停用词（不带"的"前缀，用于从清洗后文本提取实体）

_ENTITY_SUFFIX_REMOVAL = (
    "的态度变化", "的态度", "的看法",
    "发生了什么", "怎么样", "是什么", "是谁", "介绍一下", "简介",
    "的关系", "的能力", "的设定", "的经历", "的出场",
    "关系", "能力", "设定", "经历", "出场", "剧情", "事件",
    "技能", "魔法", "修为", "等级", "天赋",
    "是啥样的", "是什么样的", "啥样的", "什么样的", "是啥样", "是什么样", "啥样", "什么样",
    "规则", "禁忌", "体系", "组织", "势力", "什么系的", "什么系",
    "拥有哪些系", "有哪些系", "有什么系", "有几个系", "多少个系", "几个系", "哪些系",
    "有哪些技能", "有什么技能", "有哪些魔法", "有什么魔法", "有哪些能力", "有什么能力",
    "有哪些", "有什么",
)

# 从问题中直接抽取实体的正则（匹配后的 group 需要清理后缀）
ENTITY_PATTERNS = (
    # 反向属性检索：谁是冰系的？/ 哪些角色有冰系？/ 冰系人物有哪些？
    re.compile(r"(?:那|那么|还有)?谁(?:是|有)([\u4e00-\u9fff]{1,4}系)的?(?:呢)?[？?]?$"),
    re.compile(r"(?:哪些人|哪些角色|有哪些人|有哪些角色)(?:是|有|觉醒|拥有|修炼|掌握)([\u4e00-\u9fff]{1,4}系)"),
    re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:的)?(?:人物|角色|法师)(?:有哪些|有谁|是谁)[？?]?$"),
    re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:有哪些人物|有哪些角色|有谁|是谁)[？?]?$"),
    # 人物 + 指定法系详情：莫凡他的土系是啥样的 / 莫凡的土系是什么样
    re.compile(r"([\u4e00-\u9fff]{2,4}?)(?:他(?:的)?|她(?:的)?|的)?([\u4e00-\u9fff]{1,4}系)(?:是啥样的|是什么样的|啥样的|什么样的|是啥样|是什么样|啥样|什么样|怎么样)[？?]?$"),
    # 两人关系（最高优先级）
    re.compile(r"(.+?)和(.+?)(?:是?什么|的|什么的)?(?:关系|冲突|感情|互动)"),
    re.compile(r"(.+?)跟(.+?)(?:是?什么|的|什么的)?(?:关系|冲突|感情|互动)"),
    re.compile(r"(.+?)与(.+?)(?:是?什么|的|什么的)?(?:关系|冲突|感情|互动)"),
    re.compile(r"(.+?)对(.+?)(?:的态度|的态度变化|的看法)"),
    # 人物档案
    re.compile(r"关于(.+?)(?:的|是|$)"),
    re.compile(r"(.+?)(?:的个人资料|个人资料|个人信息|人物资料|角色资料|人物信息)"),
    re.compile(r"(.+?)(?:是谁|是什么|的情况|情况|介绍一下|简介|的身份|的背景)"),
    # 能力
    re.compile(r"(.+?系)(?:有哪些技能|有什么技能|有哪些魔法|有什么魔法|有哪些能力|有什么能力)"),
    re.compile(r"(.+?)(?:拥有哪些系|有哪些系|有什么系|有几个系|多少个系|几个系|哪些系)"),
    re.compile(r"(.+?)(?:的能力|能力|技能|魔法|修为|等级|天赋)"),
    # 时间线/剧情
    re.compile(r"(.+?)(?:的经历|经历|出场|剧情|事件)"),
    # 设定/世界观
    re.compile(r"(.+?)(?:的设定|设定|的规则|规则|的禁忌|禁忌)"),
    # 事件/灾难（"博城灾难"类）
    re.compile(r"([\u4e00-\u9fff]{2,6}(?:灾难|事件|战役|大战|冲突|变故))"),
    # 追问省略："那叶心夏呢" / "牧奴娇呢"
    re.compile(r"(?:那|那么|还有)?([\u4e00-\u9fff]{2,8})(?:呢|怎么样)?[？?]?$"),
    # 通用：中文人名/地名（2-6字）后接特定后缀
    re.compile(r"([\u4e00-\u9fff]{2,6})(?:是什么|是谁|怎么样|发生了什么)"),
)


def _clean_entity_suffix(text: str) -> str:
    """从实体文本中清理残留的后缀词和位置修饰。"""
    text = re.sub(r"^(那|那么|还有)", "", text)
    text = re.sub(r"(呢|吗|么)$", "", text)
    for suffix in _ENTITY_SUFFIX_REMOVAL:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = re.sub(r"(是什么|是谁|什么)$", "", text)
    # 清理 "在X之后/之前/期间" 类位置修饰（不在开头也匹配）
    text = re.sub(r"在[\u4e00-\u9fff]+(?:之后|之前|期间|以后|以前|时)", "", text)
    # 清理 "在X里/中/上/下" 类位置修饰
    text = re.sub(r"在[\u4e00-\u9fff]+(?:里|中|上|下)", "", text)
    return text.strip()


# ── 意图对应搜索词模板 ──────────────────────────────

def _safe_queries(e: list[str], template_fn) -> list[str]:
    """安全调用模板函数，空实体时返回空列表。"""
    if not e:
        return []
    try:
        return template_fn(e)
    except (IndexError, KeyError):
        return []


SEARCH_QUERY_TEMPLATES = {
    "character_profile": lambda e: [
        e[0],
        f"{e[0]} 身份",
        f"{e[0]} 背景",
        f"{e[0]} 关系",
        f"{e[0]} 能力",
        f"{e[0]} 经历",
        f"{e[0]} 出场",
    ],
    "relationship": lambda e: (
        [
            f"{e[0]} {e[1]} 关系",
            f"{e[0]} {e[1]} 冲突",
            f"{e[0]} {e[1]} 感情",
            f"{e[0]} {e[1]} 伴侣",
            f"{e[0]} {e[1]} 恋人",
            f"{e[0]} {e[1]} 夫人",
            f"{e[0]} {e[1]} 男友",
            f"{e[0]} {e[1]} 女友",
            f"{e[0]} {e[1]} 独处",
        ]
        if len(e) >= 2
        else [
            f"{e[0]} 关系",
            f"{e[0]} 家族",
            f"{e[0]} 朋友",
            f"{e[0]} 敌人",
        ]
    ),
    "character_by_ability": lambda e: [
        f"{e[0]} 人物",
        f"{e[0]} 角色",
        f"{e[0]} 法师",
        f"觉醒 {e[0]}",
        f"拥有 {e[0]}",
        f"修炼 {e[0]}",
        f"掌握 {e[0]}",
        f"主修 {e[0]}",
        f"辅修 {e[0]}",
        f"{e[0]} 天生",
        f"{e[0]} 第一系",
        f"{e[0]} 第二系",
        f"{e[0]} 第三系",
    ],
    "ability": lambda e: [
        f"{e[0]} 能力",
        f"{e[0]} 系",
        f"{e[0]} 魔法",
        f"{e[0]} 技能",
        f"{e[0]} 基础技能",
        f"{e[0]} 一阶变体",
        f"{e[0]} 二阶变体",
        f"{e[0]} 三阶变体",
        f"{e[0]} 修为",
        f"{e[0]} 天赋",
    ],
    "timeline": lambda e: [
        f"{e[0]} 经历",
        f"{e[0]} 出场",
        f"{e[0]} 剧情",
        f"{e[0]} 事件",
    ],
    "worldbuilding": lambda e: [
        f"{e[0]} 设定",
        f"{e[0]} 规则",
        f"{e[0]} 体系",
        f"{e[0]} 组织",
    ],
    "plot": lambda e: [
        f"{e[0]} 剧情",
        f"{e[0]} 事件",
        f"{e[0]} 发生",
    ],
    "general": lambda e: e[:3] if e else [],
}


def _is_character_system_detail(question: str, entities: list[str], intent: str) -> bool:
    """识别“某人的某系是什么样”这类人物能力细问。"""
    return (
        intent == "ability"
        and len(entities) >= 2
        and not entities[0].endswith("系")
        and entities[1].endswith("系")
        and any(marker in question for marker in ("啥样", "什么样", "怎么样", "是什么样", "是啥样"))
    )


def _character_system_queries(character: str, system: str) -> list[str]:
    return [
        f"{character} {system}",
        f"{character} 觉醒 {system}",
        f"{character} 拥有 {system}",
        f"{character} 修炼 {system}",
        f"{character} 掌握 {system}",
        f"{system} 技能",
        f"{system} 魔法",
        f"{system} 基础技能",
        f"{system} 一阶变体",
        f"{system} 二阶变体",
        f"{system} 三阶变体",
    ]


def _extract_character_system_detail(question: str) -> list[str]:
    """提取“某人的某系是什么样”中的人物和法系。"""
    patterns = (
        re.compile(r"([\u4e00-\u9fff]{2,4}?)(?:他(?:的)?|她(?:的)?)([\u4e00-\u9fff]{1,4}系)(?:是啥样的|是什么样的|啥样的|什么样的|是啥样|是什么样|啥样|什么样|怎么样)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{2,4})(?:的)([\u4e00-\u9fff]{1,4}系)(?:是啥样的|是什么样的|啥样的|什么样的|是啥样|是什么样|啥样|什么样|怎么样)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{2,4}?)([\u4e00-\u9fff]{1,4}系)(?:是啥样的|是什么样的|啥样的|什么样的|是啥样|是什么样|啥样|什么样|怎么样)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{2,4}?)(?:他(?:的)?|她(?:的)?)([\u4e00-\u9fff]{1,4}系)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{2,4})(?:的)([\u4e00-\u9fff]{1,4}系)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{2,4}?)([\u4e00-\u9fff]{1,4}系)[？?]?$"),
    )
    for pattern in patterns:
        match = pattern.search(question)
        if match:
            character = match.group(1).strip()
            system = match.group(2).strip()
            if character and system.endswith("系"):
                return [character, system]
    return []


# ── 核心函数 ──────────────────────────────────────────

@dataclass
class KnowledgeQueryPlan:
    """查询计划 dataclass，不建表。"""
    original_question: str
    intent: str = "general"
    entities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    required_terms: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)


def _clean_stop_words(question: str) -> str:
    """清洗停用词。"""
    text = question
    for w in QUESTION_STOP_WORDS:
        text = text.replace(w, "")
    text = re.sub(r"[？？!！。，,\s]+", " ", text).strip()
    return text


def _extract_entities(question: str) -> list[str]:
    """从问题中抽取实体。"""
    raw_entities: list[str] = []
    for pat in ENTITY_PATTERNS:
        for m in pat.finditer(question):
            for g in m.groups():
                if not g:
                    continue
                g = _clean_entity_suffix(g.strip())
                if g and 2 <= len(g) <= 8 and g not in raw_entities:
                    raw_entities.append(g)

    # 拆分复合实体："穆宁雪和莫凡" → "穆宁雪", "莫凡"
    entities: list[str] = []
    for e in raw_entities:
        parts = _split_compound_entity(e)
        entities.extend(parts)

    # 去重（保持顺序）
    seen: set[str] = set()
    deduped: list[str] = []
    for e in entities:
        if any(marker in e for marker in ("她", "他", "TA", "ta", "跟", "和", "与")):
            continue
        if any(existing and e.startswith(existing) and len(e) > len(existing) for existing in deduped):
            continue
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped[:5]


def _split_compound_entity(e: str) -> list[str]:
    """拆分复合实体，如 "穆宁雪和莫凡" → ["穆宁雪", "莫凡"]。"""
    parts = re.split(r"\s*(?:和|跟|与|对)\s*", e)
    parts = [p.strip() for p in parts if p.strip() and 2 <= len(p.strip()) <= 8]
    return parts if len(parts) > 1 else [e]


def _detect_intent(question: str) -> str:
    """基于规则识别意图。"""
    # 反向属性检索优先检测：如果问题匹配反向属性正则，直接返回
    _CHARACTER_BY_ABILITY_PATTERNS = (
        re.compile(r"(?:那|那么|还有)?谁(?:是|有)([\u4e00-\u9fff]{1,4}系)的?(?:呢)?[？?]?$"),
        re.compile(r"(?:哪些人|哪些角色|有哪些人|有哪些角色)(?:是|有|觉醒|拥有|修炼|掌握)([\u4e00-\u9fff]{1,4}系)"),
        re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:的)?(?:人物|角色|法师)(?:有哪些|有谁|是谁)[？?]?$"),
        re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:有哪些人物|有哪些角色|有谁|是谁)[？?]?$"),
        re.compile(r"谁(?:修炼|掌握|拥有|觉醒|主修|辅修)了?([\u4e00-\u9fff]{1,4}系)"),
    )
    for pat in _CHARACTER_BY_ABILITY_PATTERNS:
        if pat.search(question):
            return "character_by_ability"
    
    scores: dict[str, int] = {intent: 0 for intent in INTENT_PRIORITY}
    for intent, hints in INTENT_HINTS_MAP.items():
        for h in hints:
            if h in question:
                scores[intent] += 1
    best = max(scores, key=scores.get)  # type: ignore
    return best if scores[best] > 0 else "general"


def _extract_keywords(cleaned: str, entities: list[str]) -> list[str]:
    """从清洗后文本中提取关键词。"""
    text = cleaned
    for e in entities:
        text = text.replace(e, "")
    text = re.sub(r"\s+", " ", text).strip()
    kws = [
        k.strip()
        for k in re.split(r"[\s,，、]+", text)
        if k.strip() and len(k.strip()) >= 1 and k.strip() not in {
            "那", "呢", "那呢", "那么", "还有", "的", "他的", "她的", "样", "样的", "的样"
        }
    ]
    return kws


def build_knowledge_query_plan(question: str, *, project_id: str | None = None) -> KnowledgeQueryPlan:
    """将用户自然语言问题转为结构化查询计划。

    纯规则实现，不调用 LLM，不查 DB。
    """
    intent = _detect_intent(question)
    
    # 反向属性检索：优先从正则提取系别实体
    character_system_entities = _extract_character_system_detail(question) if intent == "ability" else []

    if character_system_entities:
        entities = character_system_entities
    elif intent == "character_by_ability":
        _CBA_PATTERNS = (
            re.compile(r"(?:那|那么|还有)?谁(?:是|有)([\u4e00-\u9fff]{1,4}系)的?(?:呢)?[？?]?$"),
            re.compile(r"(?:哪些人|哪些角色|有哪些人|有哪些角色)(?:是|有|觉醒|拥有|修炼|掌握)([\u4e00-\u9fff]{1,4}系)"),
            re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:的)?(?:人物|角色|法师)(?:有哪些|有谁|是谁)[？?]?$"),
            re.compile(r"([\u4e00-\u9fff]{1,4}系)(?:有哪些人物|有哪些角色|有谁|是谁)[？?]?$"),
            re.compile(r"谁(?:修炼|掌握|拥有|觉醒|主修|辅修)了?([\u4e00-\u9fff]{1,4}系)"),
        )
        cba_entities = []
        for pat in _CBA_PATTERNS:
            m = pat.search(question)
            if m:
                entity = m.group(1).strip()
                if entity and entity not in cba_entities:
                    cba_entities.append(entity)
                break
        
        if cba_entities:
            entities = cba_entities
        else:
            entities = _extract_entities(question)
    else:
        entities = _extract_entities(question)
    
    cleaned = _clean_stop_words(question)
    keywords = _extract_keywords(cleaned, entities)

    # 如果实体为空，把清洗后的关键词当实体用
    if not entities and keywords:
        entities = keywords[:2]

    # 生成搜索 queries
    template_fn = SEARCH_QUERY_TEMPLATES.get(intent, SEARCH_QUERY_TEMPLATES["general"])
    search_queries = _safe_queries(entities, template_fn)
    is_character_system_detail = _is_character_system_detail(question, entities, intent)
    if is_character_system_detail:
        search_queries = _character_system_queries(entities[0], entities[1])

    is_system_list_question = any(marker in question for marker in ("什么系", "什么系的", "哪些系", "有哪些系", "拥有哪些系", "几个系", "多少个系"))
    is_system_skill_question = bool(re.search(r".+?系(?:有哪些技能|有什么技能|有哪些魔法|有什么魔法|有哪些能力|有什么能力)", question))

    # 原问题也作为一个兜底 query；系别列表类问题清洗后常退化成纯角色名，容易污染召回。
    if not is_system_list_question and question.strip() not in search_queries:
        search_queries.append(question.strip())

    if entities and is_system_list_question:
        system_query = f"{entities[0]} 系"
        if system_query not in search_queries:
            search_queries.insert(0, system_query)
    if entities and is_system_skill_question:
        for system_query in (
            f"{entities[0]} 技能",
            f"{entities[0]} 基础技能",
            f"{entities[0]} 一阶变体",
            f"{entities[0]} 二阶变体",
            f"{entities[0]} 三阶变体",
        ):
            if system_query not in search_queries:
                search_queries.insert(0, system_query)

    # 关系类问题必须尽量同时约束双方，否则会被单个高频主角名淹没。
    if intent == "relationship" and len(entities) >= 2:
        required_terms = entities[:2]
    elif is_character_system_detail:
        # 用法系作为硬约束，人物作为排序/首轮 query 的软约束。
        # 这样资料没有“莫凡+土系”同句时，仍可召回通用土系技能表。
        required_terms = entities[1:2]
    else:
        required_terms = entities[:1] if entities else []

    # facets
    facets = []
    if intent in ("relationship", "character_profile", "character_by_ability"):
        facets.append("character")
    if intent == "worldbuilding":
        facets.append("world_entry")
    if intent in ("timeline", "plot"):
        facets.append("outline")

    return KnowledgeQueryPlan(
        original_question=question,
        intent=intent,
        entities=entities,
        keywords=keywords,
        search_queries=search_queries[:8],
        required_terms=required_terms,
        facets=facets,
    )
