"""LangGraph 创作工作流

流水线：ContextLoader → [enabled experts by workflow_position] → HumanReview

使用 LangGraph StateGraph 实现，支持：
- 流式输出（astream_events）
- Human-in-the-loop（interrupt_before）
- Checkpoint 持久化（可选）
- 动态图构建：根据项目启用的专家决定节点和边
"""

import logging
import uuid
from typing import TypedDict, Annotated, Sequence

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from agents.llm_provider import get_llm_provider, LLMProvider
from rag.context_loader import ContextLoader
from skills.runner import build_expert_system_prompt

logger = logging.getLogger(__name__)


# In-memory checkpoint storage must be shared across requests. The generate
# endpoint pauses at human_review and returns a thread_id; the resume endpoint
# uses that thread_id in a separate HTTP request.
_CHECKPOINTER = MemorySaver()


# --- 工作流状态 ---
class CreativeState(TypedDict):
    project_id: str
    chapter_id: str
    mode: str  # continue | full_pipeline | enhance | summarize
    context: str  # RAG 检索到的上下文
    draft: str  # 当前草稿
    original_text: str  # 原始文本（增强模式用）
    critiques: Annotated[list[str], lambda a, b: a + b]  # 审校意见累积
    consistency_report: str  # 一致性检查报告
    edited_draft: str  # 编辑后草稿
    revision_count: int  # 修订次数
    writer_prompt: str  # 从 Expert 配置读取，fallback 到默认值
    critic_prompt: str  # 从 Expert 配置读取，fallback 到默认值
    editor_prompt: str  # 从 Expert 配置读取，fallback 到默认值
    consistency_prompt: str  # 从 Expert 配置读取，fallback 到默认值
    llm_config: dict | None  # 用户 LLM 配置
    selected_outline_ids: list[str]  # 用户选中的大纲 ID
    selected_character_ids: list[str]  # 用户选中的角色 ID
    selected_world_entry_ids: list[str]  # 用户选中的世界观 ID
    selected_hidden_thread_ids: list[str]  # 用户选中的暗线 ID
    target_words: int  # 目标字数


# --- 默认 system_prompt（硬编码 fallback） ---
DEFAULT_WRITER_PROMPT = """你是一位才华横溢的创意写作大师。你必须严格遵循以下规则：

1. **大纲优先**：严格按照提供的大纲展开情节，不得偏离大纲设定的事件和走向。
2. **角色一致**：角色的性格、说话风格、行为方式必须与角色资料中的设定完全一致，不得出现 OOC（out of character）。
3. **世界观遵守**：所有设定（地理、历史、规则、体系等）必须与世界观设定严格一致，不得自创矛盾设定。
4. **暗线融入**：如果提供了暗线设定，需自然地将其融入情节中，不做显式说明。

在严格遵守以上约束的前提下，发挥创意写出精彩的文学内容。"""
DEFAULT_CRITIC_PROMPT = "你是一位严苛的文学审校大师。对文本进行深度审校，输出结构化评价。"
DEFAULT_CONSISTENCY_PROMPT = "你是一位世界观一致性检查专家。检查文本是否与已知设定矛盾。"


def _is_revision_state(state: CreativeState) -> bool:
    return state.get("revision_count", 0) > 0


def _build_writer_user_prompt(state: CreativeState) -> str:
    """Build a writer prompt that separates initial generation from revision."""
    context = state.get("context", "")
    draft = state.get("draft", "")
    critiques = state.get("critiques", [])
    target_words = state.get("target_words")

    if _is_revision_state(state):
        user_prompt = (
            f"## 上下文\n{context}\n\n"
            f"## 修改方向\n{chr(10).join(critiques) or '请提升整体完成度'}\n\n"
            f"## 当前候选稿（只能修改这份稿件，不得续写）\n{draft}\n\n"
            "请输出“完整修改后的章节正文”。\n"
            "硬性要求：\n"
            "1. 这是改稿任务，不是续写任务；只能重写和调整当前候选稿已有内容。\n"
            "2. 不得在原结尾之后继续写，不得新增后续剧情、后续事件、新场景或新人物出场。\n"
            "3. 保持当前候选稿的事实、因果、场景边界和章节结尾，除非修改方向明确要求修正矛盾。\n"
            "4. 输出必须是一版完整章节正文，不要输出说明、标题、项目符号或修改清单。"
        )
    elif critiques:
        user_prompt = (
            f"## 上下文\n{context}\n\n"
            f"## 当前草稿\n{draft}\n\n"
            f"## 审校意见\n{chr(10).join(critiques)}\n\n"
            "请根据审校意见改进草稿，输出完整章节正文。不要在草稿结尾之后续写新剧情。"
        )
    else:
        user_prompt = (
            f"## 上下文\n{context}\n\n"
            f"## 当前草稿\n{draft}\n\n"
            "请根据上下文和当前草稿生成/完善本章正文。"
            "如果当前草稿非空，请将其整理为完整章节正文；不要接写无关后续内容。"
        )

    if target_words:
        user_prompt += f"\n\n**目标字数：约{target_words}字，请控制篇幅，并以完整句子自然结束。**"
    return user_prompt


