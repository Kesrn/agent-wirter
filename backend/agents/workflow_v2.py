"""LangGraph 创作工作流 v2 — Expert System v2 标准章节生成链路。

节点顺序（planning_review 默认关闭）：
  context_loader → chapter_architect → chapter_writer → structural_critic
  → narrative_editor → continuity_checker → human_review

与旧 workflow.py 的区别：
- architect 先产出 ChapterTaskCard，writer 消费任务卡写正文
- critic 输出结构化 StructuralCritique，editor 消费审稿指令修改正文
- 每个节点用 v2 skill_dir（chapter-architect / chapter-writer 等）
- 旧 workflow.py 保留不动，resume 端点按 AiRun.workflow_key 判断走哪套
"""

import json
import logging
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

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

logger = logging.getLogger(__name__)

# 复用旧 workflow 的 checkpointer（保证 resume 能跨请求恢复）
from agents.workflow import _CHECKPOINTER  # noqa: E402


# ── v2 工作流状态 ──────────────────────────────────────


class CreativeStateV2(TypedDict, total=False):
    """v2 工作流状态。total=False 让所有键可选，兼容旧 state。"""

    # ── 旧字段（全部保留）──
    project_id: str
    chapter_id: str
    chapter_num: int
    mode: str
    context: str
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
    target_words: int
    selected_direction: str
    user_note: str
    skill_packs: Annotated[list[dict], lambda a, b: a + b]
    harness_run_id: str
    harness_step_id: str
    # ── v2 新增 ──
    chapter_task_card: dict      # architect 输出
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


# ── JSON 解析辅助 ──────────────────────────────────────


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """容错解析 LLM 输出的 JSON。失败时返回 fallback。"""
    raw = raw or ""
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("v2 JSON parse failed: %s", e)
    return fallback


def _format_task_card(card: dict) -> str:
    """把 ChapterTaskCard 格式化为 prompt 可读文本。"""
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
    """把 StructuralCritique 格式化为 editor 可读的修改指令。"""
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
    """章节策划师：产出 ChapterTaskCard JSON。"""
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="chapter_architect", include_context=True)

    pack, pack_summary = _build_workflow_skill_pack(
        state, node_name="chapter_architect", role_type="writer", skill_dir="chapter-architect",
    )
    base_prompt = (
        "你是章节策划师。根据大纲、前文和设定产出结构化章节任务卡。"
        "只输出 ChapterTaskCard JSON，包含 chapter_number/chapter_title/core_task/"
        "opening_anchor/scenes[]/character_goals[]/information_rules/tension_design/word_budget/forbidden[]。"
        "\n如果上下文中存在“## 上章结尾锚点”，opening_anchor 必须基于该锚点明确说明本章开头如何承接上一章结尾。"
        "不得无视上章末尾另起新场景，除非本章大纲明确要求跳切，并需在 opening_anchor 中说明跳切方式与原因。"
    )
    system_prompt = build_expert_system_prompt("writer", base_prompt, pack)

    context = state.get("context", "")
    target_words = state.get("target_words", 2000)
    chapter_num = state.get("chapter_num", 1)
    user_note = state.get("user_note", "")
    user_note_block = f"\n## 用户本轮写作要求\n{user_note}\n" if user_note else ""
    user_prompt = (
        f"## 上下文\n{context}\n\n"
        f"{user_note_block}"
        f"## 章节信息\n第{chapter_num}章，目标字数{target_words}\n\n"
        "请输出 ChapterTaskCard JSON："
    )
    raw = await llm.generate(system_prompt, user_prompt, temperature=0.6, max_tokens=4096)

    card = _parse_json_response(raw, {
        "chapter_number": chapter_num,
        "chapter_title": "",
        "core_task": "",
        "scenes": [],
        "word_budget": target_words,
        "forbidden": [],
    })

    update = {"chapter_task_card": card}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def chapter_writer_node(state: CreativeStateV2) -> dict:
    """正文写手：消费 ChapterTaskCard 写正文。修订时改写当前候选稿。"""
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

    if revision_count > 0:
        # 修订模式：改写当前候选稿，不续写
        user_prompt = (
            f"## 上下文\n{context}\n\n"
            f"## 修改方向\n{chr(10).join(critiques) or '请提升整体完成度'}\n\n"
            f"## 当前候选稿（只能修改这份稿件，不得续写）\n{draft}\n\n"
            "请输出完整修改后的章节正文。不要输出说明、标题或修改清单。"
        )
    else:
        # 初次生成：消费任务卡
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
            "4. 只输出章节正文，不要输出说明、标题或项目符号。"
        )

    if target_words:
        user_prompt += f"\n\n**目标字数：约{target_words}字，请控制篇幅，并以完整句子自然结束。**"

    result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
    update = {"draft": result}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def structural_critic_node(state: CreativeStateV2) -> dict:
    """残酷审稿人：输出结构化 StructuralCritique JSON。"""
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
        "请诊断并输出 StructuralCritique JSON："
    )
    raw = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)

    critique = _parse_json_response(raw, {
        "summary": raw[:500] if raw else "",
        "p0": [],
        "p1": [],
        "p2": [],
        "edit_instructions": {},
    })

    update = {
        "structural_critique": critique,
        "critiques": [critique.get("summary", "")],
    }
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def narrative_editor_node(state: CreativeStateV2) -> dict:
    """专业编辑：消费 StructuralCritique 修改正文，输出修订稿。"""
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
    user_prompt = (
        f"## 审稿指令\n{critique_text}\n\n"
        f"## 待修改正文\n{draft}\n\n"
        "请按审稿指令修改正文，输出完整修订稿。不要输出说明或修改清单。"
    )
    result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)

    # editor 输出覆盖 draft（final_review 看的是最终 draft）
    update = {
        "draft": result,
        "edit_report": {"rewritten": critique.get("edit_instructions", {}).get("rewrite", [])},
    }
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


