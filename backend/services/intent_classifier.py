"""对话意图分流服务 —— LLM 理解主导 + 规则安全护栏（方案 v2.4）。

五类意图（方案 §2.1）：
  knowledge_query   知识查询（查人物/世界/章节信息）
  discussion        创作讨论（不生成任务、不修改数据）
  rewrite           修改指令（第一版仅单章局部重写 chapter_rewrite）
  data_operation    数据操作（跨对象变更，任务卡 + 必须确认）
  flow_control      流程控制（撤销/采用/丢弃/定稿 → change_set / 版本操作）

路由（方案 v2.4 §1 目标架构）：
- 流程控制预筛（规则，安全第一）：命中流程词且无否定/疑问、且对应动作可用
  → 规则直达 flow_control（不调 LLM）。
- 其余 → LLM 理解主导（classify_with_llm 主路径）：严格 Pydantic 结构化输出
  （services.intent_models.LLMIntentOutput），解析失败/无 Key/校验不过
  → 规则回退（classify_rule，含否定/疑问检测的降级逻辑）。
- classify_rule 纯确定性：复合任务先拆步骤；单步按关键词判定意图。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

INTENTS = ("knowledge_query", "discussion", "rewrite", "data_operation", "flow_control")
INTENT_UNSUPPORTED = "intent_unsupported"

# ── 规则骨架（关键词 → 意图） ────────────────────────────

# 查：知识查询。强词（查/查询/搜索/资料/信息）单独即可判定查询；
# 弱词（什么/谁/最后/结局/出现）只在**没有修改词**时才参与判定——
# 否则"删掉最后一段"会被"最后"劫持成查询（"最后"在此是位置限定词）。
_KNOWLEDGE_KEYWORDS = ("查", "查询", "搜索", "搜", "找找", "哪里", "在哪", "资料", "信息")
_KNOWLEDGE_WEAK_KEYWORDS = ("什么", "谁", "最后", "结局", "出现", "怎么样", "如何")
# 改 / 重写：产生候选稿或修改章节正文（含日常口语：写/开头/结尾/段/几句/场景）
_REWRITE_KEYWORDS = (
    "改写", "重写", "修改", "润色", "压缩", "扩写", "续写", "删掉", "删除", "调整",
    "简化", "精简", "加快", "放慢", "补写", "重写一下", "改一改", "优化", "换个写法",
    "写个", "写一段", "写一段话", "写个开头", "写开头", "写结尾", "开头", "结尾",
    "写一段", "续写", "扩写", "补一段", "加一段", "删一段", "改一段", "重写这段",
    "几句", "一段", "这段", "那一段", "场景",
)
# 撤销 / 采用 / 定稿 等流程控制
_FLOW_KEYWORDS = ("撤销", "回滚", "撤回", "采用", "丢弃", "放弃", "不要了", "定稿", "使用这一版", "用这一版")
# 讨论：意见/感受/评价
_DISCUSSION_KEYWORDS = ("觉得", "认为", "是不是", "是否", "怎么样", "如何", "好", "差", "评价", "意见", "建议", "讨论", "节奏")

# 章节目标
_CHAPTER_NO_RE = re.compile(r"(?:第\s*)?(\d{1,5})\s*[章节回]")


def classify_rule(text: str) -> dict[str, Any]:
    """规则骨架分流（纯确定性，不调用 LLM）。

    返回分类结果 dict：{"intent", "steps", "plan", "requires_confirmation",
    "clarification", "confidence"}。steps 为复合任务的步骤列表（单步时为空），
    plan 为方案 §2.2 的复合计划 JSON（单步时 None）。
    """
    clean = _clean_text(text)

    # 1. 流程控制关键词优先（撤销/采用/定稿等，映射到 change_set/版本操作）。
    #    否定/疑问化时（"不要撤销，继续修改"）不判流程控制，继续走改写/讨论判定
    #    （补锁 5：规则降级路径同步支持否定/疑问）。
    for keyword in _FLOW_KEYWORDS:
        if keyword in clean:
            if _has_negation(clean) or _has_question_tone(clean):
                break
            return _flow_classification(clean, keyword)

    # 2. 识别"查...然后改..."这类复合任务：先拆步骤
    steps = _split_compound_steps(clean)
    if len(steps) >= 2:
        return _compound_classification(clean, steps)

    # 3. 单步：查 → knowledge_query；改 → rewrite；其余 → discussion。
    #    优先级：rewrite 强于 knowledge 弱词（"删掉最后一段"是修改不是查询）；
    #    强知识词（查/搜索/资料）仍优先（"查一下最后一段在哪"是查询）。
    #    P1-1：改写词 + 疑问/讨论语气（"讨论一下该不该修改第三章"）→ discussion，
    #    疑问只拦截修改类意图，不拦截知识查询（查询本身就是提问）。
    has_rewrite = _contains_any(clean, _REWRITE_KEYWORDS)
    has_strong_knowledge = _contains_any(clean, _KNOWLEDGE_KEYWORDS)
    if has_strong_knowledge and not has_rewrite:
        return _base_classification("knowledge_query", clean)
    if has_rewrite:
        if _has_question_tone(clean):
            return _base_classification("discussion", clean)
        return _base_classification("rewrite", clean)
    if _contains_any(clean, _KNOWLEDGE_WEAK_KEYWORDS):
        return _base_classification("knowledge_query", clean)
    return _base_classification("discussion", clean)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _base_classification(intent: str, clean: str) -> dict[str, Any]:
    return {
        "intent": intent,
        "steps": [],
        "plan": None,
        "requires_confirmation": False,
        "clarification": None,
        "confidence": 0.9,
    }


def _flow_classification(clean: str, keyword: str) -> dict[str, Any]:
    # 定稿：第一版不支持（intent_unsupported，不得猜测执行）
    if "定稿" in keyword:
        return {
            "intent": "flow_control",
            "steps": [],
            "plan": None,
            "requires_confirmation": False,
            "clarification": None,
            "confidence": 0.95,
            "intent_unsupported": True,
            "unsupported_reason": "第一版不支持定稿操作，仅支持单章局部重写。",
        }
    # 撤销/采用/丢弃 → flow_control，无需任务卡
    return {
        "intent": "flow_control",
        "steps": [],
        "plan": None,
        "requires_confirmation": False,
        "clarification": None,
        "confidence": 0.95,
        "action_hint": keyword,
    }


def _split_compound_steps(clean: str) -> list[dict[str, Any]]:
    """把"查A然后改B"拆成多步。用"然后/接着/再/之后"等连接词切分。

    返回每步的 {intent, raw}；连接词为动作连词（前后都像指令）才判复合，
    否则（如"然后呢"）不拆。
    """
    separators = ("再之后", "接着", "然后", "再")
    # 找到最靠前的连接词（修复：跳过完整连接词，而非只跳一个字符）
    hit = None
    for sep in separators:
        pos = clean.find(sep)
        if pos >= 0 and (hit is None or pos < hit[1]):
            hit = (sep, pos)
    if hit is None:
        return []
    sep, pos = hit
    first = clean[:pos]
    second = clean[pos + len(sep):]
    if not first or not second:
        return []
    # 前后都要像指令（含动词），否则不拆
    if not (_contains_any(first, _KNOWLEDGE_KEYWORDS + _REWRITE_KEYWORDS)
            and _contains_any(second, _KNOWLEDGE_KEYWORDS + _REWRITE_KEYWORDS)):
        return []
    steps = [
        {"intent": _intent_for_text(first), "raw": first},
        {"intent": _intent_for_text(second), "raw": second},
    ]
    return steps


def _intent_for_text(seg: str) -> str:
    if (_contains_any(seg, _KNOWLEDGE_KEYWORDS) or _contains_any(seg, _KNOWLEDGE_WEAK_KEYWORDS)) \
            and not _contains_any(seg, _REWRITE_KEYWORDS):
        return "knowledge_query"
    if _contains_any(seg, _REWRITE_KEYWORDS):
        return "rewrite"
    if _contains_any(seg, _FLOW_KEYWORDS):
        return "flow_control"
    return "discussion"


def _compound_classification(clean: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    plan = {
        "steps": [
            {"step": i + 1, "intent": step["intent"], "raw": step["raw"]}
            for i, step in enumerate(steps)
        ],
        "dependencies": [
            {"step": i + 2, "depends_on": [i + 1]}
            for i in range(len(steps) - 1)
        ],
    }
    return {
        "intent": "rewrite" if any(s["intent"] == "rewrite" for s in steps) else "knowledge_query",
        "steps": [s["intent"] for s in steps],
        "plan": plan,
        "requires_confirmation": True,
        "clarification": None,
        "confidence": 0.7,
    }


def _parse_intent_from_llm(raw: str) -> str | None:
    """从 LLM 输出里提取合法意图；非法/异常一律返回 None（回退规则结果）。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            parsed = json.loads(text[first:last + 1])
        else:
            parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        for intent in INTENTS:
            if intent in text:
                return intent
        return None
    if not isinstance(parsed, dict):
        return None
    intent = parsed.get("intent")
    if intent in INTENTS:
        return intent
    return None