# --- 通用专家节点工厂 ---
def _make_expert_node(expert_id: str, role_type: str, system_prompt: str, temperature: float, max_tokens: int):
    """为动态专家创建节点函数"""
    async def expert_node(state: CreativeState) -> dict:
        llm = get_llm_provider(state.get("llm_config"))
        prompt = build_expert_system_prompt(role_type, system_prompt or state.get("writer_prompt", DEFAULT_WRITER_PROMPT))

        if role_type == "writer":
            user_prompt = _build_writer_user_prompt(state)
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"draft": result}

        elif role_type == "critic":
            user_prompt = f"## 待审校文本\n{state.get('draft', '')}\n\n请审校："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"critiques": [result]}

        elif role_type == "editor":
            user_prompt = f"## 待编辑文本\n{state.get('draft', '')}\n\n请编辑润色："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"edited_draft": result}

        else:  # researcher / custom
            user_prompt = f"## 上下文\n{state.get('context', '')}\n\n## 当前草稿\n{state.get('draft', '')}\n\n请分析："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"critiques": [result]}

    expert_node.__name__ = f"expert_{expert_id[:8]}"
    return expert_node


# --- 内置节点函数 ---
async def context_loader_node(state: CreativeState) -> dict:
    """加载创作上下文

    当 state 中有 selected_*_ids 且非空时，按 ID 精确加载（不截断，不走向量搜索）。
    否则走现有 RAG 向量搜索逻辑（向后兼容）。
    """
    selected_outlines = state.get("selected_outline_ids", [])
    selected_characters = state.get("selected_character_ids", [])
    selected_world_entries = state.get("selected_world_entry_ids", [])
    selected_hidden_threads = state.get("selected_hidden_thread_ids", [])
    has_selections = selected_outlines or selected_characters or selected_world_entries or selected_hidden_threads
    logger.info(f"context_loader: outlines={selected_outlines}, chars={selected_characters}, we={selected_world_entries}, has_selections={has_selections}")

    if has_selections:
        # 用户主动选择了素材 → 按 ID 精确加载，不截断
        parts = []
        try:
            from db.session import async_session
            from models.outline import Outline
            from models.character import Character
            from models.world_entry import WorldEntry
            from models.chapter import Chapter
            from sqlalchemy import select

            async with async_session() as session:
                # 大纲
                if selected_outlines:
                    ids = [uuid.UUID(x) for x in selected_outlines]
                    result = await session.execute(
                        select(Outline).where(
                            Outline.id.in_(ids),
                            Outline.project_id == state["project_id"],
                        )
                    )
                    outlines = result.scalars().all()
                    if outlines:
                        ol_text = "\n".join(
                            f"### 第{o.sequence_number}章 {o.title}\n{o.summary or ''}\n{o.turning_point or ''}"
                            for o in outlines
                        )
                        parts.append(f"## 大纲\n{ol_text}")

                # 角色
                if selected_characters:
                    ids = [uuid.UUID(x) for x in selected_characters]
                    result = await session.execute(
                        select(Character).where(
                            Character.id.in_(ids),
                            Character.project_id == state["project_id"],
                        )
                    )
                    chars = result.scalars().all()
                    if chars:
                        char_text = "\n".join(
                            f"- {c.name}({c.role_type}): {c.profile or '暂无简介'}"
                            for c in chars
                        )
                        parts.append(f"## 角色资料\n{char_text}")

                # 世界观
                if selected_world_entries:
                    ids = [uuid.UUID(x) for x in selected_world_entries]
                    result = await session.execute(
                        select(WorldEntry).where(
                            WorldEntry.id.in_(ids),
                            WorldEntry.project_id == state["project_id"],
                        )
                    )
                    entries = result.scalars().all()
                    if entries:
                        we_text = "\n".join(
                            f"- [{e.category}] {e.title}: {e.content}"
                            for e in entries
                        )
                        parts.append(f"## 世界观设定\n{we_text}")

                # 暗线
                if selected_hidden_threads:
                    from models.hidden_thread import HiddenThread
                    ids = [uuid.UUID(x) for x in selected_hidden_threads]
                    result = await session.execute(
                        select(HiddenThread).where(
                            HiddenThread.id.in_(ids),
                            HiddenThread.project_id == state["project_id"],
                        )
                    )
                    threads = result.scalars().all()
                    if threads:
                        ht_text = "\n".join(
                            f"- {t.name}: {t.description or '暂无描述'}"
                            for t in threads
                        )
                        parts.append(f"## 暗线设定\n{ht_text}")

                # 前文：保持现有逻辑（最近3章，截断500字符）
                chapter_id = state.get("chapter_id", "")
                if chapter_id:
                    result = await session.execute(
                        select(Chapter)
                        .where(Chapter.project_id == state["project_id"])
                        .order_by(Chapter.sequence_number.desc())
                        .limit(3)
                    )
                    chapters = result.scalars().all()
                    if chapters:
                        ch_text = "\n\n".join(
                            f"### {c.title}\n{c.content[:500] or '(空)'}" for c in reversed(chapters)
                        )
                        parts.append(f"## 前文内容\n{ch_text}")

        except Exception as e:
            logger.exception(f"按 ID 加载上下文失败: {e}")
            parts.append(f"(上下文加载失败: {e})")

        context = "\n\n".join(parts) if parts else "(暂无上下文)"
        logger.info(f"context_loader: loaded context length={len(context)}, parts={len(parts)}")
        return {"context": context}

    # 无选中 ID → 走现有 RAG 向量搜索逻辑
    loader = ContextLoader()
    context = await loader.load_context(
        project_id=state["project_id"],
        chapter_id=state["chapter_id"],
        include_world=True,
        include_characters=True,
        include_previous_chapters=3,
        include_outline=True,
    )
    return {"context": context}


