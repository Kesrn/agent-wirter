"""LangGraph 创作工作流。

流水线：ContextLoader → [enabled experts by workflow_position] → HumanReview

使用 LangGraph StateGraph 实现，支持：
- 流式输出（astream_events）
- Human-in-the-loop（interrupt_before）
- Checkpoint 持久化（可选）
- 动态图构建：根据项目启用的专家决定节点和边

这个文件是旧版小说章节生成主链路。routes.py 负责把 HTTP/SSE 请求转成
initial_state，再调用这里编译出的 LangGraph app。节点函数只接收/返回 state
的一小部分字段，LangGraph 负责把每个节点的输出合并回全局状态。
"""

import logging
import uuid
from typing import TypedDict, Annotated, Sequence

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from agents.llm_provider import get_llm_provider, LLMProvider
from harness.llm_call_logger import LoggedLLMProvider
from rag.context_loader import ContextLoader
from skills.runner import build_expert_skill_pack, build_expert_system_prompt
from services.skill_pack_planner import SkillPackPlan, plan_workflow_skill_pack

logger = logging.getLogger(__name__)


# In-memory checkpoint storage must be shared across requests. The generate
# endpoint pauses at human_review and returns a thread_id; the resume endpoint
# uses that thread_id in a separate HTTP request.
#
# 这里不能在 get_creative_app 里每次 new MemorySaver，否则 generate 请求暂停后，
# resume 请求拿到的是另一个空 checkpointer，会找不到 thread_id 对应状态。
_CHECKPOINTER = MemorySaver()


# --- 工作流状态 ---
class CreativeState(TypedDict):
    """旧版 LangGraph 全局状态。

    LangGraph 节点之间不直接互相调用，而是通过这个 dict 传递数据：
    - context_loader 写 context；
    - writer 写 draft；
    - critic 追加 critiques；
    - consistency_checker 写 consistency_report；
    - human_review 只负责暂停，让 routes.py 根据用户操作 resume。

    Annotated + reducer 的字段表示“合并时追加而不是覆盖”，例如多轮修订时
    critiques/skill_packs 可以保留历史。
    """
    project_id: str
    chapter_id: str
    chapter_num: int  # 章节序号，供 ChapterContextService 按章查询
    mode: str  # continue | full_pipeline | enhance | summarize
    context: str  # RAG 检索到的上下文
    context_summary: dict  # L-3: 前端可解释的结构化上下文摘要
    draft: str  # 当前草稿
    original_text: str  # 原始文本（增强模式用）
    critiques: Annotated[list[str], lambda a, b: a + b]  # 审校意见累积
    consistency_report: dict  # 结构化一致性检查结果（GuardrailResult dict）
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
    include_knowledge_sources: bool  # 是否将资料库 project_sources 注入上下文
    include_previous_summary: bool  # full_pipeline 是否显式注入最近三章前文摘要
    excluded_context_keys: list[str]  # L-3: 本次生成明确排除的上下文条目
    target_words: int  # 目标字数
    selected_direction: str  # 用户选择的剧情走向（从 DirectionPicker 传入）
    user_note: str  # 用户补充要求
    skill_packs: Annotated[list[dict], lambda a, b: a + b]  # 已注入的专家 skill pack 摘要
    # ── Harness 注入（可选，generate 路径写入，节点读取用于 LLM call log）──
    harness_run_id: str  # AiRun.id
    harness_step_id: str  # 当前 AiRunStep.id（routes 在 on_chain_start 后注入）


