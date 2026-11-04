"""LangGraph 创作工作流 v2 — Expert System v2 标准章节生成链路。

节点顺序（planning_review 默认关闭）：
  context_loader → chapter_architect → chapter_writer → draft_validator
  → structural_critic → narrative_editor → edited_draft_validator
  → continuity_checker → final_quality_gate → human_review

与旧 workflow.py 的区别：
- architect 先产出 ChapterTaskCard，writer 消费任务卡写正文
- critic 输出结构化 StructuralCritique，editor 消费审稿指令修改正文
- 每个节点用 v2 skill_dir（chapter-architect / chapter-writer 等）
- 旧 workflow.py 保留不动，resume 端点按 AiRun.workflow_key 判断走哪套

v2 的核心变化是"先计划，再写正文"：architect 先把章节目标、场景边界、
信息揭示规则整理成任务卡，writer 只负责执行任务卡。这样比直接让 writer
看一大段上下文自由生成更可控，也方便用户在生成前审核任务卡。

质量验收门（draft_validator / edited_draft_validator / final_quality_gate）：
- 模型返回内容不再自动视为成功
- 字数由确定性代码给出软提醒；内部标记、正文格式等仍作为硬规则验证
- Writer 或 Editor 输出不合格时允许有限次数自动重试
- 最终质量门未通过时不得标记 COMPLETED / 发送 done / 进入 human_review
"""

import asyncio
import json
import logging
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from agents.critic_schema import repair_json_user_prompt, validate_critic_payload
from agents.guardrail import parse_guardrail_result
from agents.llm_provider import get_llm_provider
from agents.workflow import (
    CreativeState,
    DEFAULT_CONSISTENCY_PROMPT,
    DEFAULT_WRITER_PROMPT,
    _build_workflow_skill_pack,
    _maybe_wrap_llm,
    _skill_pack_summary,
    consistency_checker_node,
    context_loader_node,
    human_review_node,
)
from harness.llm_call_logger import LoggedLLMProvider  # noqa: F401
from skills.runner import build_expert_system_prompt
from services.chapter_quality import (
    QualityReport,
    EditEffectivenessReport,
    validate_draft_quality,
    validate_edited_draft_quality,
    validate_final_quality,
    validate_editor_effectiveness,
    build_retry_prompt_errors,
    clean_generated_content,
    check_heading,
)

logger = logging.getLogger(__name__)

# 复用旧 workflow 的 checkpointer（保证 resume 能跨请求恢复）
from agents.workflow import _CHECKPOINTER  # noqa: E402

# ── 默认重试配置 ──
DEFAULT_WRITER_MAX_RETRIES = 1
DEFAULT_EDITOR_MAX_RETRIES = 2


# ── v2 工作流状态 ──────────────────────────────────────


class CreativeStateV2(TypedDict, total=False):
    """v2 工作流状态。total=False 让所有键可选，兼容旧 state。

    旧字段保持不变，是为了让 routes.py、resume 逻辑、generation history、
    consistency_checker_node 等旧组件继续复用。v2 新字段只在新版节点中读写。
    """

    # ── 旧字段（全部保留）──
    project_id: str
    chapter_id: str
    chapter_num: int
    mode: str
    context: str
    context_summary: dict      # L-3: 前端任务卡预览使用的结构化上下文摘要
    draft: str
    original_text: str
    critiques: Annotated[list[str], lambda a, b: a + b]
    consistency_report: dict
    edited_draft: str
    revision_count: int
    writer_prompt: str
    critic_prompt: str
    editor_prompt: str
    consistency_prompt: str
    llm_config: dict | None
    selected_outline_ids: list[str]
    selected_character_ids: list[str]
    selected_world_entry_ids: list[str]
    selected_hidden_thread_ids: list[str]
    include_knowledge_sources: bool
    include_previous_summary: bool
    excluded_context_keys: list[str]
    target_words: int
    selected_direction: str
    user_note: str
    skill_packs: Annotated[list[dict], lambda a, b: a + b]
    harness_run_id: str
    harness_step_id: str
    # ── v2 新增 ──
    chapter_task_card: dict      # architect 输出
    writer_draft: str            # 兼容字段：等同第一版写手原文，后续不得覆盖
    original_writer_draft: str   # 第一版写手原文；只用于人工查看，永不覆盖
    cycle_writer_draft: str      # 当前生成/修订轮次的写手稿；Editor 有效性对比基线
    structural_critique: dict     # critic 输出
    edit_report: dict             # editor 输出
    workflow_key: str             # 本次 workflow 标识
    # ── L-1: task card review ──
    planning_review: bool         # 是否启用任务卡预览（写入 state 供 resume 读取）
    task_card_reviewed: bool      # 任务卡是否已审核
    modified_task_card: dict      # 用户修改后的任务卡（覆盖 chapter_task_card）
    # ── L-2: 嵌入式澄清 ──
    clarification_answers: dict   # 用户澄清答案 {question_id: answer}
    clarification_round: int      # 澄清轮次
    embedded_clarification: bool  # 澄清问题随任务卡展示，不单独暂停
    task_card_refresh_requested: bool  # 从任务卡回答澄清后，回到 architect 重规划
    context_refresh_requested: bool  # 上下文选择变化后，重建 context 再回到 architect
    # ── M-1: 生成前交互模式 ──
    pre_generation_mode: str       # FAST/PLANNING/STRICT
    max_clarification_rounds: int  # 澄清最大轮数
    # ── M-3: 澄清与任务卡解耦（graph 正式节点）──
    clarification_questions: list   # planner 输出的问题列表
    clarification_assumptions: list # planner 输出的假设
    clarification_summary: str      # 澄清总结（追加到 architect prompt）
    needs_clarification: bool       # planner 是否认为还需要澄清
    clarification_skipped: bool      # 用户是否跳过澄清
    requirements_complete: bool     # 澄清是否完成
    # ── 质量验收门 ──
    writer_retry_count: int         # Writer 重试次数
    writer_max_retries: int         # Writer 最大重试（默认 1）
    writer_validation_errors: list[dict]  # Writer 验收失败原因
    draft_quality_report: dict      # Writer 初稿质量报告
    editor_retry_count: int         # Editor 重试次数
    editor_max_retries: int         # Editor 最大重试（默认 2）
    editor_validation_errors: list[dict]  # Editor 验收失败原因
    edited_quality_report: dict     # Editor 修改后质量报告
    edit_effectiveness_report: dict # Editor 有效性报告
    final_quality_report: dict      # 最终质量门报告
    quality_gate_failed: bool       # 任一质量门重试耗尽或最终质量门失败
    quality_failure_stage: str      # draft / edited / final
    quality_failure_reason: str     # 可展示的确定性失败原因