# 否定 / 疑问 / 讨论语气检测（补锁 5：规则降级路径也要正确，不能只靠 LLM）
_NEGATION_WORDS = ("不要", "先别", "不能", "不该", "别", "不是", "不用", "无需")
_QUESTION_WORDS = ("该不该", "为什么", "咋", "怎么", "？", "?", "讨论", "聊聊", "想一下", "考虑")

# 可用动作（补锁 5：accept/discard/revert 分别判断，不能单布尔）
_ACTION_KEYWORDS = {
    "采用": "accept",
    "使用这一版": "accept",
    "用这一版": "accept",
    "丢弃": "discard",
    "放弃": "discard",
    "不要了": "discard",
    "撤销": "revert",
    "回滚": "revert",
    "撤回": "revert",
}


def _has_negation(clean: str) -> bool:
    # "不要了" 整体是丢弃意图（_ACTION_KEYWORDS），其"不要"前缀不是否定，
    # 先剔除再检测，否则"不要了"永远被误判成否定而无法触发 discard。
    rest = clean.replace("不要了", "")
    return _contains_any(rest, _NEGATION_WORDS)


def _has_question_tone(clean: str) -> bool:
    return _contains_any(clean, _QUESTION_WORDS)


def _flow_action(clean: str) -> str | None:
    """识别流程控制动作（accept/discard/revert）；无则 None。"""
    for keyword, action in _ACTION_KEYWORDS.items():
        if keyword in clean:
            return action
    return None