def _maybe_wrap_llm(llm, state, *, agent_name: str, include_context: bool = False):
    """若 state 注入了 harness_run_id，把 llm 包成 LoggedLLMProvider；否则原样返回。

    LoggedLLMProvider 会在每次 generate/generate_stream 时写 LlmCallLog，
    用于“AI Runs”页面查看模型、prompt、上下文快照和错误。没有 run_id 的旧路径
    不记录日志，保持轻量。
    """
    run_id = state.get("harness_run_id")
    if not run_id:
        return llm
    llm_config = state.get("llm_config")
    provider_name = llm_config.get("provider") if isinstance(llm_config, dict) else None
    context_snapshot = None
    if include_context:
        ctx = state.get("context", "")
        context_snapshot = {
            "context_len": len(ctx),
            "excluded_context_keys": list(state.get("excluded_context_keys", []) or []),
        } if ctx else None
    return LoggedLLMProvider(
        llm,
        run_id=run_id,
        step_id=state.get("harness_step_id"),
        agent_name=agent_name,
        provider_name=provider_name,
        context_snapshot=context_snapshot,
    )


# --- 默认 system_prompt（硬编码 fallback） ---
DEFAULT_WRITER_PROMPT = """你是一位才华横溢的创意写作大师。你必须严格遵循以下规则：

1. **大纲优先**：严格按照提供的大纲展开情节，不得偏离大纲设定的事件和走向。
2. **角色一致**：角色的性格、说话风格、行为方式必须与角色资料中的设定完全一致，不得出现 OOC（out of character）。
3. **世界观遵守**：所有设定（地理、历史、规则、体系等）必须与世界观设定严格一致，不得自创矛盾设定。
4. **暗线融入**：如果提供了暗线设定，需自然地将其融入情节中，不做显式说明。

在严格遵守以上约束的前提下，发挥创意写出精彩的文学内容。"""
DEFAULT_CRITIC_PROMPT = "你是一位严苛的文学审校大师。对文本进行深度审校，输出结构化评价。"
DEFAULT_CONSISTENCY_PROMPT = (
    "你是一位世界观一致性检查专家。检查文本是否与已知设定矛盾。"
    "检查维度：角色性格/行为一致性、世界观设定一致性、剧情逻辑一致性、时间线一致性。"
    '输出严格JSON：{"issues": [{"type": "character|worldbuilding|plot|timeline|other", "description": "具体矛盾描述", "severity": "info|low|medium|high"}], "summary": "整体结论", "overall_severity": "info|low|medium|high"}'
    "如果没有发现问题，issues 为空数组。只输出JSON，不要输出其他内容。"
)


def _is_revision_state(state: CreativeState) -> bool:
    return state.get("revision_count", 0) > 0


def _build_writer_user_prompt(state: CreativeState) -> str:
    """构建 writer 节点的用户 prompt。

    这里最重要的设计是把“初次生成”和“修订改稿”分开：
    - 初次生成：根据上下文、用户选择方向、补充要求写完整章节；
    - 修订改稿：只能重写当前候选稿，不允许在末尾继续扩写新剧情。

    这样可以避免用户点“修改”后模型误以为要续写，导致章节越改越长。
    """
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
        selected_direction = state.get("selected_direction", "")
        user_note = state.get("user_note", "")
        direction_block = ""
        if selected_direction:
            direction_block += f"\n## 剧情走向\n用户选择了以下走向：{selected_direction}\n请严格遵循此走向展开情节。\n"
        if user_note:
            direction_block += f"\n## 用户补充要求\n{user_note}\n"
        draft_block = (
            f"\n\n## 已有章节草稿（仅作本章参考，不作为续写起点）\n{draft}"
            if draft.strip()
            else "\n\n## 已有章节草稿\n（无，请从本章资料生成完整章节）"
        )
        user_prompt = (
            f"## 上下文\n{context}{direction_block}{draft_block}\n\n"
            "请根据上下文中的本章大纲、角色、设定、检索资料和用户补充，生成“当前章节对应的完整正文”。\n"
            "硬性要求：\n"
            "1. 这是章节生成任务，不是续写任务；不要把前文摘要、已有章节片段或草稿当作续写起点。\n"
            "2. 输出只覆盖本章应发生的内容，不要接写下一章或后续无关剧情。\n"
            "3. 如果已有章节草稿非空，只能作为本章参考素材重组为完整章节；不要在其结尾后继续追加。\n"
            "4. 只输出章节正文，不要输出说明、标题、项目符号或创作分析。"
        )

    if target_words:
        user_prompt += f"\n\n**目标字数：约{target_words}字，请控制篇幅，并以完整句子自然结束。**"
    return user_prompt