# ── JSON 解析辅助 ──────────────────────────────────────


class WorkflowGenerationError(RuntimeError):
    """任一关键生成节点失败时抛出的、可直接展示给用户的错误。"""

    def __init__(
        self,
        message: str,
        *,
        step_name: str | None = None,
        agent_name: str | None = None,
        step_order: int | None = None,
    ) -> None:
        super().__init__(message)
        self.step_name = step_name
        self.agent_name = agent_name
        self.step_order = step_order


class TaskCardGenerationError(WorkflowGenerationError):
    """章节任务卡不能安全生成时抛出的错误。"""


def _extract_first_json_object(raw: str) -> str | None:
    """从包含说明或 Markdown 围栏的文本中提取第一个完整 JSON 对象。

    不能用 ``\{.*\}``：它会把多个对象之间的说明也贪婪地吞进去，而且会误把
    字符串里的花括号当成对象边界。这里按 JSON 字符串的转义规则追踪大括号深度。
    """
    for start, char in enumerate(raw):
        if char != "{":
            continue

        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(raw)):
            current = raw[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue

            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:end + 1]
    return None


def _escape_control_characters_in_json_strings(payload: str) -> str:
    """修复模型在 JSON 字符串中直接输出换行、制表符等控制字符的常见错误。

    JSON 允许 ``\\n``，不允许字符串内的真实换行。该修复只作用于引号内部，
    因而不会改变对象间的正常格式化换行。
    """
    escaped_chars = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    result: list[str] = []
    in_string = False
    escaped = False

    for char in payload:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == '"':
                in_string = False
                result.append(char)
                continue
            if ord(char) < 0x20:
                result.append(escaped_chars.get(char, f"\\u{ord(char):04x}"))
                continue
            result.append(char)
            continue

        result.append(char)
        if char == '"':
            in_string = True

    return "".join(result)


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """容错解析 LLM 输出的 JSON。失败时返回 fallback。

    真实模型经常会在 JSON 外包一层说明、Markdown 代码块或空行；偶发还会把
    多行文案直接写入 JSON 字符串。先提取完整对象，再尝试常见控制字符修复。
    仍无法解析时才返回 fallback。
    """
    raw = raw or ""
    payload = _extract_first_json_object(raw)
    if not payload:
        logger.warning("v2 JSON parse failed: 未找到完整 JSON 对象")
        return fallback

    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError) as e:
        parse_error = e
    else:
        parse_error = ValueError("JSON 根节点不是对象")

    repaired_payload = _escape_control_characters_in_json_strings(payload)
    if repaired_payload != payload:
        try:
            parsed = json.loads(repaired_payload)
            if isinstance(parsed, dict):
                logger.info("v2 JSON parse repaired unescaped control characters")
                return parsed
        except (json.JSONDecodeError, ValueError) as e:
            parse_error = e

    logger.warning("v2 JSON parse failed: %s", parse_error)
    return fallback


def _task_card_string_list(value: object) -> list[str]:
    """把模型/人工编辑产生的字符串或数组统一成非空字符串数组。"""
    if isinstance(value, str):
        return [
            item.strip()
            for item in re.split(r"[,，;；\n]+", value)
            if item.strip()
        ]
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
    return []


def normalize_task_card(card: object) -> dict:
    """把 ChapterTaskCard 的常见模型变体转换为稳定运行时结构。

    模型偶尔会把 ``information_rules`` 或 ``scenes`` 返回成字符串。任务卡
    人工审核又允许编辑原始 JSON，因此生成后和恢复前都必须执行运行时标准化，
    不能只依赖 TypeScript 类型。无法分类的揭示规则按禁止揭示保守处理。
    """
    if not isinstance(card, dict):
        return {}

    nested = card.get("ChapterTaskCard") or card.get("chapter_task_card")
    source = nested if isinstance(nested, dict) else card
    normalized = dict(source)

    raw_scenes = source.get("scenes")
    scenes: list[dict] = []
    if isinstance(raw_scenes, (list, tuple)):
        for index, scene in enumerate(raw_scenes, 1):
            if isinstance(scene, dict):
                normalized_scene = dict(scene)
                for key in ("characters", "must_include", "must_not_include"):
                    if key in normalized_scene:
                        normalized_scene[key] = _task_card_string_list(
                            normalized_scene[key]
                        )
                scenes.append(normalized_scene)
            elif isinstance(scene, str) and scene.strip():
                scenes.append({
                    "title": f"场景{index}",
                    "scene_goal": scene.strip(),
                })
    elif isinstance(raw_scenes, str) and raw_scenes.strip():
        for index, scene_text in enumerate(
            _task_card_string_list(raw_scenes),
            1,
        ):
            scenes.append({
                "title": f"场景{index}",
                "scene_goal": scene_text,
            })
    normalized["scenes"] = scenes

    raw_rules = source.get("information_rules")
    if isinstance(raw_rules, dict):
        information_rules = {
            "may_reveal": _task_card_string_list(raw_rules.get("may_reveal")),
            "hint_only": _task_card_string_list(raw_rules.get("hint_only")),
            "forbidden": _task_card_string_list(raw_rules.get("forbidden")),
        }
    else:
        information_rules = {
            "may_reveal": [],
            "hint_only": [],
            # 类型异常时不擅自放宽剧情边界，按禁止揭示保守处理。
            "forbidden": _task_card_string_list(raw_rules),
        }
    normalized["information_rules"] = information_rules

    for key in ("character_goals", "tension_design", "forbidden"):
        normalized[key] = _task_card_string_list(source.get(key))

    raw_budget = source.get("word_budget")
    if isinstance(raw_budget, str):
        budget_match = re.search(r"\d+", raw_budget)
        if budget_match:
            normalized["word_budget"] = int(budget_match.group())

    return normalized


def _is_usable_task_card(card: dict) -> bool:
    """只允许具备最小写作指令的任务卡进入人工确认和 writer 节点。"""
    return (
        isinstance(card, dict)
        and isinstance(card.get("core_task"), str)
        and bool(card["core_task"].strip())
    )


