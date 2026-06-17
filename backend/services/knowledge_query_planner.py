"""知识库 Query Planner V2 —— LLM Planner + 规则 fallback。

将用户自然语言问题转为结构化检索计划（V2 schema）。
LLM 优先；失败/低置信度时降级到规则 planner。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── V2 Schema ──────────────────────────────────────────

VALID_INTENTS = (
    "character_profile",
    "character_ability",
    "character_by_ability",
    "relationship",
    "worldbuilding",
    "timeline",
    "plot_event",
    "source_lookup",
    "general",
)


@dataclass
class KnowledgeQueryPlanV2:
    """V2 查询计划，包含 LLM 生成的结构化检索指令。"""

    original_question: str
    rewritten_question: str | None = None
    intent: str = "general"
    entities: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    relation_targets: list[str] = field(default_factory=list)
    time_scope: str | None = None
    sub_queries: list[str] = field(default_factory=list)
    required_terms: list[str] = field(default_factory=list)
    optional_terms: list[str] = field(default_factory=list)
    evidence_policy: str = ""
    answer_policy: str = ""
    confidence: float = 0.0
    planner: str = "rule"  # "llm" or "rule"

    def to_v1(self):
        """转换为 V1 KnowledgeQueryPlan，兼容现有检索流程。"""
        from services.knowledge_query_plan import KnowledgeQueryPlan

        # V2 intent → V1 intent 映射
        intent_map = {
            "character_ability": "ability",
            "character_profile": "character_profile",
            "character_by_ability": "character_by_ability",
            "relationship": "relationship",
            "worldbuilding": "worldbuilding",
            "timeline": "timeline",
            "plot_event": "plot",
            "source_lookup": "general",
            "general": "general",
        }
        v1_intent = intent_map.get(self.intent, "general")
        all_entities = list(dict.fromkeys(self.entities + self.attributes))
        return KnowledgeQueryPlan(
            original_question=self.original_question,
            intent=v1_intent,
            entities=all_entities,
            keywords=self.optional_terms,
            search_queries=self.sub_queries,
            required_terms=self.required_terms,
            facets=[],
        )


# ── Planner Prompt ─────────────────────────────────────

_PLANNER_SYSTEM_PROMPT = """\
你是小说资料问答系统的 Query Planner。
你只负责把用户问题转成检索计划，不回答问题，不编造事实。

要求：
1. 只输出严格 JSON。
2. entities 只放人物、地点、组织等实体。
3. attributes 放能力、法系、身份、阵营、物品等属性。
4. sub_queries 用于资料检索，每条 2-8 个词。
5. required_terms 是必须命中的关键词。
6. optional_terms 是排序加权词。
7. evidence_policy 描述什么证据可以支持结论。
8. answer_policy 描述回答时必须如何保守表达。
9. 如果是追问，结合最近对话补全省略实体。
10. 不确定时 confidence 降低，并保留原问题关键词。