def _build_workflow_skill_pack(
    state: CreativeState,
    *,
    node_name: str,
    role_type: str,
    skill_dir: str | None = None,
    expert_name: str | None = None,
):
    """为工作流节点准备 skill pack。

    skill pack 会把本地 Skill.md、引用资料、写作技法等内容拼入 system prompt。
    planner 会根据节点名和 role_type 选择合适的 skill_dir，例如 writer/critic
    使用不同技能包，避免所有专家共用一份笼统 prompt。
    """
    plan = plan_workflow_skill_pack(
        node_name=node_name,
        role_type=role_type,
        skill_dir=skill_dir,
        expert_name=expert_name,
    )
    pack = build_expert_skill_pack(
        plan.role_type,
        skill_dir=plan.skill_dir,
        project_id=state.get("project_id", ""),
        chapter_id=state.get("chapter_id", ""),
        draft=state.get("draft", ""),
        context=state.get("context", ""),
        mode=state.get("mode", ""),
    )
    summary = _skill_pack_summary(pack, plan)
    return pack, summary


def _skill_pack_summary(pack, plan: SkillPackPlan) -> dict:
    """把完整 skill pack 压缩成可通过 SSE 返回给前端的摘要。"""
    summary = pack.to_summary()
    summary["expert"] = plan.event_expert
    summary["planner"] = plan.planner
    summary["planner_reason"] = plan.reason
    return summary