def _format_task_card(card: dict) -> str:
    """把 ChapterTaskCard 格式化为 prompt 可读文本。

    writer 不直接消费 dict，而是消费人类可读的任务卡文本。这样 prompt 更稳定，
    也便于用户在前端看到与模型实际使用一致的任务卡内容。
    """
    card = normalize_task_card(card)
    if not card:
        return "(无任务卡)"
    lines = []
    if card.get("chapter_title"):
        lines.append(f"章节标题：{card['chapter_title']}")
    if card.get("core_task"):
        lines.append(f"核心任务：{card['core_task']}")
    if card.get("opening_anchor"):
        lines.append(f"开篇锚点：{card['opening_anchor']}")
    scenes = card.get("scenes", [])
    if scenes:
        lines.append("场景划分：")
        for i, s in enumerate(scenes, 1):
            goal = s.get("scene_goal", "")
            conflict = s.get("conflict", "")
            budget = s.get("word_budget", "")
            lines.append(f"  场景{i}：{s.get('title', '')} — 目标:{goal} 冲突:{conflict} 字数:{budget}")
    rules = card.get("information_rules", {})
    if rules:
        if rules.get("forbidden"):
            lines.append(f"禁止揭示：{', '.join(rules['forbidden'])}")
        if rules.get("hint_only"):
            lines.append(f"仅暗示：{', '.join(rules['hint_only'])}")
    if card.get("forbidden"):
        lines.append(f"禁止事项：{', '.join(card['forbidden'])}")
    if card.get("word_budget"):
        lines.append(f"总字数预算：{card['word_budget']}")
    return "\n".join(lines)


def _format_critique(critique: dict) -> str:
    """把 StructuralCritique 格式化为 editor 可读的修改指令。

    critic 只诊断问题，editor 才执行修改。把两者拆开可以减少"审稿人边骂边改"
    导致指令混乱的问题。
    """
    if not critique:
        return "(无审稿意见)"
    lines = []
    if critique.get("summary"):
        lines.append(f"总评：{critique['summary']}")
    for level in ("p0", "p1", "p2"):
        items = critique.get(level, [])
        if items:
            lines.append(f"{level.upper()} 问题：")
            for item in items:
                lines.append(f"  - {item}")
    instructions = critique.get("edit_instructions", {})
    if instructions:
        for action, targets in instructions.items():
            if targets:
                lines.append(f"{action}：{', '.join(targets)}")
    return "\n".join(lines)


# ── v2 节点函数 ────────────────────────────────────────


