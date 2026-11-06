"""Clarification Loop — ClarificationResult 结构化解析。

ClarificationResult schema:
  {
    "needs_clarification": bool,
    "confidence": number (0-1),
    "missing_fields": list[str],
    "questions": list[Question],
    "assumptions_if_skipped": list[str],
    "clarification_summary": str
  }

Question:
  {
    "id": str,
    "type": "single_choice" | "multi_choice" | "free_text" | "number",
    "question": str,
    "options": list[Option] | [],
    "required": bool,
    "reason": str
  }

Option:
  {
    "value": str,
    "label": str,
    "description": str
  }

这个模块只负责“把 LLM 的澄清规划结果变成稳定结构”：
- run_clarification_planner 调模型判断是否需要追问；
- parse_clarification_result 对模型输出做容错解析；
- build_embedded_clarification_payload 是历史兼容路径，主流程已迁移到 workflow_v2
  的 clarification_planner -> human_clarification 图节点。
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 3
VALID_QUESTION_TYPES = {"single_choice", "multi_choice", "free_text", "number"}

# M-1: STRICT 模式下 AI 没返回问题时的兜底问题。
# 兜底问题保证 STRICT 模式至少让用户明确一次本章重点，不被模型“无需澄清”
# 的判断直接跳过。
DEFAULT_CLARIFICATION_QUESTIONS = [
    {
        "id": "chapter_focus",
        "type": "free_text",
        "question": "这一章你最希望 AI 优先写好的重点是什么？",
        "required": False,
        "reason": "用于控制本章重点，避免自由发挥。",
    }
]


def build_embedded_clarification_payload(
    *,
    ai_result: dict | None,
    mode: str,
    round_num: int,
    max_rounds: int,
) -> dict | None:
    """[N-1 deprecated] 根据生成前交互模式构建 clarification payload。

    .. deprecated:: N-1
        本函数仅供已废弃的 refresh_task_card 历史路径使用。M-3 后澄清链路统一走
        graph 正式节点（clarification_planner -> human_clarification），不再在
        任务卡中内嵌澄清。待旧 run 全部过期后删除本函数。

    Args:
        ai_result: run_clarification_planner 的返回 dict（可为 None）
        mode: FAST / PLANNING / STRICT
        round_num: 当前轮次（1 起始）
        max_rounds: 最大轮次

    Returns:
        None 表示不附带 clarification（前端不显示问题区）。
        dict 始终包含: needs_clarification, questions, assumptions_if_skipped,
        round, max_rounds, show_user_note, require_answer。
    """
    ai_questions: list = []
    ai_assumptions: list = []
    if ai_result and ai_result.get("needs_clarification"):
        ai_questions = ai_result.get("questions", [])[:MAX_QUESTIONS]
        ai_assumptions = ai_result.get("assumptions_if_skipped", [])

    if mode == "FAST":
        # 不强制澄清；有 AI 问题时展示但不强制
        if not ai_questions:
            return None
        return {
            "needs_clarification": True,
            "questions": ai_questions,
            "assumptions_if_skipped": ai_assumptions,
            "round": round_num,
            "max_rounds": max_rounds,
            "show_user_note": False,
            "require_answer": False,
        }

    if mode == "PLANNING":
        # 每次展示补充要求区；有 AI 问题时展示问题
        if not ai_questions:
            return None
        return {
            "needs_clarification": True,
            "questions": ai_questions,
            "assumptions_if_skipped": ai_assumptions,
            "round": round_num,
            "max_rounds": max_rounds,
            "show_user_note": True,
            "require_answer": False,
        }

    if mode == "STRICT":
        # 至少一轮；AI 无问题时用兜底问题
        questions = ai_questions if ai_questions else DEFAULT_CLARIFICATION_QUESTIONS
        require_answer = round_num <= max_rounds
        return {
            "needs_clarification": True,
            "questions": questions,
            "assumptions_if_skipped": ai_assumptions,
            "round": round_num,
            "max_rounds": max_rounds,
            "show_user_note": True,
            "require_answer": require_answer,
        }

    # 未知模式 fallback 为 PLANNING 行为
    if not ai_questions:
        return None
    return {
        "needs_clarification": True,
        "questions": ai_questions,
        "assumptions_if_skipped": ai_assumptions,
        "round": round_num,
        "max_rounds": max_rounds,
        "show_user_note": True,
        "require_answer": False,
    }


def _coerce_confidence(value: Any) -> float:
    """将 confidence 强制为 0-1 的 float。"""
    try:
        v = float(value)
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return 0.0


def _coerce_string_list(value: Any) -> list[str]:
    """把任意 list-like LLM 输出清洗成字符串列表。

    模型可能把 missing_fields/assumptions 写成数字、对象或 null，这里只保留
    可转成字符串的条目，保证响应 schema 稳定。
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _coerce_question(question: Any, index: int) -> dict[str, Any] | None:
    """将单个 question 原始数据规范化为标准结构。

    这里做了几件保护：
    - 无 id 时自动生成 question_1/question_2；
    - 非法类型降级为 free_text；
    - 没有问题文本的条目直接丢弃；
    - free_text/number 不保留 options，避免前端误渲染选择器。
    """
    if not isinstance(question, dict):
        return None

    qid = question.get("id") or f"question_{index + 1}"
    qtype = question.get("type", "free_text")
    if qtype not in VALID_QUESTION_TYPES:
        qtype = "free_text"

    qtext = question.get("question", "")
    if not isinstance(qtext, str) or not qtext.strip():
        return None  # 无问题文本的问题丢弃

    # 选项规范化
    raw_options = question.get("options", [])
    options: list[dict[str, str]] = []
    if isinstance(raw_options, list):
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            value = str(opt.get("value", ""))
            label = str(opt.get("label", value))
            description = str(opt.get("description", ""))
            if value or label:
                options.append({"value": value, "label": label, "description": description})

    # free_text / number 类型不需要 options
    if qtype in ("free_text", "number"):
        options = []

    return {
        "id": str(qid),
        "type": qtype,
        "question": qtext.strip(),
        "options": options,
        "required": bool(question.get("required", False)),
        "reason": str(question.get("reason", "")),
    }