# --- 通用专家节点工厂 ---
def _make_expert_node(
    expert_id: str,
    role_type: str,
    system_prompt: str,
    temperature: float,
    max_tokens: int,
    skill_dir: str | None = None,
):
    """为用户自定义专家创建 LangGraph 节点函数。

    用户在界面上配置的 Expert 不是硬编码函数，而是通过 role_type 决定读写哪些
    state 字段：
    - writer 写 draft；
    - critic/custom/researcher 追加 critiques；
    - editor 写 edited_draft。

    这样新增专家时无需改图结构核心逻辑，只需在 build_creative_graph 中按
    workflow_position 把节点插入到合适位置。
    """
    async def expert_node(state: CreativeState) -> dict:
        llm = get_llm_provider(state.get("llm_config"))
        llm = _maybe_wrap_llm(llm, state, agent_name=f"expert_{expert_id[:8]}", include_context=role_type in ("writer", "researcher", "custom"))
        pack, pack_summary = _build_workflow_skill_pack(
            state,
            node_name=expert_id,
            role_type=role_type,
            skill_dir=skill_dir,
        )
        base_prompt = system_prompt or state.get("writer_prompt", DEFAULT_WRITER_PROMPT)
        prompt = build_expert_system_prompt(role_type, base_prompt, pack)
        skill_update = {"skill_packs": [pack_summary]} if pack.has_content or pack.warnings else {}

        if role_type == "writer":
            user_prompt = _build_writer_user_prompt(state)
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"draft": result, **skill_update}

        elif role_type == "critic":
            user_prompt = f"## 待审校文本\n{state.get('draft', '')}\n\n请审校："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"critiques": [result], **skill_update}

        elif role_type == "editor":
            user_prompt = f"## 待编辑文本\n{state.get('draft', '')}\n\n请编辑润色："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"edited_draft": result, **skill_update}

        else:  # researcher / custom
            user_prompt = f"## 上下文\n{state.get('context', '')}\n\n## 当前草稿\n{state.get('draft', '')}\n\n请分析："
            result = await llm.generate(prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
            return {"critiques": [result], **skill_update}

    expert_node.__name__ = f"expert_{expert_id[:8]}"
    return expert_node


# --- 内置节点函数 ---
async def context_loader_node(state: CreativeState) -> dict:
    """加载创作上下文

    统一使用 ChapterContextService 聚合本章资料 + 用户显式选择的条目。
    无章节号时 fallback 到旧 RAG（向后兼容文章模式 / 旧调用路径）。
    """
    selected_outlines = state.get("selected_outline_ids", [])
    selected_characters = state.get("selected_character_ids", [])
    selected_world_entries = state.get("selected_world_entry_ids", [])
    selected_hidden_threads = state.get("selected_hidden_thread_ids", [])
    excluded_context_keys = list(state.get("excluded_context_keys", []) or [])
    chapter_num = state.get("chapter_num", 0)

    # ── 主路径：有章节号时走 ChapterContextService ──
    # ChapterContextService 是新上下文聚合入口，能同时取本章大纲、角色、
    # 角色事件、世界观、暗线、前文和知识库资料。它比旧 RAG 更贴合“按章节写作”。
    if chapter_num:
        try:
            from services.chapter_context import apply_context_exclusions, build_chapter_context, format_chapter_context_for_prompt
            from db.session import async_session as ctx_async_session
            async with ctx_async_session() as session:
                ctx = await build_chapter_context(
                    session,
                    state["project_id"],
                    chapter_num,
                    intent=state.get("mode", "generate"),
                    user_query="\n".join(
                        part for part in (
                            state.get("selected_direction", ""),
                            state.get("user_note", ""),
                        ) if part
                    ) or None,
                    selected_outline_ids=selected_outlines or None,
                    selected_character_ids=selected_characters or None,
                    selected_world_entry_ids=selected_world_entries or None,
                    selected_hidden_thread_ids=selected_hidden_threads or None,
                    include_knowledge_sources=bool(state.get("include_knowledge_sources", False)),
                    include_previous_summary=bool(state.get("include_previous_summary", False)),
                )
                context_summary = ctx.context_summary(
                    selected_outline_ids=selected_outlines,
                    selected_character_ids=selected_characters,
                    selected_world_entry_ids=selected_world_entries,
                    selected_hidden_thread_ids=selected_hidden_threads,
                    excluded_context_keys=excluded_context_keys,
                )
                filtered_ctx = apply_context_exclusions(ctx, excluded_context_keys)
                context = format_chapter_context_for_prompt(filtered_ctx)
                logger.info(
                    "context_loader: ChapterContextService loaded length=%d stats=%s has_selections=%s",
                    len(context), ctx.stats, bool(selected_outlines or selected_characters or selected_world_entries or selected_hidden_threads),
                )
                return {"context": context, "context_summary": context_summary}
        except Exception as e:
            logger.exception(f"ChapterContextService 加载失败，fallback 到旧逻辑: {e}")
            if excluded_context_keys:
                # 不能在旧的字符串 fallback 中可靠识别稳定 key；宁可不注入上下文，
                # 也不能把用户明确排除的资料重新送入 Writer。
                return {
                    "context": "(本次生成未加载上下文：上下文排除选择无法在 fallback 路径中安全应用)",
                    "context_summary": {"excluded_context_keys": excluded_context_keys},
                }

    # ── 回退：旧逻辑（文章模式 / 无章节号 / ChapterContextService 失败） ──
    # 回退路径保证老接口、文章模式或异常情况下仍能尽量拿到可用上下文。
    has_selections = selected_outlines or selected_characters or selected_world_entries or selected_hidden_threads
    if excluded_context_keys:
        return {
            "context": "(本次生成未加载上下文：当前 fallback 路径不支持逐项排除)",
            "context_summary": {"excluded_context_keys": excluded_context_keys},
        }
    if (state.get("mode", "") or "").lower() == "full_pipeline" and chapter_num:
        # full_pipeline 的硬边界是“只传结构化本章资料 + 上章结尾锚点”。
        # 如果 ChapterContextService 异常，不允许落回旧 RAG/最近三章正文路径，否则会再次把本地文章送入模型。
        return {
            "context": "(本次生成未加载旧 fallback 上下文：full_pipeline 严格模式禁止注入本地前文、旧稿或资料库正文；请检查 ChapterContextService 异常日志)",
            "context_summary": {"excluded_context_keys": list(state.get("excluded_context_keys", []) or []), "strict_full_pipeline": True},
        }
    logger.info(f"context_loader fallback: outlines={selected_outlines}, chars={selected_characters}, we={selected_world_entries}, has_selections={has_selections}")

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
                    current_seq = int(state.get("chapter_num") or 0)
                    conditions = [Chapter.project_id == state["project_id"]]
                    if current_seq:
                        conditions.append(Chapter.sequence_number < current_seq)
                    conditions.extend([Chapter.content.isnot(None), Chapter.content != ""])
                    result = await session.execute(
                        select(Chapter)
                        .where(*conditions)
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
        logger.info(f"context_loader fallback: loaded context length={len(context)}, parts={len(parts)}")
        return {"context": context, "context_summary": {"excluded_context_keys": list(state.get("excluded_context_keys", []) or [])}}

    # 最终 Fallback: 旧 RAG 向量搜索逻辑（向后兼容文章模式 / 无章节号场景）。
    # 如果用户没有显式选择素材，也没有章节号，就由 ContextLoader 自动检索项目资料。
    loader = ContextLoader()
    context = await loader.load_context(
        project_id=state["project_id"],
        chapter_id=state["chapter_id"],
        include_world=True,
        include_characters=True,
        include_previous_chapters=3,
        include_outline=True,
    )
    return {"context": context, "context_summary": {"excluded_context_keys": list(state.get("excluded_context_keys", []) or [])}}


async def writer_node(state: CreativeState) -> dict:
    """创意大师：初次生成章节；修订时改写当前候选稿而不是续写。"""
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="writer", include_context=True)
    pack, pack_summary = _build_workflow_skill_pack(state, node_name="writer", role_type="writer")
    system_prompt = build_expert_system_prompt("writer", state.get("writer_prompt") or DEFAULT_WRITER_PROMPT, pack)
    user_prompt = _build_writer_user_prompt(state)
    result = await llm.generate(system_prompt, user_prompt, temperature=0.8)
    update = {"draft": result}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def critic_node(state: CreativeState) -> dict:
    """残酷大师：结构化审校。

    审校结果追加到 critiques，后续 review/revise 会把这些意见交回 writer。
    """
    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="critic")
    pack, pack_summary = _build_workflow_skill_pack(state, node_name="critic", role_type="critic")
    system_prompt = build_expert_system_prompt("critic", state.get("critic_prompt") or DEFAULT_CRITIC_PROMPT, pack)
    user_prompt = f"## 待审校文本\n{state.get('draft', '')}\n\n请审校："
    result = await llm.generate(system_prompt, user_prompt, temperature=0.3, max_tokens=2048)
    update = {"critiques": [result]}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def consistency_checker_node(state: CreativeState) -> dict:
    """一致性检查：与世界观/角色/前文对照，输出结构化 GuardrailResult。

    该节点关注“是否违背已有设定”，不是文学质量评价。输出会被 parse_guardrail_result
    标准化为 dict，前端可以展示 severity/issues，而不是解析自然语言。
    """
    from agents.guardrail import parse_guardrail_result

    llm = get_llm_provider(state.get("llm_config"))
    llm = _maybe_wrap_llm(llm, state, agent_name="consistency_checker", include_context=True)
    pack, pack_summary = _build_workflow_skill_pack(
        state,
        node_name="consistency_checker",
        role_type="consistency_checker",
    )
    system_prompt = build_expert_system_prompt("consistency_checker", state.get("consistency_prompt") or DEFAULT_CONSISTENCY_PROMPT, pack)
    user_prompt = f"## 上下文/设定\n{state.get('context', '')}\n\n## 待检查文本\n{state.get('draft', '')}\n\n请检查一致性："
    raw_result = await llm.generate(system_prompt, user_prompt, temperature=0.2, max_tokens=2048)
    guardrail_result = parse_guardrail_result(raw_result)
    update = {"consistency_report": guardrail_result}
    if pack.has_content or pack.warnings:
        update["skill_packs"] = [pack_summary]
    return update