def flow_control_passthrough(
    clean: str,
    available_actions: set[str],
) -> dict[str, Any] | None:
    """流程控制预筛（方案 v2.4 决策 4 / 补锁 5）。

    命中流程词 且 无否定 且 无疑问/讨论语气 且 对应动作可用 → 规则直达 flow_control。
    否则返回 None（交给 LLM 或普通规则路径）。
    """
    action = _flow_action(clean)
    if action is None and "定稿" not in clean:
        return None
    if _has_negation(clean):
        return None
    if _has_question_tone(clean):
        return None
    if action is None:
        # 定稿：第一版不支持（intent_unsupported，不得猜测执行）
        return {
            "intent": "flow_control",
            "steps": [],
            "plan": None,
            "requires_confirmation": False,
            "clarification": None,
            "confidence": 0.95,
            "intent_unsupported": True,
            "unsupported_reason": "第一版不支持定稿操作，仅支持单章局部重写。",
        }
    if action not in available_actions:
        return None
    return {
        "intent": "flow_control",
        "steps": [],
        "plan": None,
        "requires_confirmation": False,
        "clarification": None,
        "confidence": 0.95,
        "action_hint": action,
    }


def _llm_to_classifier_result(llm: "LLMIntentOutput") -> dict[str, Any]:
    """LLM 严格输出 → 与 classify_rule 同结构的分类结果。

    - requires_confirmation 由后端计算（rewrite/data_operation → True，方案补锁 1）
    - 复合任务：plan 转对外结构；含不支持能力 → intent_unsupported（补锁 4）
    - resolved_instruction 由后端按 operation/requirements 组装（方案 A）
    """
    result: dict[str, Any] = {
        "intent": llm.intent,
        "steps": [],
        "plan": None,
        "requires_confirmation": llm.intent in ("rewrite", "data_operation", "flow_control"),
        "clarification": (
            {"needs_clarification": True, "reason": llm.clarification_question,
             "questions": []}
            if llm.needs_clarification and llm.clarification_question
            else None
        ),
        "confidence": 0.6,
        "llm_used": True,
    }

    # 目标与实体（供任务卡/生成使用）
    if llm.target_chapter_no is not None:
        result["target_chapter_no"] = llm.target_chapter_no
    if llm.mentioned_entities:
        result["mentioned_entities"] = llm.mentioned_entities
    if llm.operation is not None:
        result["operation"] = llm.operation.value
    if llm.requirements:
        result["requirements"] = llm.requirements

    # 复合任务：单一权威计划（补锁 4：识别但拒绝执行）
    if llm.plan is not None:
        from services.intent_models import to_conversation_plan
        result["plan"] = to_conversation_plan(llm.plan)
        result["steps"] = [s["intent"] for s in result["plan"]["steps"]]
        # P0-2：第一版仅支持单章重写——任何多步计划都拒绝执行，不只限于
        # 含 knowledge/data_operation/flow_control 步骤（"重写第一章然后重写
        # 第二章"同样不得偷偷压成一张单章任务卡，第二步静默丢失）。
        result["intent_unsupported"] = True
        result["unsupported_reason"] = (
            "当前暂不支持自动执行多步任务，请先完成知识查询，再确认修改任务。"
        )
        result["task_card"] = None
    return result