输出 JSON schema:
{
  "rewritten_question": "补全后的完整问题",
  "intent": "character_profile|character_ability|character_by_ability|relationship|worldbuilding|timeline|plot_event|source_lookup|general",
  "entities": ["实体1", "实体2"],
  "attributes": ["属性1"],
  "relation_targets": [],
  "time_scope": null,
  "sub_queries": ["检索词1", "检索词2"],
  "required_terms": ["必须命中词"],
  "optional_terms": ["可选加权词"],
  "evidence_policy": "什么证据可以支持结论",
  "answer_policy": "回答时必须如何保守表达",
  "confidence": 0.85
}"""


def _build_user_prompt(question: str, recent_messages: list[dict] | None = None) -> str:
    """构建 Planner 用户 prompt。"""
    parts: list[str] = []

    if recent_messages:
        parts.append("## 最近对话")
        for msg in recent_messages[-6:]:  # 最多 3 轮
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", ""))[:300]
            parts.append(f"{role}：{content}")
        parts.append("")

    parts.append("## 当前问题")
    parts.append(question)
    parts.append("")

    parts.append("## 已知可用资料类型")
    parts.append("- project_source_chunks（资料切片）")
    parts.append("- characters（角色表）")
    parts.append("- character_events（角色事件）")
    parts.append("- outlines（大纲）")
    parts.append("- world_entries（世界观设定）")
    parts.append("- hidden_threads（暗线）")

    return "\n".join(parts)


# ── JSON 解析 ──────────────────────────────────────────

def _safe_parse_json(text: str) -> dict:
    """从 LLM 响应中安全提取 JSON。"""
    # 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    # 第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ── Plan 校验 ──────────────────────────────────────────

def _validate_plan(data: dict) -> bool:
    """校验 LLM 输出的 plan 是否合法。"""
    if not data:
        return False
    intent = data.get("intent", "")
    if intent not in VALID_INTENTS:
        return False
    entities = data.get("entities", [])
    if not isinstance(entities, list):
        return False
    sub_queries = data.get("sub_queries", [])
    if not isinstance(sub_queries, list):
        return False
    confidence = data.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        return False
    return True


def _dict_to_plan(data: dict, original_question: str) -> KnowledgeQueryPlanV2:
    """将 LLM 输出 dict 转为 KnowledgeQueryPlanV2。"""
    return KnowledgeQueryPlanV2(
        original_question=original_question,
        rewritten_question=data.get("rewritten_question"),
        intent=data.get("intent", "general"),
        entities=[str(e) for e in data.get("entities", []) if e],
        attributes=[str(a) for a in data.get("attributes", []) if a],
        relation_targets=[str(r) for r in data.get("relation_targets", []) if r],
        time_scope=data.get("time_scope"),
        sub_queries=[str(s) for s in data.get("sub_queries", []) if s],
        required_terms=[str(t) for t in data.get("required_terms", []) if t],
        optional_terms=[str(t) for t in data.get("optional_terms", []) if t],
        evidence_policy=str(data.get("evidence_policy", "")),
        answer_policy=str(data.get("answer_policy", "")),
        confidence=float(data.get("confidence", 0)),
        planner="llm",
    )


# ── LLM Planner ────────────────────────────────────────

async def _build_llm_plan(
    question: str,
    recent_messages: list[dict] | None,
    user_id: str | None,
    db: AsyncSession | None,
) -> KnowledgeQueryPlanV2 | None:
    """调用 LLM 生成 query plan。失败返回 None。"""
    if not user_id or not db:
        return None

    try:
        from agents.llm_provider import get_llm_provider
        from api.llm_deps import get_user_llm_config

        llm_config_dict = await get_user_llm_config(user_id, db)
        provider = get_llm_provider(llm_config_dict)

        user_prompt = _build_user_prompt(question, recent_messages)
        result_text = await provider.generate(
            _PLANNER_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.0,
            max_tokens=800,
        )

        data = _safe_parse_json(result_text)
        if _validate_plan(data):
            return _dict_to_plan(data, question)

        logger.warning("LLM planner returned invalid plan: %s", data)
        return None

    except Exception:
        logger.warning("LLM planner failed, falling back to rule planner", exc_info=True)
        return None


# ── Rule Fallback ───────────────────────────────────────

def _build_rule_plan(question: str, recent_messages: list[dict] | None = None) -> KnowledgeQueryPlanV2:
    """基于规则构建 query plan（V2 schema），作为 LLM fallback。"""
    from services.knowledge_query_plan import build_knowledge_query_plan

    v1 = build_knowledge_query_plan(question)

    # 从 V1 映射到 V2
    # V1 的 ability intent 对应 V2 的 character_ability
    intent_map = {
        "ability": "character_ability",
        "character_profile": "character_profile",
        "character_by_ability": "character_by_ability",
        "relationship": "relationship",
        "worldbuilding": "worldbuilding",
        "timeline": "timeline",
        "plot": "plot_event",
        "general": "general",
    }
    v2_intent = intent_map.get(v1.intent, "general")

    # 属性提取：V1 没有 attributes，从 entities 中提取 "X系" 类实体
    attributes = [e for e in v1.entities if e.endswith("系")]
    entities = [e for e in v1.entities if not e.endswith("系")]

    # required_terms 直接映射
    required_terms = list(v1.required_terms)

    # 构建 evidence_policy 和 answer_policy
    evidence_policy = ""
    answer_policy = ""
    if v2_intent == "character_ability":
        evidence_policy = "先找人物与该能力的直接证据；没有直接证据时可使用通用设定，但必须说明两者区别。"
        answer_policy = "区分个人证据和通用设定，不得把通用设定说成该人物已掌握。"
    elif v2_intent == "character_by_ability":
        evidence_policy = "只把明确绑定人物和该能力的片段作为正向证据；技能表不能作为人物归属证据。"
        answer_policy = "列出当前资料明确提到的人物；疑似内容放入待确认。"
    elif v2_intent == "relationship":
        evidence_policy = "双方同段出现且包含关系词的片段优先。"
        answer_policy = "只陈述资料中明确描述的关系。"
    elif v2_intent == "worldbuilding":
        evidence_policy = "设定/参考资料优先，不要误召回纯人物关系片段。"
        answer_policy = "基于资料中的设定内容回答，不要编造。"

    # 追问消解：如果问题含代词，从 recent_messages 提取上文实体补充
    rewritten = None
    if recent_messages and any(p in question for p in ("她", "他", "TA", "ta")):
        for msg in reversed(recent_messages):
            if msg.get("role") == "user":
                hist_plan = build_knowledge_query_plan(str(msg.get("content", "")))
                for entity in hist_plan.entities:
                    if entity not in question:
                        rewritten = re.sub(r"她|他|TA|ta", entity, question, count=1)
                        break
                break

    # 短追问消解："那叶心夏呢？"
    if not rewritten and len(question.strip()) <= 12:
        followup_markers = ("那", "那么", "还有", "呢")
        if any(m in question for m in followup_markers) and recent_messages:
            for msg in reversed(recent_messages):
                if msg.get("role") == "user":
                    hist_plan = build_knowledge_query_plan(str(msg.get("content", "")))
                    if hist_plan.intent == "relationship" and len(hist_plan.entities) >= 2:
                        # 当前追问只提了一个新实体，补全为关系问题
                        current_entities = v1.entities
                        if len(current_entities) == 1 and current_entities[0] not in hist_plan.entities:
                            # 优先选莫凡做锚点，否则选第一个不是当前追问实体的
                            if "莫凡" in hist_plan.entities and current_entities[0] != "莫凡":
                                anchor = "莫凡"
                            else:
                                anchor = next(
                                    (e for e in hist_plan.entities if e != current_entities[0]),
                                    hist_plan.entities[0],
                                )
                            rewritten = f"{anchor}和{current_entities[0]}是什么关系"
                            v2_intent = "relationship"
                    break

    # 如果追问消解重写了问题，用重写后的问题重新提取实体和检索词
    effective_v1 = v1
    if rewritten and rewritten != question:
        effective_v1 = build_knowledge_query_plan(rewritten)
        entities = [e for e in effective_v1.entities if not e.endswith("系")]
        attributes = [e for e in effective_v1.entities if e.endswith("系")]
        required_terms = list(effective_v1.required_terms)
        # 重新计算 evidence/answer policy
        if v2_intent == "relationship":
            evidence_policy = "双方同段出现且包含关系词的片段优先。"
            answer_policy = "只陈述资料中明确描述的关系。"

    return KnowledgeQueryPlanV2(
        original_question=question,
        rewritten_question=rewritten,
        intent=v2_intent,
        entities=entities + attributes,  # 保持向后兼容：entities 含所有实体
        attributes=attributes,
        relation_targets=[],
        time_scope=None,
        sub_queries=effective_v1.search_queries,
        required_terms=required_terms,
        optional_terms=effective_v1.keywords,
        evidence_policy=evidence_policy,
        answer_policy=answer_policy,
        confidence=0.6,  # 规则 planner 默认置信度
        planner="rule",
    )


# ── 主入口 ──────────────────────────────────────────────

async def build_knowledge_query_plan_v2(
    question: str,
    *,
    recent_messages: list[dict] | None = None,
    user_id: str | None = None,
    db: AsyncSession | None = None,
) -> KnowledgeQueryPlanV2:
    """V2 Planner 主入口：LLM 优先，规则 fallback。"""
    # 1. 尝试 LLM planner
    llm_plan = await _build_llm_plan(question, recent_messages, user_id, db)
    if llm_plan and llm_plan.confidence >= 0.5:
        return llm_plan

    # 2. 规则 fallback
    rule_plan = _build_rule_plan(question, recent_messages)

    # 3. 如果 LLM 有结果但置信度低，用规则 plan 但保留 LLM 的 rewritten_question
    if llm_plan and llm_plan.rewritten_question:
        rule_plan.rewritten_question = llm_plan.rewritten_question

    return rule_plan