async def chapter_architect_node(state: CreativeStateV2) -> dict:
    """章节策划师：产出 ChapterTaskCard JSON。

    输入：上下文、章节号、目标字数、用户补充要求、澄清总结。
    输出：chapter_task_card。后续 writer 必须严格按这张卡写正文。
    """
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="chapter_architect", include_context=True)

    pack, pack_summary = _build_workflow_skill_pack(
        state, node_name="chapter_architect", role_type="writer", skill_dir="chapter-architect",
    )
    base_prompt = (
        "你是章节策划师。根据本章大纲、上章结尾锚点/可用前文摘要和设定产出结构化章节任务卡。"
        "只输出 ChapterTaskCard JSON，包含 chapter_number/chapter_title/core_task/"
        "opening_anchor/scenes[]/character_goals[]/information_rules/tension_design/word_budget/forbidden[]。"
        "所有字符串必须是合法 JSON；若内容需要换行，请使用 \\n 转义，不得在引号内直接换行。"
        "\n如果上下文中存在“## 上章结尾锚点”，opening_anchor 必须基于该锚点明确说明本章开头如何承接上一章结尾。"
        "不得无视上章末尾另起新场景，除非本章大纲明确要求跳切，并需在 opening_anchor 中说明跳切方式与原因。"
    )
    system_prompt = build_expert_system_prompt("writer", base_prompt, pack)

    context = state.get("context", "")
    target_words = state.get("target_words", 2000)
    chapter_num = state.get("chapter_num", 1)
    user_note = state.get("user_note", "")
    user_note_block = f"\n## 用户本轮写作要求\n{user_note}\n" if user_note else ""
    # M-3: 读取澄清总结（graph 节点产出的 clarification_summary）。
    # 如果用户回答了澄清问题，这些答案会先被 planner 总结，再注入 architect，
    # 影响任务卡生成，而不是直接拼给 writer。
    clarification_summary = state.get("clarification_summary", "")
    clarification_block = f"\n## 用户澄清结果\n{clarification_summary}\n" if clarification_summary else ""
    user_prompt = (
        f"## 上下文\n{context}\n\n"
        f"{user_note_block}"
        f"{clarification_block}"
        f"## 章节信息\n第{chapter_num}章，目标字数{target_words}\n\n"
        "请输出 ChapterTaskCard JSON："
    )

    try:
        raw = await asyncio.wait_for(
            llm.generate(system_prompt, user_prompt, temperature=0.6, max_tokens=4096),
            timeout=180,
        )
    except asyncio.TimeoutError as e:
        logger.warning("chapter_architect LLM 调用超过 180 秒，已取消本次任务卡生成")
        raise TaskCardGenerationError(
            "章节任务卡生成失败：模型服务超过 180 秒没有返回，请稍后重试或切换更快的模型配置。",
            step_name="plan_chapter",
            agent_name="chapter_architect",
            step_order=2,
        ) from e
    except Exception as e:
        logger.exception("chapter_architect LLM 调用失败: %s", e)
        raise TaskCardGenerationError(
            "章节任务卡生成失败：模型服务调用失败，请检查模型配置或网络后重试。",
            step_name="plan_chapter",
            agent_name="chapter_architect",
            step_order=2,
        ) from e

    card = normalize_task_card(_parse_json_response(raw, {}))
    if not _is_usable_task_card(card):
        logger.warning("chapter_architect 返回的任务卡不完整，拒绝进入人工审核")
        raise TaskCardGenerationError(
            "章节任务卡生成失败：模型返回的结构化内容无法解析，请重新生成。",
            step_name="plan_chapter",
            agent_name="chapter_architect",
            step_order=2,
        )

    # 非关键字段使用安全默认值，确保 writer 与前端编辑器可以稳定消费。
    card.setdefault("chapter_number", chapter_num)
    card.setdefault("chapter_title", f"第{chapter_num}章" if chapter_num else "")
    card.setdefault("scenes", [])
    card.setdefault(
        "information_rules",
        {"may_reveal": [], "hint_only": [], "forbidden": []},
    )
    card.setdefault("word_budget", target_words)
    card.setdefault("forbidden", [])

    update = {"chapter_task_card": card}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def chapter_writer_node(state: CreativeStateV2) -> dict:
    """正文写手：消费 ChapterTaskCard 写正文。修订时改写当前候选稿。

    writer 是唯一真正产出正文 draft 的节点。v2 中它不再自己规划章节结构，
    而是执行 architect 生成、用户可审核的任务卡。
    质量验收失败时会从 draft_validator 回环到此节点重写。
    """
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="chapter_writer", include_context=True)

    pack, pack_summary = _build_workflow_skill_pack(
        state, node_name="chapter_writer", role_type="writer", skill_dir="chapter-writer",
    )
    base_prompt = state.get("writer_prompt") or (
        "你是正文写手。严格依据已确认的章节任务卡和项目设定完成本章正文。"
        "不修改任务卡，不擅自增加长期设定，不在正文中输出元信息。"
    )
    system_prompt = build_expert_system_prompt("writer", base_prompt, pack)

    context = state.get("context", "")
    draft = state.get("draft", "")
    critiques = state.get("critiques", [])
    target_words = state.get("target_words")
    revision_count = state.get("revision_count", 0)
    card = state.get("chapter_task_card", {})
    retry_count = state.get("writer_retry_count", 0)

    if revision_count > 0:
        retry_feedback = ""
        if retry_count > 0:
            retry_errors = state.get("writer_validation_errors", [])
            retry_messages = [
                str(error.get("message", "")).strip()
                for error in retry_errors
                if isinstance(error, dict) and str(error.get("message", "")).strip()
            ]
            if retry_messages:
                retry_feedback = (
                    "\n\n## 上一次修订稿验收失败原因（本次必须修复）\n"
                    + "\n".join(f"- {message}" for message in retry_messages)
                )
        user_prompt = (
            f"## 上下文\n{context}\n\n"
            f"## 修改方向\n{chr(10).join(critiques) or '请提升整体完成度'}\n\n"
            f"## 当前候选稿（只能修改这份稿件，不得续写）\n{draft}\n\n"
            f"{retry_feedback}\n\n"
            "请输出完整修改后的章节正文。不要输出说明、标题或修改清单。"
        )
    elif retry_count > 0:
        # 质量验收失败重写：将确定性错误明确传入 Prompt
        retry_errors = state.get("writer_validation_errors", [])
        retry_feedback = ""
        if retry_errors:
            retry_lines = ["上一次输出未通过系统验收："]
            for i, err in enumerate(retry_errors, 1):
                retry_lines.append(f"{i}. {err.get('message', '')}")
            retry_lines.append(f"\n目标字数约 {target_words} 字，请严格控制篇幅。")
            retry_lines.append("禁止输出场景标题、增强标签、写作说明和修改说明。")
            retry_lines.append("请重新输出完整纯正文，不要解释。")
            retry_feedback = "\n\n## 重写要求\n" + "\n".join(retry_lines)

        card_text = _format_task_card(card)
        user_prompt = (
            f"## 章节任务卡\n{card_text}\n\n"
            f"## 上下文\n{context}\n\n"
            f"{retry_feedback}\n\n"
            "请根据章节任务卡和上下文生成本章完整正文。\n"
            "硬性要求：\n"
            "1. 严格遵循任务卡的场景划分和信息揭示边界，不得偏离本章大纲。\n"
            "2. 任务卡和本章大纲是最高优先级，执行重点和用户补充仅作参考，不得引入大纲之外的关键事件、人物或设定。\n"
            "3. 不修改任务卡，不新增任务卡之外的长期设定。\n"
            "4. 只输出章节正文，不要输出说明、标题或项目符号。\n"
            "5. 禁止在正文内部署【增强开始/结束】等规划标记、场景标题和修改说明。"
        )
    else:
        card_text = _format_task_card(card)
        selected_direction = state.get("selected_direction", "")
        user_note = state.get("user_note", "")
        direction_block = ""
        if selected_direction:
            direction_block += f"\n## 执行重点（参考）\n{selected_direction}\n注意：这只是执行重点提示，不得偏离本章大纲和任务卡。"
        if user_note:
            direction_block += f"\n## 用户补充要求\n{user_note}\n"
        user_prompt = (
            f"## 章节任务卡\n{card_text}\n\n"
            f"## 上下文\n{context}{direction_block}\n\n"
            "请根据章节任务卡和上下文生成本章完整正文。\n"
            "硬性要求：\n"
            "1. 严格遵循任务卡的场景划分和信息揭示边界，不得偏离本章大纲。\n"
            "2. 任务卡和本章大纲是最高优先级，执行重点和用户补充仅作参考，不得引入大纲之外的关键事件、人物或设定。\n"
            "3. 不修改任务卡，不新增任务卡之外的长期设定。\n"
            "4. 只输出章节正文，不要输出说明、标题或项目符号。\n"
            "5. 禁止在正文内部署【增强开始/结束】等规划标记、场景标题和修改说明。"
        )

    if target_words:
        user_prompt += f"\n\n**目标字数：约{target_words}字，请控制篇幅，并以完整句子自然结束。**"

    try:
        result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
    except Exception as e:
        logger.exception("chapter_writer LLM 调用失败: %s", e)
        # 不能把错误文本伪装成正文继续下游流程，否则会生成"半成功"的候选稿。
        raise WorkflowGenerationError(
            "章节正文生成失败：模型服务调用失败，请检查模型配置或网络后重试。",
            step_name="generate_draft",
            agent_name="chapter_writer",
            step_order=3,
        ) from e
    if not result or not result.strip():
        raise WorkflowGenerationError(
            "章节正文生成失败：模型返回空内容，请重新生成。",
            step_name="generate_draft",
            agent_name="chapter_writer",
            step_order=3,
        )

    # ``draft`` 是当前候选稿，后续 editor / 修订 writer 会覆盖它。首次写手输出
    # 单独保留，供人工审核时与编辑后的候选稿对照；修订不能改写这份基线。
    # writer_retry_count 由 draft_validator_node 递增，此处仅透传。
    update: dict = {
        "draft": result,
        "cycle_writer_draft": result,
    }
    if revision_count == 0 and not state.get("writer_draft"):
        update["writer_draft"] = result
    if revision_count == 0 and not state.get("original_writer_draft"):
        update["original_writer_draft"] = result
    # 如果不是修订模式且不是重试（第一次调用），重置计数器
    if revision_count == 0 and retry_count == 0:
        update["writer_retry_count"] = 0
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def structural_critic_node(state: CreativeStateV2) -> dict:
    """残酷审稿人：输出结构化 StructuralCritique JSON。

    它只负责诊断，不改正文。结果同时写 structural_critique（给 editor 用）
    和 critiques（兼容旧 review/revise 展示）。
    """
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="structural_critic")

    pack, pack_summary = _build_workflow_skill_pack(
        state, node_name="structural_critic", role_type="critic", skill_dir="structural-critic",
    )
    base_prompt = state.get("critic_prompt") or (
        "你是残酷审稿人。对正文进行诊断，输出 StructuralCritique JSON。"
        "包含 summary/p0[]/p1[]/p2[]/must_keep[]/edit_instructions{delete[],merge[],rewrite[],keep[]}。"
        "只诊断，不重写正文。"
    )
    system_prompt = build_expert_system_prompt("critic", base_prompt, pack)

    card = state.get("chapter_task_card", {})
    card_text = _format_task_card(card)
    draft = state.get("draft", "")
    user_prompt = (
        f"## 章节任务卡\n{card_text}\n\n"
        f"## 待审校正文\n{draft}\n\n"
        "请诊断并只输出一个 JSON 对象，不要 Markdown，不要增加 StructuralCritique 外层包装。"
        "字段必须严格为：summary(string)、p0/p1/p2/must_keep(string[])、"
        "requires_revision(boolean)、edit_instructions{delete[],merge[],move_forward[],"
        "move_later[],rewrite[],keep[]}。每项不超过160字，每组最多8项，确保 JSON 完整闭合。"
    )

    try:
        raw = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
    except Exception as e:
        logger.exception("structural_critic LLM 调用失败: %s", e)
        raise WorkflowGenerationError(
            "章节审校失败：模型服务调用失败，请检查模型配置或网络后重试。",
            step_name="critique",
            agent_name="structural_critic",
            step_order=5,
        ) from e
    if not raw or not raw.strip():
        raise WorkflowGenerationError(
            "章节审校失败：模型返回空内容，请重新生成。",
            step_name="critique",
            agent_name="structural_critic",
            step_order=5,
        )

    critique_model = validate_critic_payload(_parse_json_response(raw, None))
    if critique_model is None:
        try:
            repaired_raw = await llm.generate(
                system_prompt,
                repair_json_user_prompt(raw),
                temperature=0.0,
                max_tokens=4096,
            )
        except Exception as e:
            logger.exception("structural_critic JSON 修复调用失败: %s", e)
            raise WorkflowGenerationError(
                "章节审校失败：审校结果格式错误，自动修复调用失败，请重新生成。",
                step_name="critique",
                agent_name="structural_critic",
                step_order=5,
            ) from e
        critique_model = validate_critic_payload(
            _parse_json_response(repaired_raw or "", None)
        )
        if critique_model is None:
            raise WorkflowGenerationError(
                "章节审校失败：StructuralCritique JSON 连续两次无法解析或结构不完整，请重新生成。",
                step_name="critique",
                agent_name="structural_critic",
                step_order=5,
            )

    critique = critique_model.model_dump()

    update = {
        "structural_critique": critique,
        "critiques": [critique.get("summary", "")],
    }
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def narrative_editor_node(state: CreativeStateV2) -> dict:
    """专业编辑：消费 StructuralCritique 修改正文，输出修订稿。

    editor 输出会覆盖 draft，后续 continuity_checker 和 human_review 看到的是
    编辑后的最终候选稿。编辑失败会中止流程，避免未完成的候选稿被误当作最终稿。
    质量验收失败时会从 edited_draft_validator 回环到此节点重改。
    """
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="narrative_editor")

    pack, pack_summary = _build_workflow_skill_pack(
        state, node_name="narrative_editor", role_type="editor", skill_dir="narrative-editor",
    )
    base_prompt = state.get("editor_prompt") or (
        "你是专业编辑。根据审稿指令对正文进行修订，输出完整可用修订稿。"
        "可删并移重写，但不改变已确认的剧情结果。"
    )
    system_prompt = build_expert_system_prompt("editor", base_prompt, pack)

    critique = state.get("structural_critique", {})
    critique_text = _format_critique(critique)
    draft = state.get("draft", "")
    retry_count = state.get("editor_retry_count", 0)

    # 质量验收失败重写：将确定性错误明确传入 Prompt
    retry_feedback = ""
    if retry_count > 0:
        retry_errors = state.get("editor_validation_errors", [])
        if retry_errors:
            retry_lines = ["上一次编辑未通过系统验收："]
            for i, err in enumerate(retry_errors, 1):
                retry_lines.append(f"{i}. {err.get('message', '')}")
            retry_lines.append("请根据审稿指令和验收反馈重新修改，输出完整正文，不要解释。")
            retry_feedback = "\n\n## 验收反馈\n" + "\n".join(retry_lines)

    # 注入初稿长度和目标字数，帮 editor 控制篇幅
    target_words = state.get("target_words")
    length_hint = ""
    if target_words and target_words > 0:
        length_hint = (
            f"\n\n**目标字数约 {target_words} 字（{int(target_words * 0.8)}～{int(target_words * 1.2)} 字范围），"
            f"请确保修改后的正文不超出范围。**"
        )

    user_prompt = (
        f"## 审稿指令\n{critique_text}\n\n"
        f"## 待修改正文\n{draft}\n\n"
        f"{retry_feedback}"
        f"{length_hint}\n\n"
        "请按审稿指令修改正文，输出完整修订稿。\n"
        "硬性要求：\n"
        "1. 只输出修订后的完整正文，不要输出说明、修改清单或标题。\n"
        "2. 禁止在正文中保留【增强开始/结束】等规划标记或场景标题。"
    )

    try:
        result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)
    except Exception as e:
        logger.exception("narrative_editor LLM 调用失败: %s", e)
        raise WorkflowGenerationError(
            "章节编辑失败：模型服务调用失败，请检查模型配置或网络后重试。",
            step_name="edit_draft",
            agent_name="narrative_editor",
            step_order=6,
        ) from e
    if not result or not result.strip():
        raise WorkflowGenerationError(
            "章节编辑失败：模型返回空内容，请重新生成。",
            step_name="edit_draft",
            agent_name="narrative_editor",
            step_order=6,
        )

    update: dict = {
        "draft": result,
        "edit_report": {"rewritten": critique.get("edit_instructions", {}).get("rewrite", [])},
    }
    # 非重试的首次调用，重置编辑器计数器
    if retry_count == 0:
        update["editor_retry_count"] = 0
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