def _coerce_questions(raw_questions: Any) -> list[dict[str, Any]]:
    """规范化问题列表，裁剪到 MAX_QUESTIONS。

    问题数量限制是产品和体验边界：生成前最多追问 3 个问题，避免把写作流程
    变成冗长问卷。
    """
    if not isinstance(raw_questions, list):
        return []
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, q in enumerate(raw_questions):
        coerced = _coerce_question(q, i)
        if coerced is None:
            continue
        # 去重：同 id 的问题只保留第一个
        if coerced["id"] in seen_ids:
            continue
        seen_ids.add(coerced["id"])
        questions.append(coerced)
        if len(questions) >= MAX_QUESTIONS:
            break
    return questions


def parse_clarification_result(raw: str) -> dict[str, Any]:
    """容错解析 LLM 输出的 ClarificationResult JSON。

    解析策略（与 parse_guardrail_result 一致）：
    1. regex 提取 {...}
    2. json.loads
    3. 失败时返回 fallback（needs_clarification=False，不阻断流程）

    Args:
        raw: LLM 返回的原始文本

    Returns:
        ClarificationResult dict（永远非 None，永远有所有字段）
    """
    raw = raw or ""
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                questions = _coerce_questions(parsed.get("questions"))
                needs = bool(parsed.get("needs_clarification", False))
                # 如果没有问题但 needs_clarification=True，强制设为 False。
                # 前端需要 questions 才能渲染交互；没有问题时继续停在澄清节点没有意义。
                if needs and not questions:
                    needs = False
                # 如果有问题但 needs_clarification=False，强制设为 True。
                # 模型偶尔会字段自相矛盾，实际以 questions 是否存在为准。
                if questions and not needs:
                    needs = True

                summary = parsed.get("clarification_summary", "")
                if not isinstance(summary, str):
                    summary = str(summary) if summary else ""

                return {
                    "needs_clarification": needs,
                    "confidence": _coerce_confidence(parsed.get("confidence")),
                    "missing_fields": _coerce_string_list(parsed.get("missing_fields")),
                    "questions": questions,
                    "assumptions_if_skipped": _coerce_string_list(parsed.get("assumptions_if_skipped")),
                    "clarification_summary": summary,
                    "parse_error": False,
                }
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("clarification: LLM output JSON parse failed: %s", e)

    return {
        "needs_clarification": False,
        "confidence": 0.0,
        "missing_fields": [],
        "questions": [],
        "assumptions_if_skipped": [],
        "clarification_summary": "",
        "parse_error": True,
        "raw": raw.strip(),
    }


