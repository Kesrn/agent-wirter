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

# 单个 source 最多贡献的 chunk 数，避免一个长资料淹没证据池
MAX_CHUNKS_PER_SOURCE = 8


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
    """按句末标点 + 逗号/分号/换行切分子句，返回去空后的非空片段列表。"""
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
    """分类后的单条证据。"""
    evidence_type: str
    source_kind: str
    source_id: str
    chunk_id: str | None
    title: str
    snippet: str
    score: float
    matched_query: str = ""
    source_type: str = ""  # project_source 的 source_type


@dataclass
class GroupedEvidence:
    """按类型分组的证据集合。"""
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
            })
        return citations[:15]


# ── 证据分类 ────────────────────────────────────────────

def _classify_evidence(result: dict, intent: str, entities: list[str], attributes: list[str]) -> str:
    """根据内容特征和查询意图，将单条检索结果分类。"""
    content = (result.get("snippet", "") or "").lower()
    title = (result.get("title", "") or "").lower()
    source_type = result.get("source_type", "")

    # 1. 否定证据
    neg_count = sum(1 for t in NEGATION_TERMS if t in content)
    if neg_count >= 2:
        return "negative_evidence"

    # 2. 世界观设定（来自设定类 source_type 或内容含设定关键词）
    if source_type in ("reference", "note") or any(t in content for t in WORLDBUILDING_TERMS):
        if not any(t in content for t in ABILITY_TABLE_TERMS):
            # 设定类但不是技能表
            if intent in ("worldbuilding", "plot_event"):
                return "worldbuilding_entry"

    # 3. 技能表
    table_hits = sum(1 for t in ABILITY_TABLE_TERMS if t in content)
    if table_hits >= 2 or "技能表" in title:
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
        if entity_present and attr_present and table_hits < 2:
            known = tuple(entities)
            snippet_raw = result.get("snippet", "") or ""
            clauses = _split_clauses(snippet_raw)
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

    # 6. 人物直接证据（人物资料类，无明确属性绑定时降级）
    # 原：character_ability 意图 + 含人物名 → direct。
    # 问题：无属性时把所有含人物名的片段都当直接证据，易引入与该人物无关的法系片段。
    # 改：仅当确实无法解析出属性时保留为 generic（人物资料相关但不构成能力绑定）。
    if intent in ("character_profile", "character_ability") and entities:
        if any(e.lower() in content for e in entities):
            if table_hits < 2:
                return "generic_context"

    # 7. 时间线 / 事件
    if intent in ("timeline", "plot_event"):
        return "timeline_event"

    # 8. 世界观设定（兜底）
    if any(t in content for t in WORLDBUILDING_TERMS):
        return "worldbuilding_entry"

    # 9. 技能表（兜底）
    if table_hits >= 1:
        return "ability_table"

    # 10. 关系证据（兜底）
    if rel_hits >= 2:
        return "relationship_evidence"

    return "generic_context"


# ── 重排优先级 ────────────────────────────────────────────

def _rerank_priority(evidence_type: str, intent: str) -> int:
    """返回排序优先级（越小越优先）。"""
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
    """
    from services.knowledge_source import search_project_knowledge_with_plan

    entities = entities or plan.entities
    attributes = attributes or []
    # 清洗：规则路径下 v2_plan.entities 可能混入"X系"法系词，剔除后才是干净人名，
    # 避免把法系词当作"第三人人名"参与就近绑定判定。
    clean_entities = [e for e in entities if e and not e.endswith("系")]
    if clean_entities:
        entities = clean_entities

    # 1. 复用现有检索
    if include_structured:
        structured_results, chunk_results = await search_project_knowledge_with_plan(
            db, project_id, plan, limit=limit,
        )
    else:
        from services.knowledge_source import search_project_knowledge
        structured_results = []
        chunk_results = await search_project_knowledge(
            db, project_id, plan.original_question,
            required_terms=plan.required_terms, limit=limit,
        )

    # 2. 分类
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
        ))

    # 3. 重排：按类型优先级 + 原始分数
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

    return structured_results, chunk_results, grouped