# ── 路由函数 ───────────────────────────────────────────


def route_after_review_v2(state: CreativeStateV2) -> str:
    """修订次数超限则结束，否则回到 writer 重写。"""
    if state.get("revision_count", 0) > 3:
        return END
    return "chapter_writer"


# ── M-3: 澄清与任务卡解耦 ────────────────────────────


async def clarification_planner_node(state: CreativeStateV2) -> dict:
    """澄清规划师节点：调 LLM 判断是否需要澄清，输出 questions 到 state。

    这是真正的 LangGraph 节点，内部调用 run_clarification_planner（调 LLM）。
    routes.py 不再手动跑 planner。
    """
    from agents.clarification import DEFAULT_CLARIFICATION_QUESTIONS, run_clarification_planner

    # 用户在前端选择"跳过澄清"后，后续不再追问，直接进入任务卡。
    if state.get("clarification_skipped"):
        return {
            "needs_clarification": False,
            "requirements_complete": True,
            "clarification_questions": [],
        }

    current_round = state.get("clarification_round", 0) or 0
    next_round = current_round + 1
    mode = (state.get("pre_generation_mode", "PLANNING") or "PLANNING").upper()
    result = await run_clarification_planner(
        llm_config=state.get("llm_config"),
        context=state.get("context", ""),
        chapter_num=state.get("chapter_num", 0),
        target_words=state.get("target_words", 2000),
        user_note=state.get("user_note", ""),
        previous_answers=state.get("clarification_answers", {}),
        round_num=next_round,
        max_rounds=state.get("max_clarification_rounds", 3),
        harness_run_id=state.get("harness_run_id", ""),
    )

    questions = result.get("questions", []) or []
    needs_clarification = bool(result.get("needs_clarification", False))

    # STRICT 模式首轮必须向用户确认一次。若 LLM 判断无需澄清，也用兜底问题。
    # 这保证产品语义是"先采访，再任务卡"，同时不会被模型过早放行。
    if mode == "STRICT" and next_round <= 1:
        if not questions:
            questions = DEFAULT_CLARIFICATION_QUESTIONS
        needs_clarification = True
    elif needs_clarification and not questions:
        needs_clarification = False

    return {
        "clarification_round": next_round,
        "clarification_questions": questions,
        "clarification_assumptions": result.get("assumptions_if_skipped", []),
        "clarification_summary": result.get("clarification_summary", ""),
        "needs_clarification": needs_clarification,
        "clarification_skipped": False,
        "requirements_complete": not needs_clarification,
    }