# ── 路由函数 ───────────────────────────────────────────


def route_after_review_v2(state: CreativeStateV2) -> str:
    """修订次数超限则结束，否则回到 writer 重写。"""
    if state.get("revision_count", 0) > 3:
        return END
    return "chapter_writer"


async def task_card_review_node(state: CreativeStateV2) -> dict:
    """任务卡审核节点（interrupt_before 暂停，等待用户确认/修改任务卡）"""
    return {}


# ── 图构建 ─────────────────────────────────────────────


def build_creative_graph_v2(planning_review: bool = False) -> StateGraph:
    """构建 v2 创作工作流图。

    Args:
        planning_review: 是否启用章节任务卡审核（I-4 默认关闭，预留）。
    """
    graph = StateGraph(CreativeStateV2)

    # 节点
    graph.add_node("context_loader", context_loader_node)  # 复用旧节点
    graph.add_node("chapter_architect", chapter_architect_node)
    graph.add_node("chapter_writer", chapter_writer_node)
    graph.add_node("structural_critic", structural_critic_node)
    graph.add_node("narrative_editor", narrative_editor_node)
    graph.add_node("continuity_checker", consistency_checker_node)  # 复用旧节点
    graph.add_node("human_review", human_review_node)  # 复用旧节点

    # planning_review: 在 architect 和 writer 之间插入任务卡审核节点
    if planning_review:
        graph.add_node("task_card_review", task_card_review_node)

    # 边：标准链
    graph.set_entry_point("context_loader")
    graph.add_edge("context_loader", "chapter_architect")
    if planning_review:
        # architect → task_card_review → writer
        graph.add_edge("chapter_architect", "task_card_review")
        graph.add_edge("task_card_review", "chapter_writer")
    else:
        graph.add_edge("chapter_architect", "chapter_writer")
    graph.add_edge("chapter_writer", "structural_critic")
    graph.add_edge("structural_critic", "narrative_editor")
    graph.add_edge("narrative_editor", "continuity_checker")
    graph.add_edge("continuity_checker", "human_review")
    graph.add_conditional_edges("human_review", route_after_review_v2)

    return graph


def get_creative_app_v2(planning_review: bool = False):
    """获取编译后的 v2 工作流应用（带 checkpoint 和 HITL）。

    planning_review=True 时会在 architect 完成后暂停，等待用户审核任务卡。
    """
    graph = build_creative_graph_v2(planning_review=planning_review)
    interrupt_nodes = ["task_card_review", "human_review"] if planning_review else ["human_review"]
    app = graph.compile(
        checkpointer=_CHECKPOINTER,
        interrupt_before=interrupt_nodes,
    )
    return app


# ── continue 模式 v2 图（无 HITL） ────────────────────


def build_creative_graph_v2_continue() -> StateGraph:
    """continue 模式的 v2 图：context_loader → architect → writer → continuity → END。

    与 full_pipeline 的区别：无 critic/editor/human_review，不暂停 HITL。
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