def filter_answered_questions(
    questions: list[dict[str, Any]],
    answered_ids: set[str],
) -> list[dict[str, Any]]:
    """过滤掉已经回答过的问题（多轮澄清时不重复问）。

    Args:
        questions: 当前轮次的问题列表
        answered_ids: 已经回答过的问题 id 集合

    Returns:
        过滤后的问题列表（最多 MAX_QUESTIONS 个）
    """
    filtered = [q for q in questions if q.get("id") not in answered_ids]
    return filtered[:MAX_QUESTIONS]


def build_clarification_summary(
    questions: list[dict[str, Any]],
    answers: dict[str, str],
) -> str:
    """将问题和回答整合为 clarification_summary 文本。

    这个摘要会被注入 chapter_architect_node，而不是直接注入 writer。
    目的是让澄清结果先影响任务卡规划，再由任务卡约束正文生成。

    Args:
        questions: 已问过的问题列表
        answers: {question_id: answer_value} 映射

    Returns:
        压缩摘要文本
    """
    lines: list[str] = []
    for q in questions:
        qid = q.get("id", "")
        qtext = q.get("question", "")
        answer = answers.get(qid, "")
        if answer:
            lines.append(f"Q: {qtext} A: {answer}")
    return "; ".join(lines) if lines else ""


# ── Prompt 构建 ────────────────────────────────────────


def build_clarification_prompt(
    *,
    context: str,
    chapter_num: int,
    target_words: int,
    user_note: str,
    previous_answers: dict[str, str],
    round_num: int,
    max_rounds: int,
) -> str:
    """构建 clarification-planner 的 user prompt。

    prompt 明确要求 planner 只判断“还缺什么信息”，不生成正文、不改大纲。
    这能把“需求澄清”和“章节策划”职责拆开，降低节点输出越界。

    Args:
        context: ChapterContextService 格式化的上下文（含大纲、角色、设定、前文等）
        chapter_num: 当前章节序号
        target_words: 目标字数
        user_note: 用户补充要求
        previous_answers: 前几轮的回答 {question_id: answer_value}
        round_num: 当前轮次（1 起始）
        max_rounds: 最大轮次

    Returns:
        user prompt 字符串
    """
    parts: list[str] = [
        f"## 上下文\n{context}",
        f"\n## 章节信息\n第{chapter_num}章，目标字数{target_words}",
    ]

    if user_note:
        parts.append(f"\n## 用户补充要求\n{user_note}")

    if previous_answers:
        answer_lines = []
        for qid, answer in previous_answers.items():
            answer_lines.append(f"- {qid}: {answer}")
        parts.append(f"\n## 已有澄清回答（第{round_num}轮）\n" + "\n".join(answer_lines))
        parts.append("请不要重复已经回答过的问题。")
    else:
        parts.append(f"\n## 澄清轮次\n第{round_num}轮（最多{max_rounds}轮）")

    parts.append(
        "\n请分析以上信息是否足够生成高质量章节正文。"
        "如果缺少会明显影响剧情走向、人物动机、视角、节奏、设定一致性的关键信息，"
        "生成最多 3 个高价值问题。"
        "如果信息已足够明确，设 needs_clarification=false。"
        "\n只输出 ClarificationResult JSON："
    )
    return "\n".join(parts)


# ── Planner Agent ──────────────────────────────────────