async def human_clarification_node(state: CreativeStateV2) -> dict:
    """人工澄清节点（interrupt_before 暂停，等待用户回答/跳过）。

    节点本身不处理答案，只作为暂停点。routes.resume 收到 submit_clarification
    或 skip_clarification 后，会把答案写回 state，再让图继续执行。
    """
    return {}


def route_after_clarification(state: CreativeStateV2) -> str:
    """澄清后路由：判断是否需要继续澄清还是进入 architect。

    - FAST：直接跳过澄清
    - STRICT 且 round==0：强制至少 1 轮
    - requirements_complete：澄清完成
    - round >= max_rounds：超过最大轮数
    """
    mode = (state.get("pre_generation_mode", "PLANNING") or "PLANNING").upper()
    round_num = state.get("clarification_round", 0) or 0
    max_rounds = max(1, state.get("max_clarification_rounds", 3) or 3)
    requirements_complete = state.get("requirements_complete", False)
    needs_clarification = state.get("needs_clarification", False)
    questions = state.get("clarification_questions", []) or []

    # FAST 模式：跳过澄清，直接进入任务卡，追求速度。
    if mode == "FAST":
        return "chapter_architect"
    if state.get("clarification_skipped"):
        return "chapter_architect"
    # STRICT 模式：至少 1 轮人工澄清，追求需求明确。
    if mode == "STRICT" and round_num <= 1:
        return "human_clarification"
    # 超过最大轮数
    if round_num >= max_rounds:
        return "chapter_architect"
    # 澄清完成
    if requirements_complete:
        return "chapter_architect"
    if needs_clarification and questions:
        return "human_clarification"
    # 继续澄清
    return "chapter_architect"


async def task_card_review_node(state: CreativeStateV2) -> dict:
    """任务卡审核节点（interrupt_before 暂停，等待用户确认/修改任务卡）。

    用户确认后，routes.resume 会把修改后的 task_card 写回 chapter_task_card，
    再从该节点继续到 chapter_writer。
    """
    return {}


async def context_refresher_node(state: CreativeStateV2) -> dict:
    """重新构建本次上下文，不重新运行澄清规划。"""
    update = await context_loader_node(state)
    update["context_refresh_requested"] = False
    return update


def route_after_task_card_review(state: CreativeStateV2) -> str:
    """任务卡审核后的去向。

    回答内嵌澄清问题不会直接进入 writer，而是先回到 architect 生成新的
    ChapterTaskCard；确认或跳过澄清后才进入 writer。
    """
    if state.get("context_refresh_requested"):
        return "context_refresher"
    if state.get("task_card_refresh_requested"):
        return "chapter_architect"
    return "chapter_writer"


# ── 质量验收门节点 ──────────────────────────────────────


def _quality_failure_reason(stage: str, report: dict) -> str:
    """把确定性质量报告转为稳定的终态失败原因。"""
    stage_labels = {"draft": "初稿", "edited": "编辑稿", "final": "最终"}
    issues = report.get("issues", []) if isinstance(report, dict) else []
    messages = [
        f"{issue.get('code', '')}: {issue.get('message', '')}"
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") == "ERROR"
    ]
    prefix = f"{stage_labels.get(stage, stage)}质量验收未通过"
    return f"{prefix}：" + "；".join(messages) if messages else prefix


async def draft_validator_node(state: CreativeStateV2) -> dict:
    """Writer 初稿确定性质量检查。

    检查 draft 是否通过标记、结尾等硬规则；字数仅作为人工审核提醒。
    通过则进入 structural_critic，
    失败且重试未耗尽则回到 chapter_writer 重写，失败且耗尽则抛出错误。
    """
    draft = state.get("draft", "")
    target_words = state.get("target_words", 0)
    retry_count = state.get("writer_retry_count", 0)
    max_retries = state.get("writer_max_retries", DEFAULT_WRITER_MAX_RETRIES)
    run_id = state.get("harness_run_id", "")

    report = validate_draft_quality(draft, target_words, run_id=run_id)
    errors = [i for i in report.issues if i.severity == "ERROR"]

    update: dict = {
        "draft_quality_report": report.to_dict(),
        "writer_retry_count": retry_count,
        "writer_validation_errors": [
            {"code": i.code, "severity": i.severity, "message": i.message}
            for i in errors
        ],
    }

    if report.passed:
        _, _, accepted_draft = check_heading(draft)
        # Editor 有效性以当前轮次通过质量门的 Writer 稿为基线。第一版原稿单独
        # 保存在 original_writer_draft / writer_draft，不能在修订轮次覆盖。
        update["draft"] = accepted_draft
        update["cycle_writer_draft"] = accepted_draft
        # 兼容直接调用 validator 的旧测试/旧 checkpoint：只有缺失第一版基线时
        # 才补写，正常工作流和后续修订均不会覆盖。
        if not state.get("writer_draft"):
            update["writer_draft"] = accepted_draft
        if not state.get("original_writer_draft"):
            update["original_writer_draft"] = accepted_draft
    else:
        # retry_count 表示已经安排/执行的额外重试次数。第一次失败从 0 增到 1；
        # 路由使用 <= max_retries，因此 max=1 时会真正执行一次重试。
        next_retry_count = retry_count + 1
        update["writer_retry_count"] = next_retry_count
        if next_retry_count > max_retries:
            reason = _quality_failure_reason("draft", report.to_dict())
            update.update({
                "quality_gate_failed": True,
                "quality_failure_stage": "draft",
                "quality_failure_reason": reason,
            })

    return update