async def writer_node(state: CreativeState) -> dict:
    """创意大师：初次生成章节；修订时改写当前候选稿而不是续写。"""
    llm = get_llm_provider(state.get("llm_config"))
    system_prompt = build_expert_system_prompt("writer", state.get("writer_prompt") or DEFAULT_WRITER_PROMPT)
    user_prompt = _build_writer_user_prompt(state)
    result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
    return {"draft": result}


async def critic_node(state: CreativeState) -> dict:
    """残酷大师：结构化审校"""
    llm = get_llm_provider(state.get("llm_config"))
    system_prompt = build_expert_system_prompt("critic", state.get("critic_prompt") or DEFAULT_CRITIC_PROMPT)
    user_prompt = f"## 待审校文本\n{state.get('draft', '')}\n\n请审校："
    result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
    return {"critiques": [result]}


async def consistency_checker_node(state: CreativeState) -> dict:
    """一致性检查：与世界观/角色/前文对照"""
    llm = get_llm_provider(state.get("llm_config"))
    system_prompt = build_expert_system_prompt("consistency_checker", state.get("consistency_prompt") or DEFAULT_CONSISTENCY_PROMPT)
    user_prompt = f"## 上下文/设定\n{state.get('context', '')}\n\n## 待检查文本\n{state.get('draft', '')}\n\n请检查一致性："
    result = await llm.generate(system_prompt, user_prompt, temperature=0.2, max_tokens=2048)
    return {"consistency_report": result}


async def human_review_node(state: CreativeState) -> dict:
    """人工审批节点（interrupt_before 暂停，等待用户决策）"""
    return {}


# --- 路由函数 ---
def route_after_review(state: CreativeState) -> str:
    """根据修订次数决定是否继续"""
    if state.get("revision_count", 0) > 3:
        return END
    return "writer"