def _build_resolved_instruction(llm: "LLMIntentOutput") -> str | None:
    """方案 A：后端按 operation/requirements 模板组装 resolved_instruction。

    未匹配 operation 或没有 requirements → None（调用方回退原始消息）。
    """
    from services.intent_models import Operation
    if llm.operation is None:
        return None
    op_label = {
        Operation.STRENGTHEN_CHARACTER_PORTRAYAL: "强化人物表现",
        Operation.REWRITE_CHAPTER: "重写章节",
        Operation.COMPRESS_CHAPTER: "压缩章节",
        Operation.EXPAND_CHAPTER: "扩写章节",
        Operation.POLISH_CHAPTER: "润色章节",
        Operation.CONTINUE_CHAPTER: "续写章节",
    }.get(llm.operation)
    if op_label is None:
        return None
    parts = [f"执行{op_label}"]
    if llm.mentioned_entities:
        parts.append("重点处理：" + "、".join(llm.mentioned_entities))
    if llm.requirements:
        parts.extend(llm.requirements)
    return "；".join(parts)


def _format_history_for_prompt(chat_history: list[dict[str, Any]] | None) -> str:
    """对话历史 → prompt 文本（最近 5 回合，先 user 后 assistant 成对）。"""
    if not chat_history:
        return ""
    lines = []
    for turn in chat_history:
        role = "用户" if turn.get("role") == "user" else "助手"
        text = (turn.get("content") or "").strip()
        if text:
            lines.append(f"{role}：{text}")
    return "\n".join(lines)


def _format_workspace_for_prompt(workspace_context: dict[str, Any] | None) -> str:
    """工作对象上下文 → prompt 文本（章节号/标题/版本/选中文本/当前段落）。"""
    if not workspace_context:
        return ""
    parts = []
    if workspace_context.get("chapter_no") is not None:
        parts.append(f"当前章节：第{workspace_context['chapter_no']}章")
    if workspace_context.get("chapter_title"):
        parts.append(f"章节标题：{workspace_context['chapter_title']}")
    if workspace_context.get("current_version_id"):
        parts.append(f"章节版本：{workspace_context['current_version_id']}")
    selection = (workspace_context.get("selection") or "").strip()
    if selection:
        parts.append(f"用户选中的文本：{selection[:200]}")
    paragraph = (workspace_context.get("current_paragraph") or "").strip()
    if paragraph:
        parts.append(f"光标所在段落：{paragraph[:200]}")
    return "；".join(parts)