async def edited_draft_validator_node(state: CreativeStateV2) -> dict:
    """Editor 修改后确定性质量检查。

    检查编辑后 draft 的质量，并验证 editor 有效性（Critic 要求修改时必须有实质改动）。
    通过则进入 continuity_checker，失败且重试未耗尽则回到 narrative_editor 重改。
    """
    draft = state.get("draft", "")
    cycle_writer_draft = (
        state.get("cycle_writer_draft")
        or state.get("writer_draft", "")
    )
    target_words = state.get("target_words", 0)
    retry_count = state.get("editor_retry_count", 0)
    max_retries = state.get("editor_max_retries", DEFAULT_EDITOR_MAX_RETRIES)
    structural_critique = state.get("structural_critique", {})
    run_id = state.get("harness_run_id", "")

    report = validate_edited_draft_quality(
        cycle_writer_draft, draft, target_words, structural_critique, run_id=run_id,
    )
    eff = validate_editor_effectiveness(
        cycle_writer_draft,
        draft,
        structural_critique,
    )

    errors = [i for i in report.issues if i.severity == "ERROR"]

    update: dict = {
        "edited_quality_report": report.to_dict(),
        "edit_effectiveness_report": eff.to_dict(),
        "editor_retry_count": retry_count,
        "editor_validation_errors": [
            {"code": i.code, "severity": i.severity, "message": i.message}
            for i in errors
        ],
    }

    if report.passed:
        _, _, accepted_draft = check_heading(draft)
        update["draft"] = accepted_draft
    else:
        next_retry_count = retry_count + 1
        update["editor_retry_count"] = next_retry_count
        if next_retry_count > max_retries:
            reason = _quality_failure_reason("edited", report.to_dict())
            update.update({
                "quality_gate_failed": True,
                "quality_failure_stage": "edited",
                "quality_failure_reason": reason,
            })

    return update


async def final_quality_gate_node(state: CreativeStateV2) -> dict:
    """最终质量门：在 human_review 前执行综合检查。

    通过条件：无 ERROR issues、正文非空、无禁止标记、结尾完整，
    若 Critic 要求修改则编辑有效性已通过。字数偏差作为 WARNING 带入人工审核。
    失败时设置 quality_gate_failed。
    """
    draft = state.get("draft", "")
    target_words = state.get("target_words", 0)
    run_id = state.get("harness_run_id", "")

    eff_data = state.get("edit_effectiveness_report", {})
    eff = EditEffectivenessReport(
        requires_revision=eff_data.get("requires_revision", False),
        similarity=eff_data.get("similarity", 0.0),
        changed_chars=eff_data.get("changed_chars", 0),
        changed_original_chars=eff_data.get("changed_original_chars", 0),
        inserted_chars=eff_data.get("inserted_chars", 0),
        passed=eff_data.get("passed", False),
    )

    report = validate_final_quality(draft, target_words, edit_effectiveness=eff, run_id=run_id)

    return {
        "final_quality_report": report.to_dict(),
        "quality_gate_failed": not report.passed,
        "quality_failure_stage": "final" if not report.passed else "",
        "quality_failure_reason": _quality_failure_reason("final", report.to_dict()) if not report.passed else "",
    }


# ── 质量门路由函数 ─────────────────────────────────────


def route_after_draft_validator(state: CreativeStateV2) -> str:
    """Writer 质量门路由。

    Draft 通过 → structural_critic
    Draft 失败且 retry 未耗尽 → chapter_writer
    Draft 失败且 retry 已耗尽 → workflow error
    """
    report_data = state.get("draft_quality_report", {})
    if not isinstance(report_data, dict):
        return "structural_critic"

    passed = report_data.get("passed", False)
    if passed:
        return "structural_critic"

    retry_count = state.get("writer_retry_count", 0)
    max_retries = state.get("writer_max_retries", DEFAULT_WRITER_MAX_RETRIES)
    if retry_count <= max_retries:
        return "chapter_writer"

    return END  # 耗尽 → workflow error（由 routes.py 处理）


def route_after_edited_draft_validator(state: CreativeStateV2) -> str:
    """Editor 质量门路由。

    通过 → continuity_checker
    失败且 retry 未耗尽 → narrative_editor
    失败且 retry 已耗尽 → END（workflow error）
    """
    report_data = state.get("edited_quality_report", {})
    if not isinstance(report_data, dict):
        return "continuity_checker"

    passed = report_data.get("passed", False)
    if passed:
        return "continuity_checker"

    retry_count = state.get("editor_retry_count", 0)
    max_retries = state.get("editor_max_retries", DEFAULT_EDITOR_MAX_RETRIES)
    if retry_count <= max_retries:
        return "narrative_editor"

    return END


def route_after_final_quality_gate(state: CreativeStateV2) -> str:
    """最终质量门路由。

    通过 → human_review
    失败 → END（不进入人工审核）
    """
    report_data = state.get("final_quality_report", {})
    if not isinstance(report_data, dict):
        return "human_review"
    if report_data.get("passed", False):
        return "human_review"
    return END


# ── 图构建 ─────────────────────────────────────────────