# --- 构建工作流 ---
def build_creative_graph(enabled_experts: list | None = None) -> StateGraph:
    """构建创作工作流图

    Args:
        enabled_experts: 项目启用的专家列表（Expert ORM 对象）。
            若为 None 或空，使用默认静态流水线。
    """
    graph = StateGraph(CreativeState)

    # context_loader 和 human_review 始终存在
    graph.add_node("context_loader", context_loader_node)
    graph.add_node("human_review", human_review_node)
    graph.set_entry_point("context_loader")

    if not enabled_experts:
        # 无专家配置 → 默认流水线：context_loader → writer → [critic || consistency_checker] → human_review
        graph.add_node("writer", writer_node)
        graph.add_node("critic", critic_node)
        graph.add_node("consistency_checker", consistency_checker_node)

        graph.add_edge("context_loader", "writer")
        graph.add_edge("writer", "critic")
        graph.add_edge("writer", "consistency_checker")
        graph.add_edge("critic", "human_review")
        graph.add_edge("consistency_checker", "human_review")
        graph.add_conditional_edges("human_review", route_after_review)
        return graph

    # --- 动态构建：根据 enabled_experts 的 workflow_position 决定节点和边 ---
    # 按位置分组
    replace_writer = None
    replace_critic = None
    pre_writer = []
    post_writer = []
    pre_critic = []
    post_critic = []
    standalone = []

    for exp in enabled_experts:
        pos = exp.workflow_position
        if pos == "replace_writer":
            replace_writer = exp
        elif pos == "replace_critic":
            replace_critic = exp
        elif pos == "pre_writer":
            pre_writer.append(exp)
        elif pos == "post_writer":
            post_writer.append(exp)
        elif pos == "pre_critic":
            pre_critic.append(exp)
        elif pos == "post_critic":
            post_critic.append(exp)
        elif pos == "standalone":
            standalone.append(exp)

    # 构建有序节点链：context_loader → pre_writer* → writer → post_writer* → pre_critic* → critic → post_critic* → consistency_checker → human_review
    node_chain = []  # (node_name, node_fn)

    # pre_writer experts
    for i, exp in enumerate(pre_writer):
        name = f"pre_writer_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens)
        node_chain.append((name, fn))

    # writer: replace_writer 或默认
    if replace_writer:
        fn = _make_expert_node(str(replace_writer.id), replace_writer.role_type, replace_writer.system_prompt, replace_writer.temperature, replace_writer.max_tokens)
        node_chain.append(("writer", fn))
    else:
        node_chain.append(("writer", writer_node))

    # post_writer experts
    for i, exp in enumerate(post_writer):
        name = f"post_writer_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens)
        node_chain.append((name, fn))

    # pre_critic experts
    for i, exp in enumerate(pre_critic):
        name = f"pre_critic_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens)
        node_chain.append((name, fn))

    # critic: replace_critic 或默认
    if replace_critic:
        fn = _make_expert_node(str(replace_critic.id), replace_critic.role_type, replace_critic.system_prompt, replace_critic.temperature, replace_critic.max_tokens)
        node_chain.append(("critic", fn))
    else:
        node_chain.append(("critic", critic_node))

    # post_critic experts
    for i, exp in enumerate(post_critic):
        name = f"post_critic_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens)
        node_chain.append((name, fn))

    # consistency_checker 始终使用默认
    node_chain.append(("consistency_checker", consistency_checker_node))

    # 添加所有节点到图
    for name, fn in node_chain:
        graph.add_node(name, fn)

    # 连接边：context_loader → chain[0] → ... → last pre/post_critic → human_review
    # consistency_checker 与 critic 并行（都从 writer 之后开始）
    graph.add_edge("context_loader", node_chain[0][0])
    for i in range(len(node_chain) - 1):
        graph.add_edge(node_chain[i][0], node_chain[i + 1][0])

    # 找到 writer 在 chain 中的位置，用于并行边
    writer_idx = next(i for i, (n, _) in enumerate(node_chain) if n == "writer")
    # consistency_checker 从 writer 之后开始（与 critic 分支并行）
    graph.add_edge("writer", "consistency_checker")
    # consistency_checker 和 critic 分支都汇入 human_review
    graph.add_edge("consistency_checker", "human_review")

    last_chain_node = node_chain[-1][0]
    graph.add_edge(last_chain_node, "human_review")
    graph.add_conditional_edges("human_review", route_after_review)

    return graph


def get_creative_app(enabled_experts: list | None = None):
    """获取编译后的工作流应用（带 checkpoint 和 HITL）"""
    graph = build_creative_graph(enabled_experts)
    app = graph.compile(
        checkpointer=_CHECKPOINTER,
        interrupt_before=["human_review"],
    )
    return app
