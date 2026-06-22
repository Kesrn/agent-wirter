"""结构化问答路由（文档 §13）。

三类问题：
- character_ability：人物有什么系/能力 → 查 ability_profile，回退 project_knowledge_facts
- event_query：发生了什么/主要事件 → 查 event_timeline
- world_rule_query：规则/体系/等级 → 查 world_rule

查询优先级（文档约定）：
  manual > fanfic > ability_profile(LLM) > project_knowledge_facts(规则) > RAG fallback
禁止 LLM 凭常识补充，查不到明确说未找到。

返回格式兼容现有知识库 QA（answer/citations/query_plan/retrieval_stats）。
"""

from __future__ import annotations

import logging
import uuid
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from models.structured_knowledge import (
    CharacterProfile, AbilityProfile, EventTimeline, WorldRule,
)
from services.magic_systems import canonical_magic_system_name

logger = logging.getLogger(__name__)


def _like_escape(value: str) -> str:
    """转义 SQL LIKE 通配符，使 % 和 _ 仅作字面量匹配。

    反斜杠本身先转义，再转义 % 与 _。配合 column.like(pattern, escape='\\\\') 使用。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_chinese_int(value: str) -> int | None:
    """Parse simple Chinese numerals used in chapter references."""
    if not value:
        return None
    if value.isdigit():
        return int(value)

    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    section = 0
    seen = False
    for ch in value:
        if ch in digits:
            section = digits[ch]
            seen = True
        elif ch in units:
            seen = True
            unit = units[ch]
            if section == 0:
                section = 1
            total += section * unit
            section = 0
        else:
            return None
    return total + section if seen else None


# ── 意图识别（文档 §13 关键词） ─────────────────────────

def _detect_structured_intent(question: str) -> str:
    """识别结构化问答意图。"""
    q = question.strip()
    # character_ability
    if any(kw in q for kw in ("什么系", "什么系别", "什么能力", "掌握了什么", "会什么技能",
                               "有哪些系", "有什么系", "有什么能力", "魔法系别", "什么魔法",
                               "会不会", "会.*吗")):
        return "character_ability"
    # "X会Y吗" 这类是非问句：用正则单独匹配（上面的子串匹配覆盖不到中间夹着能力名的情形）
    import re
    if re.search(r"[\u4e00-\u9fff]{2,4}会[\u4e00-\u9fff]{1,8}吗", q):
        return "character_ability"
    # world_rule_query
    if any(kw in q for kw in ("规则", "体系", "等级", "设定", "怎么划分", "是什么制度",
                               "修炼境界", "魔法等级", "等级划分")):
        return "world_rule_query"
    # event_query
    if any(kw in q for kw in ("发生了什么", "主要事件", "经过", "结果", "影响",
                               "事件", "剧情", "发生了")):
        return "event_query"
    return "unknown"


def _extract_character_name(question: str) -> str | None:
    """从问题中提取人物名（2-4 汉字，出现在问句里）。"""
    import re
    # 找"X的""X有什么""X会"等模式
    patterns = [
        r"([\u4e00-\u9fff]{2,4})(?:的|有什么|有什么系|会什么|掌握了什么|是什么系|是什么)",
        r"([\u4e00-\u9fff]{2,4})有什么能力",
        r"([\u4e00-\u9fff]{2,4})会[\u4e00-\u9fff]{1,8}吗",
    ]
    for p in patterns:
        m = re.search(p, question)
        if m:
            name = m.group(1)
            # 排除明显非人名
            if name not in ("什么", "怎么", "哪些", "这些", "那个", "这个"):
                return name
    return None


def _character_name_variants(name: str) -> list[str]:
    """Generate a few lightweight variants for common name typos.

    复用 merge 层的 normalize_character_name，保证 QA 查询口径与正式表一致：
    若 name 属于已知异体簇，返回 规范名 + 全部异体；否则做最小侯/候、去小变体。
    """
    from services.extraction_normalization import normalize_character_name

    canonical, cluster_aliases = normalize_character_name(name)
    bases = [canonical] + [a for a in cluster_aliases if a != canonical]
    if not cluster_aliases:
        # 无已知簇时保留原有的轻量变体逻辑（侯/候、去小）
        bases = [name]
        if len(name) == 3 and name[1] == "小" and name[2] != "小":
            bases.append(name[0] + name[2])

    variants: list[str] = []
    for base in bases:
        for candidate in (base, base.replace("侯", "候"), base.replace("候", "侯")):
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _extract_system_name(question: str) -> str | None:
    """从问题中提取法系名（X系）。"""
    import re
    m = re.search(r"([\u4e00-\u9fff]{1,4}系)", question)
    if m:
        return m.group(1)
    return None


def _canonical_magic_system_name(name: str) -> str | None:
    """Return canonical X系 name from noisy model labels such as 雷霆系魔法/雷系星尘."""
    return canonical_magic_system_name(name)


# ── 主入口 ───────────────────────────────────────────────

async def answer_structured_question(
    db: AsyncSession,
    project_id: str,
    question: str,
    *,
    conversation_id: str | None = None,
) -> dict:
    """结构化问答主入口。返回兼容现有 QA 格式。"""
    pid = str(project_id)
    intent = _detect_structured_intent(question)

    if intent == "character_ability":
        return await _answer_character_ability(db, pid, question, intent, conversation_id)
    if intent == "event_query":
        return await _answer_event_query(db, pid, question, intent, conversation_id)
    if intent == "world_rule_query":
        return await _answer_world_rule(db, pid, question, intent, conversation_id)

    # 未识别意图
    return _empty_result(intent, conversation_id, "无法识别问题类型，请尝试询问人物能力、事件或世界规则。")


# ── character_ability ───────────────────────────────────

async def _answer_character_ability(
    db: AsyncSession, pid: str, question: str, intent: str,
    conversation_id: str | None,
) -> dict:
    character = _extract_character_name(question)
    if not character:
        return _empty_result(intent, conversation_id,
                             "请明确指定要查询的人物名称，例如「莫凡有什么系」。")

    # 是非问句："X会Y吗" → 检查 Y 是否在 X 的能力中
    import re
    yes_no_match = re.search(r"会([\u4e00-\u9fff]{1,8})吗", question)
    queried_ability = yes_no_match.group(1) if yes_no_match else None

    variants = _character_name_variants(character)
    result = await db.execute(
        select(AbilityProfile)
        .where(AbilityProfile.project_id == pid)
        .where(AbilityProfile.character_name.in_(variants))
        .order_by(AbilityProfile.source_priority.desc(), AbilityProfile.confidence.desc())
    )
    abilities = list(result.scalars().all())

    # 是非问句：检查特定能力是否存在
    if queried_ability:
        from services.magic_systems import canonical_magic_system_name
        queried_canonical = canonical_magic_system_name(queried_ability) or queried_ability
        matched = []
        for a in abilities:
            a_canonical = canonical_magic_system_name(a.ability_name) or a.ability_name
            if queried_ability in a.ability_name or a_canonical == queried_canonical:
                matched.append(a)
        if matched:
            best = matched[0]
            answer = (f"根据结构化知识库，**{character}** 会「{queried_ability}」。")
            citations = [{
                "source_kind": "structured_fact",
                "source_id": str(best.source_id) if best.source_id else None,
                "chunk_id": None,
                "chapter_no": best.first_seen_chapter,
                "title": f"{best.character_name} - {best.ability_name}",
                "snippet": (best.evidence[0] if best.evidence else "")[:300],
                "evidence_type": "character_ability_fact",
                "matched_query": f"{character} -> {queried_ability}",
                "score": best.confidence,
                "table": "ability_profile",
            }] if best.source_id else []
            return {
                "answer": answer,
                "citations": citations,
                "query_plan": {"mode": "structured", "intent": intent, "tables": ["ability_profile"]},
                "retrieval_stats": {"structured_hits": len(matched), "vector_hits": 0},
                "conversation_id": conversation_id or "",
            }
        # 不在能力列表中 → 明确说当前资料不支持
        return {
            "answer": f"当前结构化资料不支持 **{character}** 会「{queried_ability}」。",
            "citations": [],
            "query_plan": {"mode": "structured", "intent": intent, "tables": ["ability_profile"]},
            "retrieval_stats": {"structured_hits": 0, "vector_hits": 0},
            "conversation_id": conversation_id or "",
        }

    # ability_profile 无记录 → 回退 project_knowledge_facts
    wants_magic_system = any(term in question for term in ("什么系", "哪些系", "有什么系", "魔法系别"))
    has_magic_system = any(a.ability_type == "magic_element" for a in abilities)
    if not abilities or (wants_magic_system and not has_magic_system):
        facts_citations, facts_answer = await _fallback_to_facts(db, pid, variants)
        if facts_answer:
            return {
                "answer": facts_answer,
                "citations": facts_citations,
                "query_plan": {"mode": "structured", "intent": intent, "tables": ["project_knowledge_facts"]},
                "retrieval_stats": {"structured_hits": len(facts_citations), "vector_hits": 0},
                "conversation_id": conversation_id or "",
            }
        return _empty_result(intent, conversation_id)

    display_rows: list[tuple[AbilityProfile, str]] = []
    for ability in abilities:
        display_name = ability.ability_name
        if wants_magic_system:
            if ability.ability_type != "magic_element":
                continue
            canonical = _canonical_magic_system_name(ability.ability_name)
            if not canonical:
                continue
            display_name = canonical
        display_rows.append((ability, display_name))

    if wants_magic_system and not display_rows:
        facts_citations, facts_answer = await _fallback_to_facts(db, pid, variants)
        if facts_answer:
            return {
                "answer": facts_answer,
                "citations": facts_citations,
                "query_plan": {"mode": "structured", "intent": intent, "tables": ["project_knowledge_facts"]},
                "retrieval_stats": {"structured_hits": len(facts_citations), "vector_hits": 0},
                "conversation_id": conversation_id or "",
            }
        return _empty_result(intent, conversation_id)

    # 按能力展示名去重。answer 和 citations 必须用同一批去重后的事实，
    # 否则同一能力多条来源会在引用区刷屏。
    from collections import defaultdict
    best_by_key: dict[tuple[str, ...], tuple[AbilityProfile, str]] = {}
    for a, display_name in display_rows:
        key = (a.ability_type, display_name) if wants_magic_system else (
            a.character_name, a.ability_type, display_name,
        )
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = (a, display_name)
            continue
        current_ability = current[0]
        current_score = (
            current_ability.source_priority or 0,
            current_ability.confidence or 0,
            1 if current_ability.evidence else 0,
        )
        new_score = (
            a.source_priority or 0,
            a.confidence or 0,
            1 if a.evidence else 0,
        )
        if new_score > current_score:
            best_by_key[key] = (a, display_name)
    deduped_abilities = list(best_by_key.values())

    # 按 ability_type 分组
    grouped: dict[str, list[tuple[AbilityProfile, str]]] = defaultdict(list)
    for a, display_name in deduped_abilities:
        grouped[a.ability_type].append((a, display_name))

    lines = [f"根据结构化知识库，**{character}** 的能力如下：\n"]
    type_labels = {
        "magic_element": "魔法系别", "spell": "技能/法术",
        "cultivation_level": "境界", "martial_art": "武技",
        "item": "物品", "bloodline": "血脉", "skill": "技能", "unknown": "其它",
    }
    for atype, items in grouped.items():
        label = type_labels.get(atype, atype)
        names = "、".join(display_name for _, display_name in items)
        lines.append(f"- **{label}**：{names}")

    answer = "\n".join(lines)
    citations = [
        {
            "source_kind": "structured_fact",
            "source_id": str(a.source_id) if a.source_id else None,
            "chunk_id": None,
            "chapter_no": a.first_seen_chapter,
            "title": f"{a.character_name} - {display_name}",
            "snippet": (a.evidence[0] if a.evidence else "")[:300],
            "evidence_type": "character_ability_fact",
            "matched_query": f"{a.character_name} -> {display_name}",
            "score": a.confidence,
            "table": "ability_profile",
        }
        for a, display_name in deduped_abilities if a.source_id
    ]
    return {
        "answer": answer,
        "citations": citations,
        "query_plan": {"mode": "structured", "intent": intent, "tables": ["ability_profile"]},
        "retrieval_stats": {"structured_hits": len(deduped_abilities), "vector_hits": 0},
        "conversation_id": conversation_id or "",
    }


async def _fallback_to_facts(
    db: AsyncSession, pid: str, characters: list[str],
) -> tuple[list[dict], str | None]:
    """ability_profile 无记录时，回退查 project_knowledge_facts。"""
    try:
        from services.knowledge_fact_index import query_character_system_facts
        facts = []
        seen: set[tuple[str, str]] = set()
        for character in characters:
            for fact in await query_character_system_facts(db, pid, subject=character, limit=50):
                key = (fact.subject, fact.object)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(fact)
        if not facts:
            return [], None
        character = characters[0]
        systems = list(dict.fromkeys(f.object for f in facts))
        lines = "\n".join(f"- {s}" for s in systems[:12])
        answer = f"根据规则事实索引，**{character}** 明确绑定的法系有：\n\n{lines}"
        citations = [
            {
                "source_kind": "structured_fact",
                "source_id": str(f.source_id),
                "chunk_id": str(f.chunk_id) if f.chunk_id else None,
                "title": (f.metadata_ or {}).get("source_title", "") or "资料",
                "snippet": f.evidence_text[:300],
                "evidence_type": "character_system_fact",
                "matched_query": f"{f.subject} -> {f.object}",
                "score": 1.0,
                "table": "project_knowledge_facts",
            }
            for f in facts
        ]
        return citations, answer
    except Exception:
        logger.exception("fallback_to_facts 失败")
        return [], None


# ── event_query ─────────────────────────────────────────

async def _answer_event_query(
    db: AsyncSession, pid: str, question: str, intent: str,
    conversation_id: str | None,
) -> dict:
    # 是否指定章节
    import re
    chapter_match = re.search(r"第([\d]+|[零〇一二两三四五六七八九十百千万]+)章", question)
    chapter_no = None
    if chapter_match:
        chapter_no = _parse_chinese_int(chapter_match.group(1))

    stmt = select(EventTimeline).where(EventTimeline.project_id == pid)
    if chapter_no:
        stmt = stmt.where(EventTimeline.chapter_no == chapter_no)
    # 关键词匹配 event_title / event_desc
    else:
        # 去除问题里的常见疑问词，剩余作为关键词
        kws = [w for w in re.split(r"[？?\s，,。]+", question) if len(w) >= 2]
        if kws:
            conditions = []
            for kw in kws[:5]:
                pattern = f"%{_like_escape(kw)}%"
                conditions.append(EventTimeline.event_title.like(pattern, escape="\\"))
                conditions.append(EventTimeline.event_desc.like(pattern, escape="\\"))
            if conditions:
                stmt = stmt.where(or_(*conditions))
    stmt = stmt.order_by(EventTimeline.chapter_no.asc().nullslast(),
                         EventTimeline.importance.desc()).limit(20)

    result = await db.execute(stmt)
    events = list(result.scalars().all())
    if not events:
        return _empty_result(intent, conversation_id)

    lines = [f"根据结构化知识库，相关事件如下：\n"]
    for e in events:
        ch = f"（第{e.chapter_no}章）" if e.chapter_no else ""
        lines.append(f"- {e.event_title}{ch}：{e.event_desc or ''}")

    answer = "\n".join(lines)
    citations = [
        {
            "source_kind": "structured_fact",
            "source_id": str(e.source_id) if e.source_id else None,
            "chunk_id": None,
            "chapter_no": e.chapter_no,
            "title": e.event_title,
            "snippet": (e.evidence[0] if e.evidence else "")[:300],
            "evidence_type": "event_timeline_fact",
            "matched_query": e.event_title,
            "score": e.confidence,
            "table": "event_timeline",
        }
        for e in events if e.source_id
    ]
    return {
        "answer": answer,
        "citations": citations,
        "query_plan": {"mode": "structured", "intent": intent, "tables": ["event_timeline"]},
        "retrieval_stats": {"structured_hits": len(events), "vector_hits": 0},
        "conversation_id": conversation_id or "",
    }


# ── world_rule_query ────────────────────────────────────

async def _answer_world_rule(
    db: AsyncSession, pid: str, question: str, intent: str,
    conversation_id: str | None,
) -> dict:
    # priority 是字符串 high/medium/low，用 CASE 映射成数值排序（high>medium>low）
    from sqlalchemy import case
    priority_order = case(
        (WorldRule.priority == "high", 3),
        (WorldRule.priority == "medium", 2),
        (WorldRule.priority == "low", 1),
        else_=0,
    )
    result = await db.execute(
        select(WorldRule)
        .where(WorldRule.project_id == pid)
        .order_by(priority_order.desc(), WorldRule.confidence.desc())
        .limit(30)
    )
    rules = list(result.scalars().all())
    if not rules:
        return _empty_result(intent, conversation_id)

    # 按 category 聚合
    from collections import defaultdict
    grouped: dict[str, list[WorldRule]] = defaultdict(list)
    for r in rules:
        grouped[r.category].append(r)

    lines = ["根据结构化知识库，世界规则如下：\n"]
    for cat, items in grouped.items():
        lines.append(f"### {cat}")
        for r in items:
            lines.append(f"- {r.rule_text}")

    answer = "\n".join(lines)
    citations = [
        {
            "source_kind": "structured_fact",
            "source_id": str(r.source_id) if r.source_id else None,
            "chunk_id": None,
            "chapter_no": r.chapter_no,
            "title": r.category,
            "snippet": (r.evidence[0] if r.evidence else "")[:300],
            "evidence_type": "world_rule_fact",
            "matched_query": r.category,
            "score": r.confidence,
            "table": "world_rule",
        }
        for r in rules if r.source_id
    ]
    return {
        "answer": answer,
        "citations": citations,
        "query_plan": {"mode": "structured", "intent": intent, "tables": ["world_rule"]},
        "retrieval_stats": {"structured_hits": len(rules), "vector_hits": 0},
        "conversation_id": conversation_id or "",
    }


def _empty_result(intent: str, conversation_id: str | None,
                  message: str = "当前结构化知识库中未找到明确记录。") -> dict:
    return {
        "answer": message,
        "citations": [],
        "query_plan": {"mode": "structured", "intent": intent, "tables": []},
        "retrieval_stats": {"structured_hits": 0, "vector_hits": 0},
        "conversation_id": conversation_id or "",
    }