async def classify_with_llm(
    user_id: str,
    content: str,
    db,
    rule_result: dict[str, Any],
    *,
    chat_history: list[dict[str, Any]] | None = None,
    workspace_context: dict[str, Any] | None = None,
    confirmed_constraints: list[str] | None = None,
) -> dict[str, Any]:
    """LLM 理解主路径（方案 v2.4 决策 2 / 补锁 2 / 补锁 3）。

    严格 Pydantic 结构化输出；解析失败 / 无 Key / 校验不过 → 规则回退
    （llm_used=False，绝不猜测执行）。
    多轮上下文（补锁 3）：最近 5 回合对话历史 + 当前工作对象（选中文本/
    当前段落）+ 已确认约束，全部注入 prompt——"按刚才第二种方案改"依赖
    这些上下文才能解析成具体指令。
    """
    try:
        from agents.llm_provider import get_llm_provider
        from api.llm_deps import get_user_llm_config
        from services.intent_models import LLMIntentOutput

        llm_config = await get_user_llm_config(user_id, db)
        provider = get_llm_provider(llm_config)
        system_prompt = (
            "你是小说创作助手的意图理解器。根据用户消息与上下文，"
            "输出严格 JSON（不要输出 JSON 之外的任何内容）：\n"
            "{\n"
            '  "intent": "rewrite|knowledge_query|discussion|data_operation|flow_control",\n'
            '  "operation": "strengthen_character_portrayal|rewrite_chapter|compress_chapter|'
            'expand_chapter|polish_chapter|continue_chapter" 或 null,\n'
            '  "target_scope": "current_chapter|explicit_chapter|project_level|none",\n'
            '  "target_chapter_no": 数字或 null,\n'
            '  "mentioned_entities": ["人物名", ...],\n'
            '  "requirements": ["具体改写要求", ...],\n'
            '  "needs_clarification": false,\n'
            '  "clarification_question": "需要澄清时的问题" 或 null,\n'
            '  "plan": 复合任务时的 {"steps": [{"intent": "...", "raw": "...", '
            '"target_chapter_no": N}], "dependencies": [{"from_step": 1, "to_step": 2}]} 或 null\n'
            "}\n"
            "约束：intent 五选一；不要输出 requires_confirmation 字段；"
            "target_chapter_no 从 1 开始；requirements 每条不超过 120 字；"
            "不得输出 JSON 之外的任何内容。"
        )
        # 补锁 3：多轮上下文注入。对话历史/工作对象/已确认约束支持
        # "按刚才第二种方案改"这类依赖前文的指令解析。
        history_text = _format_history_for_prompt(chat_history)
        workspace_text = _format_workspace_for_prompt(workspace_context)
        constraints_text = "；".join(
            (c or "").strip() for c in (confirmed_constraints or []) if (c or "").strip()
        )
        context_parts = []
        if history_text:
            context_parts.append(f"## 对话历史（最近 5 个回合）\n{history_text}")
        if workspace_text:
            context_parts.append(f"## 当前工作对象\n{workspace_text}")
        if constraints_text:
            context_parts.append(f"## 已确认的修改约束\n{constraints_text}")
        user_prompt = (
            "识别下面这条用户消息的意图。\n"
            "knowledge_query=查询信息；discussion=创作讨论不修改数据；"
            "rewrite=改写/重写/润色章节正文；data_operation=跨对象数据变更；"
            "flow_control=撤销/采用/丢弃/定稿。\n"
            + ("\n".join(context_parts) + "\n" if context_parts else "")
            + f"用户消息：{content}\n只输出 JSON。"
        )
        raw = await provider.generate(system_prompt, user_prompt, temperature=0.1, max_tokens=400)
        llm = LLMIntentOutput.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 — LLM 主路径失败不影响主链路
        logger.warning("chat intent LLM 主路径失败，回退规则结果: %s", exc)
        return rule_result

    result = _llm_to_classifier_result(llm)
    resolved = _build_resolved_instruction(llm)
    if resolved:
        result["resolved_instruction"] = resolved
    return result


async def classify_intent(
    user_id: str,
    content: str,
    db,
    *,
    available_actions: set[str] | None = None,
    chat_history: list[dict[str, Any]] | None = None,
    workspace_context: dict[str, Any] | None = None,
    confirmed_constraints: list[str] | None = None,
) -> dict[str, Any]:
    """完整分流入口（方案 v2.4 目标架构）：

    1. 流程控制预筛（规则直达，安全第一）——命中且无否定/疑问且动作可用
    2. LLM 理解主路径（严格结构化，注入最近 5 回合对话历史 + 工作对象 +
       已确认约束）
    3. 降级：LLM 不可用 → 规则结果（含否定/疑问检测）
    """
    clean = _clean_text(content)

    # 1) 流程控制预筛（不调 LLM）
    gate = flow_control_passthrough(clean, available_actions or set())
    if gate is not None:
        return gate

    # 2) LLM 主路径
    rule_result = classify_rule(content)
    llm_result = await classify_with_llm(
        user_id, content, db, rule_result,
        chat_history=chat_history,
        workspace_context=workspace_context,
        confirmed_constraints=confirmed_constraints,
    )
    if llm_result.get("llm_used"):
        return llm_result

    # 3) 规则降级（否定/疑问检测已内聚到 classify_rule 的流程词分支：
    #    "不要撤销，继续修改" → rewrite / discussion，绝不误判 flow_control）
    return rule_result