async def run_clarification_planner(
    *,
    llm_config: dict | None,
    context: str,
    chapter_num: int,
    target_words: int = 2000,
    user_note: str = "",
    previous_answers: dict[str, str] | None = None,
    round_num: int = 1,
    max_rounds: int = 3,
    harness_run_id: str = "",
    harness_step_id: str | None = None,
) -> dict[str, Any]:
    """调 LLM 运行澄清规划师，返回 ClarificationResult dict。

    使用 LoggedLLMProvider 记录 run_id / step_id / agent_name=clarification_planner。
    失败时 fallback needs_clarification=False，不阻断生成。

    Args:
        llm_config: 用户 LLM 配置（传给 get_llm_provider）
        context: ChapterContextService 格式化的上下文
        chapter_num: 当前章节序号
        target_words: 目标字数
        user_note: 用户补充要求
        previous_answers: 前几轮的回答 {question_id: answer_value}
        round_num: 当前轮次（1 起始）
        max_rounds: 最大轮次
        harness_run_id: AiRun.id（用于 LLM call log），空字符串则不记日志
        harness_step_id: 当前 AiRunStep.id

    Returns:
        ClarificationResult dict（parse_clarification_result 格式）
    """
    from agents.llm_provider import get_llm_provider
    from agents.workflow import _maybe_wrap_llm
    from skills.runner import build_expert_skill_pack, build_expert_system_prompt

    prev = previous_answers or {}
    answered_ids = set(prev.keys())

    # 构建 prompt：当前上下文 + 用户补充 + 已有回答。
    # previous_answers 存在时会要求模型不要重复追问。
    user_prompt = build_clarification_prompt(
        context=context,
        chapter_num=chapter_num,
        target_words=target_words,
        user_note=user_note,
        previous_answers=prev,
        round_num=round_num,
        max_rounds=max_rounds,
    )

    # 构建 system prompt（含 SKILL.md 内容）。
    # clarification-planner 的技能包可以沉淀“什么问题值得问、什么信息不该问”。
    pack = build_expert_skill_pack(
        "writer",  # clarification-planner 用 writer role_type 加载 skill
        skill_dir="clarification-planner",
        context=context,
        mode="generate",
    )
    base_prompt = (
        "你是澄清规划师。在章节生成前判断输入是否足够明确。"
        "如果缺少关键信息，生成最多 3 个高价值问题。"
        "不写正文，不改大纲。只输出 ClarificationResult JSON。"
    )
    system_prompt = build_expert_system_prompt("writer", base_prompt, pack)

    # 获取 LLM 并包装日志：如果本次生成有 AiRun，就把 planner 调用也记入
    # LLM call log，方便排查“为什么这次问了这些问题”。
    llm = get_llm_provider(llm_config)
    state_for_wrap = {
        "harness_run_id": harness_run_id,
        "harness_step_id": harness_step_id,
        "llm_config": llm_config,
        "context": context,
    }
    llm = _maybe_wrap_llm(llm, state_for_wrap, agent_name="clarification_planner", include_context=True)

    try:
        raw = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
        result = parse_clarification_result(raw)
    except Exception as e:
        logger.warning("run_clarification_planner LLM 调用失败，fallback needs_clarification=False: %s", e)
        result = {
            "needs_clarification": False,
            "confidence": 0.0,
            "missing_fields": [],
            "questions": [],
            "assumptions_if_skipped": [],
            "clarification_summary": "",
            "parse_error": True,
            "raw": str(e),
        }

    # 过滤已回答的问题，多轮澄清时避免重复问同一个 question_id。
    if result["questions"] and answered_ids:
        result["questions"] = filter_answered_questions(result["questions"], answered_ids)
        # 过滤后如果没有新问题了，不需要再澄清
        if not result["questions"]:
            result["needs_clarification"] = False

    # 如果有已有回答，整合到 summary。即使本轮不再追问，前面回答过的信息也不能丢。
    if prev:
        existing_summary = build_clarification_summary(
            [{"id": k, "question": k} for k in prev],
            prev,
        )
        if result["clarification_summary"]:
            result["clarification_summary"] = existing_summary + "; " + result["clarification_summary"]
        else:
            result["clarification_summary"] = existing_summary

    return result