def build_creative_graph_v2(
    planning_review: bool = False,
    pre_generation_mode: str = "PLANNING",
    embedded_clarification: bool = True,
) -> StateGraph:
    """构建 v2 创作工作流图。

    Args:
        planning_review: 是否启用章节任务卡审核（I-4 默认关闭，预留）。

    L-2 默认拓扑：context_loader → clarification_planner → chapter_architect
                → [task_card_review（内嵌澄清）] → chapter_writer → draft_validator
                → structural_critic → narrative_editor → edited_draft_validator
                → continuity_checker → final_quality_gate → human_review

    ``embedded_clarification=False`` 只用于兼容已创建、仍停在旧
    ``human_clarification`` 节点的工作流。
    """
    graph = StateGraph(CreativeStateV2)

    # 节点：context_loader / continuity_checker / human_review 复用旧 workflow。
    # 其余节点是 v2 专属，用任务卡和结构化审稿提高可控性。
    # 质量验收门节点是确定性代码验证，不调用 LLM。
    graph.add_node("context_loader", context_loader_node)  # 复用旧节点
    graph.add_node("chapter_architect", chapter_architect_node)
    graph.add_node("chapter_writer", chapter_writer_node)
    graph.add_node("draft_validator", draft_validator_node)
    graph.add_node("structural_critic", structural_critic_node)
    graph.add_node("narrative_editor", narrative_editor_node)
    graph.add_node("edited_draft_validator", edited_draft_validator_node)
    graph.add_node("continuity_checker", consistency_checker_node)  # 复用旧节点
    graph.add_node("final_quality_gate", final_quality_gate_node)
    graph.add_node("human_review", human_review_node)  # 复用旧节点

    mode = (pre_generation_mode or "PLANNING").upper()
    use_clarification = planning_review and mode != "FAST"

    # planning_review: 插入任务卡审核；非 FAST 时再插入澄清节点。
    # 这样"生成前交互"只在需要时存在，不影响默认快速生成路径。
    if use_clarification:
        graph.add_node("clarification_planner", clarification_planner_node)
        if not embedded_clarification:
            graph.add_node("human_clarification", human_clarification_node)
    if planning_review:
        graph.add_node("task_card_review", task_card_review_node)
        graph.add_node("context_refresher", context_refresher_node)
        graph.add_edge("context_refresher", "chapter_architect")

    # ── 边：标准链（含质量验收门）──
    graph.set_entry_point("context_loader")
    if use_clarification and embedded_clarification:
        graph.add_edge("context_loader", "clarification_planner")
        graph.add_edge("clarification_planner", "chapter_architect")
        graph.add_edge("chapter_architect", "task_card_review")
        graph.add_conditional_edges(
            "task_card_review",
            route_after_task_card_review,
            {
                "context_refresher": "context_refresher",
                "chapter_architect": "chapter_architect",
                "chapter_writer": "chapter_writer",
            },
        )
    elif use_clarification:
        graph.add_edge("context_loader", "clarification_planner")
        graph.add_conditional_edges(
            "clarification_planner",
            route_after_clarification,
            {
                "human_clarification": "human_clarification",
                "chapter_architect": "chapter_architect",
            },
        )
        graph.add_edge("human_clarification", "clarification_planner")
        graph.add_edge("chapter_architect", "task_card_review")
        graph.add_conditional_edges(
            "task_card_review",
            route_after_task_card_review,
            {
                "context_refresher": "context_refresher",
                "chapter_architect": "chapter_architect",
                "chapter_writer": "chapter_writer",
            },
        )
    elif planning_review:
        graph.add_edge("context_loader", "chapter_architect")
        graph.add_edge("chapter_architect", "task_card_review")
        graph.add_conditional_edges(
            "task_card_review",
            route_after_task_card_review,
            {
                "context_refresher": "context_refresher",
                "chapter_architect": "chapter_architect",
                "chapter_writer": "chapter_writer",
            },
        )
    else:
        graph.add_edge("context_loader", "chapter_architect")
        graph.add_edge("chapter_architect", "chapter_writer")

    # ── 质量验收门链路 ──
    # chapter_writer → draft_validator → structural_critic (带重试回路)
    graph.add_edge("chapter_writer", "draft_validator")
    graph.add_conditional_edges(
        "draft_validator",
        route_after_draft_validator,
        {
            "structural_critic": "structural_critic",
            "chapter_writer": "chapter_writer",
            END: END,
        },
    )
    # structural_critic → narrative_editor → edited_draft_validator
    graph.add_edge("structural_critic", "narrative_editor")
    graph.add_edge("narrative_editor", "edited_draft_validator")
    graph.add_conditional_edges(
        "edited_draft_validator",
        route_after_edited_draft_validator,
        {
            "continuity_checker": "continuity_checker",
            "narrative_editor": "narrative_editor",
            END: END,
        },
    )
    # continuity_checker → final_quality_gate → human_review
    graph.add_edge("continuity_checker", "final_quality_gate")
    graph.add_conditional_edges(
        "final_quality_gate",
        route_after_final_quality_gate,
        {
            "human_review": "human_review",
            END: END,
        },
    )
    graph.add_conditional_edges("human_review", route_after_review_v2)

    return graph


def get_creative_app_v2(
    planning_review: bool = False,
    pre_generation_mode: str = "PLANNING",
    embedded_clarification: bool = True,
):
    """获取编译后的 v2 工作流应用（带 checkpoint 和 HITL）。

    L-2 模式下只会在 task_card_review 和 human_review 处暂停。旧 run 仍可传
    ``embedded_clarification=False``，保持原来的 human_clarification 暂停点。
    """
    mode = (pre_generation_mode or "PLANNING").upper()
    graph = build_creative_graph_v2(
        planning_review=planning_review,
        pre_generation_mode=mode,
        embedded_clarification=embedded_clarification,
    )
    interrupt_nodes = ["human_review"]
    if planning_review:
        if mode != "FAST" and not embedded_clarification:
            interrupt_nodes.insert(0, "human_clarification")
        interrupt_nodes.insert(0, "task_card_review")
    app = graph.compile(
        checkpointer=_CHECKPOINTER,
        interrupt_before=interrupt_nodes,
    )
    return app


# ── continue 模式 v2 图（无 HITL） ────────────────────


def build_creative_graph_v2_continue() -> StateGraph:
    """continue 模式的 v2 图：context_loader → architect → writer → continuity → END。

    与 full_pipeline 的区别：无 critic/editor/human_review，不暂停 HITL。
    适用于用户已经选好续写方向，希望快速拿到一版后续内容的场景。
    """
    graph = StateGraph(CreativeStateV2)
    graph.add_node("context_loader", context_loader_node)
    graph.add_node("chapter_architect", chapter_architect_node)
    graph.add_node("chapter_writer", chapter_writer_node)
    graph.add_node("continuity_checker", consistency_checker_node)

    graph.set_entry_point("context_loader")
    graph.add_edge("context_loader", "chapter_architect")
    graph.add_edge("chapter_architect", "chapter_writer")
    graph.add_edge("chapter_writer", "continuity_checker")
    graph.add_edge("continuity_checker", END)
    return graph


def get_creative_app_v2_continue():
    """获取 continue 模式的 v2 工作流应用（不 interrupt，跑完直接完成）。"""
    graph = build_creative_graph_v2_continue()
    return graph.compile(checkpointer=_CHECKPOINTER)