async def human_review_node(state: CreativeState) -> dict:
    """人工审批节点（interrupt_before 暂停，等待用户决策）"""
    return {}


# --- 路由函数 ---
def route_after_review(state: CreativeState) -> str:
    """根据修订次数决定是否继续。

    human_review 暂停后，如果用户选择 revise，routes.py 会把 revision_count + 1
    写回 state，再从 human_review 继续。超过上限后直接 END，避免无限循环烧 token。
    """
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
    # StateGraph 只描述节点和边，不立即执行。get_creative_app 会 compile 成可运行 app。
    graph = StateGraph(CreativeState)

    # context_loader 和 human_review 始终存在：
    # 前者保证所有生成都有上下文，后者提供人工审核/批准/修订的暂停点。
    graph.add_node("context_loader", context_loader_node)
    graph.add_node("human_review", human_review_node)
    graph.set_entry_point("context_loader")

    if not enabled_experts:
        # 无专家配置 → 默认流水线：
        # context_loader → writer → [critic || consistency_checker] → human_review。
        # writer 后有两条边，LangGraph 会让审校和一致性检查都消费同一个 draft，
        # 最终在 human_review 前合并状态。
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
    # 用户可在 UI 中把专家放到 pre_writer/post_writer/replace_writer 等位置。
    # 这里把配置转换成实际图拓扑，使“AI 团队”可以配置而不是写死。
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
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens, getattr(exp, "skill_dir", None))
        node_chain.append((name, fn))

    # writer: replace_writer 或默认
    if replace_writer:
        fn = _make_expert_node(str(replace_writer.id), replace_writer.role_type, replace_writer.system_prompt, replace_writer.temperature, replace_writer.max_tokens, getattr(replace_writer, "skill_dir", None))
        node_chain.append(("writer", fn))
    else:
        node_chain.append(("writer", writer_node))

    # post_writer experts
    for i, exp in enumerate(post_writer):
        name = f"post_writer_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens, getattr(exp, "skill_dir", None))
        node_chain.append((name, fn))

    # pre_critic experts
    for i, exp in enumerate(pre_critic):
        name = f"pre_critic_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens, getattr(exp, "skill_dir", None))
        node_chain.append((name, fn))

    # critic: replace_critic 或默认
    if replace_critic:
        fn = _make_expert_node(str(replace_critic.id), replace_critic.role_type, replace_critic.system_prompt, replace_critic.temperature, replace_critic.max_tokens, getattr(replace_critic, "skill_dir", None))
        node_chain.append(("critic", fn))
    else:
        node_chain.append(("critic", critic_node))

    # post_critic experts
    for i, exp in enumerate(post_critic):
        name = f"post_critic_{i}"
        fn = _make_expert_node(str(exp.id), exp.role_type, exp.system_prompt, exp.temperature, exp.max_tokens, getattr(exp, "skill_dir", None))
        node_chain.append((name, fn))

    # consistency_checker 始终使用默认，避免用户自定义专家绕过设定一致性保护。
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
    """获取编译后的工作流应用（带 checkpoint 和 HITL）。

    interrupt_before=["human_review"] 表示图执行到 human_review 之前暂停。
    routes.py 会把 thread_id 返回给前端，用户点击批准/修订时再调用 resume 接口继续。
    """
    graph = build_creative_graph(enabled_experts)
    app = graph.compile(
        checkpointer=_CHECKPOINTER,
        interrupt_before=["human_review"],
    )
    return app
