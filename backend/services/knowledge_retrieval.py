"""知识库证据检索层 —— 分类、重排、分组。

在现有 search_project_knowledge_with_plan 基础上，对检索结果做：
1. 证据类型分类（7 种）
2. 按查询意图重排
3. 分组输出给 prompt
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── 证据类型枚举 ────────────────────────────────────────

EVIDENCE_TYPES = (
    "direct_character_evidence",   # 人物与属性的直接绑定
    "relationship_evidence",       # 两人同段 + 关系词
    "ability_table",               # 技能表 / 能力设定表
    "worldbuilding_entry",         # 世界观设定
    "timeline_event",              # 时间线 / 事件
    "negative_evidence",           # 否定证据（"没有说明" "无法确定"）
    "generic_context",             # 普通正文兜底
)

# 否定证据关键词
NEGATION_TERMS = (
    "没有说明", "未说明", "没说明", "不能说明", "无法说明",
    "不是", "并非", "不属于", "没有明确", "未明确", "无法确定",
    "不确定", "不清楚", "没有提到", "未提到", "没有写",
    "没有直接证据", "没有找到", "资料中没有",
)

# 技能表关键词
ABILITY_TABLE_TERMS = (
    "初阶", "中阶", "高阶", "超阶", "禁咒",
    "一阶变体", "二阶变体", "三阶变体",
    "基础技能", "技能表", "法系",
)

# 能力语义词（无属性匹配时的直接证据判定）：snippet 含这些词说明是
# 能力/战斗/修炼类内容，而非纯场景描写。
# "许昭霆雷电实力如何"时 chunk101（"尝我的雷印！！许昭霆大吼…雷弧…幽狼兽"）
# 不含属性字面（雷电能力/实力），但明显是雷系实战——没有这组词它会被判
# generic_context 挤出证据池；而"张小侯站在城墙望着敌军"（纯场景）不含
# 这些词，仍正确降级 generic。
ABILITY_CONTEXT_TERMS = (
    "雷系", "火系", "冰系", "风系", "土系", "水系", "光系", "暗系",
    "雷印", "雷弧", "电弧", "雷电", "闪电", "魔法", "法术", "技能",
    "觉醒", "修为", "星子", "星轨", "星尘", "释放", "施展", "修炼",
    "战斗", "对战", "攻击", "击败", "轰", "劈", "诅咒", "治愈", "召唤",
)

# 关系词
RELATIONSHIP_TERMS = (
    "恋人", "情侣", "夫妻", "妻子", "丈夫", "男友", "女友",
    "朋友", "敌人", "师徒", "父子", "母女", "兄弟", "姐妹",
    "搭档", "伙伴", "同伴", "对手", "下属", "上司",
    "关系", "感情", "冲突", "互动", "信任", "依赖",
    "凡雪山", "创立", "共同",
)

# 世界观关键词
WORLDBUILDING_TERMS = (
    "设定", "规则", "体系", "组织", "势力", "种族",
    "禁忌", "世界", "大陆", "帝国", "王国", "神庙",
    "灾难", "事件", "战役", "大战", "冲突",
)

# 人物名字模式：2-4个中文字符
CHARACTER_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")

# 单个 source 最多贡献的 chunk 数，避免一个长资料淹没证据池。
# 8.7MB 小说原文会切出 7000+ 个 chunk，上限太小会把关键实战片段（许昭霆
# 雷印战幽狼兽）挤出证据池——按长原文场景放宽到 16。
MAX_CHUNKS_PER_SOURCE = 16


# ── 就近绑定判定 ────────────────────────────────────────
# 解决"赵满延的光系魔法保护张小侯"被误归因为张小侯会光系的问题。
# 只做句法就近判定，不引入具体角色/法系名，避免硬编码偏向某部作品。

# 子句分隔符：比 _split_sentences 更细，含逗号/分号/换行，
# 能在"赵满延的光系魔法保护了张小侯"这类长句里定位到逗号级边界。
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r，,]+")

# 人物—属性绑定连接词（通用动词/系词，不含具体内容）
BINDING_TERMS = (
    "是", "为", "属于", "觉醒", "拥有", "修炼", "掌握",
    "学会", "使用", "释放", "施展", "擅长", "精通", "是个",
)


def _split_clauses(text: str) -> list[str]:
    """按句末标点 + 逗号/分号/换行切分子句，返回去空后的非空片段列表。

    人物-属性绑定必须在较短语义片段内判断；整段文本太长时，容易把不同人物和
    不同能力错误绑定到一起。
    """
    return [s.strip() for s in _CLAUSE_SPLIT_RE.split(text) if s.strip()]


def _find_all(haystack: str, needle: str) -> list[int]:
    """返回 needle 在 haystack 中所有出现位置（左端点）。"""
    if not needle:
        return []
    starts = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            break
        starts.append(idx)
        start = idx + 1
    return starts


def _has_other_name_between(clause: str, entity: str, attr: str,
                            e_pos: int, a_pos: int,
                            known_entities: tuple[str, ...] = ()) -> bool:
    """检查 entity 与 attr 在 clause 中之间是否夹着第三个人名。

    用于拦截"赵满延的光系魔法保护了张小侯"：entity=张小侯、attr=光系，
    两者之间夹着"赵满延"这个人名，说明光系是赵满延的而非张小侯的。

    只把"已知人物实体列表里出现过、且不是当前 entity"的名字当作第三人，
    避免把"释放""觉醒"这类 2-4 字的绑定动词误判为人名。
    """
    if not known_entities:
        # 没有已知实体列表时退化为：跳过任何与绑定词重叠的疑似人名
        lo, hi = sorted((e_pos, a_pos))
        span = clause[lo + len(entity):hi] if e_pos < a_pos else clause[lo + len(attr):hi]
        for m in CHARACTER_NAME_RE.finditer(span):
            name = m.group()
            if name == entity or name == attr or name in entity or entity in name:
                continue
            if any(ch in name for bt in BINDING_TERMS for ch in bt):
                continue
            return True
        return False

    others = [ke for ke in known_entities if ke and ke != entity and ke != attr]
    if not others:
        return False
    lo, hi = sorted((e_pos, a_pos))
    span = clause[lo + len(entity):hi] if e_pos < a_pos else clause[lo + len(attr):hi]
    return any(o.lower() in span for o in others)


def _is_entity_attr_bound(entity: str, attr: str, clause: str,
                          known_entities: tuple[str, ...] = ()) -> bool:
    """判断 entity 与 attr 在单个子句内是否就近绑定。

    绑定成立条件（满足其一）：
    1. 属格紧邻：clause 含 "entity的attr"（张小侯的风系）
    2. 绑定词连接：entity 与 attr 之间字符距离 ≤ 阈值，且中间含绑定词
       （张小侯觉醒风系 / 张小侯释放风系魔法）
    且不满足"第三人夹击"排除条件。
    known_entities 传入已知人物实体，用于精确识别第三人（避免把动词误判为人名）。
    """
    # 这里做的是规则级“证据质量过滤”，不是最终回答。
    # 如果绑定不成立，该证据会降级为普通上下文，避免误导 LLM。
    e = entity.lower()
    a = attr.lower()
    c = clause.lower()
    if e not in c or a not in c:
        return False

    # 1. 属格紧邻
    genitive = f"{e}的{a}"
    gpos = c.find(genitive)
    if gpos >= 0:
        if _has_other_name_between(c, e, a, gpos, gpos + len(e) + 1, known_entities):
            return False
        return True

    e_positions = _find_all(c, e)
    a_positions = _find_all(c, a)
    for ep in e_positions:
        for ap in a_positions:
            dist = abs(ap - ep)
            if dist > max(8, len(e) + len(a) + 4):
                continue
            # 取 entity 与 attr 之间的文本（含 attr/entity 邻域以捕捉"是"等连接词）
            if ep < ap:
                window = c[ep:ap + len(a)]
            else:
                window = c[ap:ep + len(e)]
            # 2. 绑定词连接
            if any(bt in window for bt in BINDING_TERMS):
                # 排除第三人人名夹击
                if _has_other_name_between(c, e, a, ep, ap, known_entities):
                    continue
                return True
    return False


# ── 数据结构 ────────────────────────────────────────────

@dataclass
class ClassifiedEvidence:
    """分类后的单条证据。

    evidence_type 决定它在 prompt 里的优先级和分组位置。高质量直接证据会比
    泛泛正文上下文先进入模型上下文。
    """
    evidence_type: str
    source_kind: str
    source_id: str
    chunk_id: str | None
    title: str
    snippet: str
    score: float
    matched_query: str = ""
    source_type: str = ""  # project_source 的 source_type
    chapter_no: int | None = None  # 引用章节（结构化 chunk 来自 metadata）


@dataclass
class GroupedEvidence:
    """按类型分组的证据集合。

    问答 prompt 不是把所有检索结果平铺，而是按证据类型组织。这样模型更容易区分：
    哪些是直接事实，哪些只是背景资料，哪些是否定/不足证据。
    """
    direct_character: list[ClassifiedEvidence] = field(default_factory=list)
    relationship: list[ClassifiedEvidence] = field(default_factory=list)
    ability_table: list[ClassifiedEvidence] = field(default_factory=list)
    worldbuilding: list[ClassifiedEvidence] = field(default_factory=list)
    timeline: list[ClassifiedEvidence] = field(default_factory=list)
    negative: list[ClassifiedEvidence] = field(default_factory=list)
    generic: list[ClassifiedEvidence] = field(default_factory=list)

    def positive_evidence(self) -> list[ClassifiedEvidence]:
        """返回所有非否定证据，按分组顺序。"""
        return (
            self.direct_character
            + self.relationship
            + self.ability_table
            + self.worldbuilding
            + self.timeline
            + self.generic
        )

    def to_prompt_text(self, *, max_chars: int = 18000, per_section: int = 6) -> str:
        """生成分组证据的 prompt 文本，按总字符预算截断。

        - 按分组优先级顺序填充，预算耗尽即停止，保证高优先级证据不被低优先级挤掉。
        - 否定证据只在没有任何正向证据时才包含，避免干扰模型。
        - max_chars: 证据部分的总字符预算（不含 history/web/policy，那些由 ask 流程另计）。
        """
        # 正向证据存在时不主动加入 negative_evidence，避免“没有说明”类句子
        # 干扰模型对已有证据的判断；只有完全没有正向证据时才提示资料不足。
        positive = self.positive_evidence()
        parts: list[str] = []

        sections = [
            ("人物直接证据", self.direct_character),
            ("关系证据", self.relationship),
            ("技能表 / 能力设定", self.ability_table),
            ("世界观设定", self.worldbuilding),
            ("时间线 / 事件", self.timeline),
            ("普通资料", self.generic),
        ]

        # 预留 section 标题开销（"## xxx\n" 约 8 字符）与分隔符
        overhead_per_section = 10
        used = 0
        for label, items in sections:
            if not items:
                continue
            if used + overhead_per_section >= max_chars:
                break
            lines = [f"## {label}"]
            added = 0
            for item in items[:per_section]:
                snippet = item.snippet[:600]
                line = f"- [{item.title}] {snippet}"
                if used + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                used += len(line) + 1
                added += 1
            if added:
                parts.append("\n".join(lines))
                used += overhead_per_section

        # 否定证据仅在无正向证据时才出现
        if not positive and self.negative:
            if used + overhead_per_section < max_chars:
                lines = ["## 否定或限制性证据"]
                for item in self.negative[:3]:
                    snippet = item.snippet[:600]
                    line = f"- [{item.title}] {snippet}"
                    if used + len(line) + 1 > max_chars:
                        break
                    lines.append(line)
                    used += len(line) + 1
                if len(lines) > 1:
                    parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else "（未找到相关证据）"

    def to_citations(self) -> list[dict]:
        """生成 citations 列表（不含否定证据）。"""
        citations: list[dict] = []
        seen: set[str] = set()
        for item in self.positive_evidence():
            key = f"{item.source_kind}:{item.source_id}:{item.chunk_id or ''}"
            if key in seen:
                continue
            seen.add(key)
            citations.append({
                "source_kind": item.source_kind,
                "source_id": item.source_id,
                "chunk_id": item.chunk_id,
                "title": item.title,
                "snippet": item.snippet[:300],
                "evidence_type": item.evidence_type,
                "matched_query": item.matched_query,
                "score": item.score,
                "chapter_no": item.chapter_no,
            })
        return citations[:15]


# ── 证据分类 ────────────────────────────────────────────

def _is_ability_table_shape(content: str, title: str) -> bool:
    """判断片段是否真技能表（表格结构），而非正文里恰好出现阶位词。

    特征：管道分隔的表格行（"11 | 冰系 | 初阶 | 冰蔓"）、表头词（序号/阶位/
    技能名称）、或标题明确含"技能表"。博城篇原文每章都提"初阶魔法师"，
    但那是正文不是表格——正文片段不得被误判为技能表。
    """
    return (
        "|" in content
        or "技能表" in title
        or bool(re.search(r"(序号|阶位|技能名称|法术名称|技能描述)", content))
    )


def _classify_evidence(result: dict, intent: str, entities: list[str], attributes: list[str]) -> str:
    """根据内容特征和查询意图，将单条检索结果分类。

    分类不是为了“证明答案”，而是为了控制 prompt 组织方式和排序优先级。
    例如人物能力问题应优先看 direct_character_evidence，而不是世界观背景段落。
    """
    content = (result.get("snippet", "") or "").lower()
    title = (result.get("title", "") or "").lower()
    source_type = result.get("source_type", "")
    snippet_raw = result.get("snippet", "") or ""
    clauses = _split_clauses(snippet_raw)

    # 1. 否定证据：按句子语义判断，不能按整块是否含人名判断。
    # "资料中没有说明莫凡的土系表现，无法确定他是否拥有土系"这类缺失/否定
    # 声明句，即使含查询实体也判 negative（用户问什么、资料就没写什么）；
    # 而"张英璐被杀死，没有反抗"这类实体句本身是正向事实的片段不判 negative
    # （"没有反抗"只是另一子句的否定，实体句"张英璐被杀死"仍是直接证据）。
    neg_clauses = [cl for cl in clauses if any(t in cl.lower() for t in NEGATION_TERMS)]
    if neg_clauses:
        focus_clauses = [
            cl for cl in clauses
            if (entities and any(e.lower() in cl.lower() for e in entities))
            or (attributes and any(a.lower() in cl.lower() for a in attributes))
        ]
        if focus_clauses:
            # 实体/属性所在的子句全部是否定句 → 整条证据是"缺失声明"，判 negative
            if all(cl in neg_clauses for cl in focus_clauses):
                return "negative_evidence"
        elif len(neg_clauses) >= 2:
            # 实体/属性完全没出现，纯否定/缺失背景 → 高置信否定
            return "negative_evidence"

    # 设定类条目特征：reference/note 来源、标题含设定词、或内容带设定语气。
    # 剧情正文（博城篇含"灾难/大战/事件"等词）不是设定，不得判世界观——
    # 否则"莫凡在博城篇达到了什么水平"的正文证据全被归为 worldbuilding_entry。
    is_setting_entry = (
        source_type in ("reference", "note")
        or "设定" in title or "世界观" in title or "规则" in title
        or any(m in content for m in ("设定：", "设定是", "世界观", "本世界", "这个世界"))
    )

    # 2. 世界观设定（仅设定类条目）
    if is_setting_entry:
        if not _is_ability_table_shape(content, title):
            # 设定类但不是技能表
            if intent in ("worldbuilding", "plot_event"):
                return "worldbuilding_entry"

    # 3. 技能表：必须有表格结构（管道分隔行 / 表头词 / 标题含"技能表"）。
    # 博城篇原文大量出现"初阶/中阶/高阶"字样，若仅凭阶位词计数就判技能表，
    # 会把全部正文片段打成 ability_table，真正的剧情证据进不了 prompt
    # （"莫凡在博城篇达到了什么水平"被技能表罗列带偏、时间线答错）。
    table_hits = sum(1 for t in ABILITY_TABLE_TERMS if t in content)
    is_table = _is_ability_table_shape(content, title)
    if is_table and (table_hits >= 1 or "技能表" in title):
        return "ability_table"

    # 4. 关系证据
    rel_hits = sum(1 for t in RELATIONSHIP_TERMS if t in content)
    if rel_hits >= 1 and intent == "relationship":
        # 关系意图下，检查是否两个实体同段出现
        if len(entities) >= 2:
            both_present = all(e in content for e in entities[:2])
            if both_present:
                return "relationship_evidence"
        if rel_hits >= 2:
            return "relationship_evidence"

    # 5. 人物直接证据（目标人物 + 属性就近绑定）
    # 不再仅凭"片段同时含人物和属性"判定，必须【目标实体】与属性子句级就近绑定，
    # 否则"赵满延的光系魔法保护张小侯"会被误归因为张小侯会光系。
    # 目标实体取 entities[0]（查询主语）；若其它实体与属性绑定，说明属性不属于目标，降级。
    if entities and attributes:
        entity_present = any(e.lower() in content for e in entities)
        attr_present = any(a.lower() in content for a in attributes)
        if entity_present and attr_present and not is_table:
            known = tuple(entities)
            target = entities[0]
            others = [e for e in entities[1:] if e]

            # 目标实体是否与任一属性就近绑定
            target_bound = any(
                _is_entity_attr_bound(target, a, cl, known_entities=known)
                for a in attributes for cl in clauses
            )
            if target_bound:
                return "direct_character_evidence"

            # 目标未绑定：若其它实体与属性绑定了，说明属性属于旁人 → 旁人相关
            other_bound = any(
                _is_entity_attr_bound(o, a, cl, known_entities=known)
                for o in others for a in attributes for cl in clauses
            )
            if other_bound:
                return "relationship_evidence"
            # 同段出现但无任何就近绑定 → 普通正文，避免误归因
            return "generic_context"

    # 6. 人物直接证据（人物资料类，无明确属性绑定时的处理）
    # character_profile（"张小侯家庭"、"许昭霆是谁"）：无属性时含查询主语实体的
    # 片段即该人物的直接相关证据，避免配角片段被 ability_table/worldbuilding
    # 兜底类型挤出 citations；
    # character_ability 但 attributes 为空或字面未命中时：只有 snippet 含
    # 能力语义词（法系/技能/战斗）才判直接证据——"许昭霆雷电实力如何"的
    # 原文实战（"尝我的雷印！！…雷弧…"）不含"雷电能力/实力"字面但明显是
    # 能力内容；"张小侯站在城墙之上望着敌军"（纯场景）仍降级 generic，
    # 避免无关片段伪装成能力直接证据。
    if intent in ("character_profile", "character_ability") and entities:
        if any(e.lower() in content for e in entities):
            if not is_table and (
                intent == "character_profile"
                or any(t in content for t in ABILITY_CONTEXT_TERMS)
            ):
                return "direct_character_evidence"

    # 7. 时间线 / 事件
    if intent in ("timeline", "plot_event"):
        return "timeline_event"

    # 8. 世界观设定（兜底：仅设定类条目，剧情正文不判）
    if is_setting_entry and any(t in content for t in WORLDBUILDING_TERMS):
        return "worldbuilding_entry"

    # 9. 技能表（兜底，同样要求表格结构）
    if is_table and table_hits >= 1:
        return "ability_table"

    # 10. 关系证据（兜底）
    if rel_hits >= 2:
        return "relationship_evidence"

    return "generic_context"


# ── 重排优先级 ────────────────────────────────────────────

def _rerank_priority(evidence_type: str, intent: str) -> int:
    """返回排序优先级（越小越优先）。

    不同 intent 的高价值证据类型不同：关系问题优先 relationship_evidence，
    人物能力问题优先 direct_character_evidence，世界观问题则更看重设定条目。
    """
    # 人物能力问题
    if intent in ("character_ability", "character_profile"):
        priority_map = {
            "direct_character_evidence": 0,
            "timeline_event": 1,
            "ability_table": 2,
            "worldbuilding_entry": 3,
            "generic_context": 4,
            "relationship_evidence": 5,
            "negative_evidence": 10,
        }
        return priority_map.get(evidence_type, 5)

    # 反向属性问题
    if intent == "character_by_ability":
        priority_map = {
            "direct_character_evidence": 0,
            "generic_context": 1,
            "worldbuilding_entry": 2,
            "ability_table": 3,
            "timeline_event": 4,
            "relationship_evidence": 5,
            "negative_evidence": 10,
        }
        return priority_map.get(evidence_type, 5)

    # 关系问题
    if intent == "relationship":
        priority_map = {
            "relationship_evidence": 0,
            "direct_character_evidence": 1,
            "generic_context": 2,
            "timeline_event": 3,
            "worldbuilding_entry": 4,
            "ability_table": 5,
            "negative_evidence": 10,
        }
        return priority_map.get(evidence_type, 5)

    # 默认
    return {
        "direct_character_evidence": 0,
        "relationship_evidence": 1,
        "ability_table": 2,
        "worldbuilding_entry": 3,
        "timeline_event": 4,
        "generic_context": 5,
        "negative_evidence": 10,
    }.get(evidence_type, 5)


# ── 主入口 ────────────────────────────────────────────────

def clean_query_entities(entities: list[str] | None) -> list[str]:
    """清洗查询实体：剔除"X系"法系词（规则路径下 v2_plan.entities 可能混入），
    避免把法系词当作"第三人人名"参与就近绑定判定；全部被剔除时保留原样。"""
    entities = [e for e in (entities or []) if e]
    cleaned = [e for e in entities if not e.endswith("系")]
    return cleaned if cleaned else entities


async def retrieve_and_classify(
    db: AsyncSession,
    project_id: str,
    plan,  # KnowledgeQueryPlan (V1 兼容)
    *,
    intent: str = "general",
    entities: list[str] | None = None,
    attributes: list[str] | None = None,
    include_structured: bool = True,
    limit: int = 12,
) -> tuple[list[dict], list[dict], GroupedEvidence]:
    """检索 + 分类 + 重排 + 分组。

    返回 (structured_results, chunk_results, grouped)。
    structured_results 和 chunk_results 保持原始格式供兼容使用。
    grouped 是分类后的证据分组供 prompt 使用。

    这是 ask_knowledge_question 的证据入口。它先复用 knowledge_source 的检索结果，
    再在本层做“证据质量判断”，避免问答 prompt 只是低质量片段列表。
    """
    from services.knowledge_source import search_project_knowledge_with_plan

    entities = entities or plan.entities
    attributes = attributes or []
    # 清洗：规则路径下 v2_plan.entities 可能混入"X系"法系词，剔除后才是干净人名，
    # 避免把法系词当作"第三人人名"参与就近绑定判定。
    entities = clean_query_entities(entities)

    # 1. 复用现有检索：include_structured=True 时先查正式结构化表，再查资料切片。
    if include_structured:
        structured_results, chunk_results = await search_project_knowledge_with_plan(
            db, project_id, plan, limit=limit,
        )
    else:
        # 资料问答（ask）：仅检索上传资料切片（写作域隔离），但执行 Query Planner
        # 已生成的多条子查询（plan.search_queries，最多 3 条，不额外调用 LLM），
        # 按 chunk_id 合并去重、保留最高分/最完整 snippet，再按分数重排。
        # 避免单次检索被某个子主题带偏，丢失铁笼、结局等关联证据。
        structured_results, chunk_results = await search_project_knowledge_with_plan(
            db, project_id, plan, limit=limit, include_structured=False,
        )

    # 2. 分类：结构化结果和 chunk 结果统一变成 ClassifiedEvidence，后续统一排序/分组。
    all_classified = _classify_results(
        structured_results, chunk_results, intent, entities, attributes,
    )

    # 3. 重排：按类型优先级 + 原始分数。
    # 类型优先级保证“高可信证据”靠前，原始分数保证同类证据中更相关的靠前。
    all_classified.sort(
        key=lambda e: (_rerank_priority(e.evidence_type, intent), -e.score),
    )

    # 3.5 每 source 最多保留 MAX_CHUNKS_PER_SOURCE 条，避免单个长资料淹没证据池。
    #     结构化条目（character/outline/world_entry 等 chunk_id 为空）不受此限。
    source_chunk_counts: dict[str, int] = {}
    capped: list[ClassifiedEvidence] = []
    for item in all_classified:
        if item.chunk_id:
            key = item.source_id
            if source_chunk_counts.get(key, 0) >= MAX_CHUNKS_PER_SOURCE:
                continue
            source_chunk_counts[key] = source_chunk_counts.get(key, 0) + 1
        capped.append(item)
    all_classified = capped

    # 4. 分组
    grouped = _group_evidence(all_classified)

    return structured_results, chunk_results, grouped


def _classify_results(
    structured_results: list[dict],
    chunk_results: list[dict],
    intent: str,
    entities: list[str],
    attributes: list[str],
) -> list[ClassifiedEvidence]:
    """把检索结果（结构化 + chunk 字典）统一分类为 ClassifiedEvidence。"""
    all_classified: list[ClassifiedEvidence] = []

    for r in structured_results:
        ev_type = _classify_evidence(r, intent, entities, attributes)
        all_classified.append(ClassifiedEvidence(
            evidence_type=ev_type,
            source_kind=r.get("source_kind", "unknown"),
            source_id=str(r.get("source_id", "")),
            chunk_id=str(r.get("chunk_id", "")) if r.get("chunk_id") else None,
            title=r.get("title", ""),
            snippet=r.get("snippet", "")[:600],
            score=r.get("score", 0.0),
            matched_query=r.get("matched_query", ""),
            source_type=r.get("source_type", ""),
            chapter_no=r.get("chapter_no"),
        ))

    for r in chunk_results:
        ev_type = _classify_evidence(r, intent, entities, attributes)
        all_classified.append(ClassifiedEvidence(
            evidence_type=ev_type,
            source_kind=r.get("source_kind", "unknown"),
            source_id=str(r.get("source_id", "")),
            chunk_id=str(r.get("chunk_id", "")) if r.get("chunk_id") else None,
            title=r.get("title", ""),
            snippet=r.get("snippet", "")[:600],
            score=r.get("score", 0.0),
            matched_query=r.get("matched_query", ""),
            source_type=r.get("source_type", ""),
            chapter_no=r.get("chapter_no"),
        ))

    return all_classified


def _group_evidence(all_classified: list[ClassifiedEvidence]) -> GroupedEvidence:
    """按证据类型分组。"""
    grouped = GroupedEvidence()
    for item in all_classified:
        if item.evidence_type == "direct_character_evidence":
            grouped.direct_character.append(item)
        elif item.evidence_type == "relationship_evidence":
            grouped.relationship.append(item)
        elif item.evidence_type == "ability_table":
            grouped.ability_table.append(item)
        elif item.evidence_type == "worldbuilding_entry":
            grouped.worldbuilding.append(item)
        elif item.evidence_type == "timeline_event":
            grouped.timeline.append(item)
        elif item.evidence_type == "negative_evidence":
            grouped.negative.append(item)
        else:
            grouped.generic.append(item)
    return grouped


# ── 证据充分性判断 + 有上限的补查 ─────────────────────────

@dataclass
class SufficiencyReport:
    """第一轮多查询检索后的证据充分性报告（规则型，不调用 LLM）。"""
    sufficient: bool
    missing_entities: list[str]      # 未被任何正向证据覆盖的查询实体
    missing_attributes: list[str]    # 无对应句子覆盖的目标属性（地点/结局/实力等）
    has_exact_hit: bool              # 是否至少一条证据中实体与核心词同句（非顺带提及）
    positive_count: int              # 正向证据总数


def _question_core_terms(plan) -> list[str]:
    """补查/精确命中用的核心词：优先 planner 的属性与 required_terms，回退问题本身。

    V1 规则 plan 没有 attributes 字段（只有 V2 LLM plan 有），统一用 getattr 兼容。
    """
    terms = [a for a in (getattr(plan, "attributes", None) or []) if a]
    for t in (getattr(plan, "required_terms", None) or []):
        if t and t not in terms:
            terms.append(t)
    return terms


def check_evidence_sufficiency(plan, grouped: GroupedEvidence) -> SufficiencyReport:
    """判断第一轮证据是否充分（人物覆盖 / 属性句 / 精确命中 / 顺带提及）。

    全部满足才算充分：
    1. 每个查询实体至少出现在一条正向证据的 snippet 中；
    2. 每个目标属性（地点/结局/实力等）至少有一条证据句子覆盖；
    3. 至少一条证据中查询主语实体与核心词同子句（排除"只是被顺带提及"）；
    4. 存在至少一条正向证据。
    任一不满足 → 返回 insufficient，由 ask 流程执行一次有上限的补查。
    """
    entities = [e for e in (plan.entities or []) if e]
    core_terms = _question_core_terms(plan)
    positive = grouped.positive_evidence()
    blob = " ".join(ev.snippet.lower() for ev in positive)

    missing_entities = [e for e in entities if e.lower() not in blob]
    missing_attributes = [a for a in core_terms if a.lower() not in blob]

    # 精确命中：实体与任一核心词出现在同一条证据的同一子句（就近，非全篇散落）。
    has_exact_hit = False
    if entities:
        e0 = entities[0].lower()
        for ev in positive:
            for cl in _split_clauses(ev.snippet):
                cll = cl.lower()
                if e0 in cll and any(t.lower() in cll for t in core_terms):
                    has_exact_hit = True
                    break
            if has_exact_hit:
                break
    else:
        # 无查询实体（general 意图）：只要有正向证据即视为覆盖
        has_exact_hit = len(positive) > 0

    sufficient = (
        len(positive) > 0
        and not missing_entities
        and not missing_attributes
        and has_exact_hit
    )
    return SufficiencyReport(
        sufficient=sufficient,
        missing_entities=missing_entities,
        missing_attributes=missing_attributes,
        has_exact_hit=has_exact_hit,
        positive_count=len(positive),
    )


def build_supplemental_queries(plan, report: SufficiencyReport) -> list[str]:
    """规则生成补充查询（最多 2 条），用于一次有上限的补查。

    不调用 LLM：基于"原问题 + 第一轮缺失维度"组合拼接，天然有界；
    补查只执行一轮，禁止循环。

    缺失实体/属性之外，**无精确命中**（has_exact_hit=False，证据全是顺带提及）
    同样触发补查——否则"许昭霆雷电实力如何"召回的只是"雷系座位上/莫凡怎么
    这么变态"这类提及片段，关键实战（雷印战幽狼兽）永远进不了证据池。
    """
    entities = [e for e in (plan.entities or []) if e]
    core_terms = _question_core_terms(plan)
    qs: list[str] = []

    # 缺失实体 → 实体 + 核心词（属性/必含词优先）
    for e in report.missing_entities[:2]:
        suffix = core_terms[0] if core_terms else plan.original_question
        qs.append(f"{e} {suffix}")

    # 缺失属性 → 查询主语 + 属性
    for a in report.missing_attributes[:1]:
        prefix = entities[0] if entities else ""
        qs.append(f"{prefix} {a}".strip() or a)

    # 无精确命中（全是顺带提及）→ 用原问题核心词 + 实体重新组合，
    # 换一组子查询打捞被 top-N 截掉的实战片段
    if report.has_exact_hit is False and not qs:
        anchor = entities[0] if entities else ""
        terms = [t for t in core_terms if t and t != anchor]
        if anchor and terms:
            qs.append(f"{anchor} {terms[0]}")
            if len(terms) > 1:
                qs.append(f"{anchor} {terms[1]}")

    seen: set[str] = set()
    out: list[str] = []
    for q in qs:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:2]

