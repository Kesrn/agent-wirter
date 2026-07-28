"""API 路由。

这个文件承载主要业务 HTTP 接口：
- 项目/章节/文档/素材/专家的 CRUD；
- 章节生成、续写、润色、审校和 HITL 恢复；
- 知识库上传、切片、抽取、搜索、问答；
- AI Run、步骤日志、人工决策和写作记忆审核。

普通 CRUD 基本遵循“校验项目归属 -> 查询/修改 ORM -> commit -> 返回 schema”。
复杂逻辑被拆到 services/agents/harness 中，本文件主要负责 HTTP 参数、权限、
事务边界、SSE 事件封装和前端兼容。
"""

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field
from urllib.parse import quote
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, func

from db.session import get_db
from models.project import Project
from models.expert import Expert
from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.chapter_review_note import ChapterReviewNote
from models.ai_run import AiRun
from models.ai_run_step import AiRunStep
from models.llm_call_log import LlmCallLog
from models.document import Document
from models.document_version import DocumentVersion
from models.generation_record import GenerationRecord
from models.evaluation import EvaluationDataset, EvaluationCase, EvaluationRun, EvaluationResult
from models.world_entry import WorldEntry
from models.character import Character
from models.character_event import CharacterEvent
from models.character_relation import CharacterRelation
from models.outline import Outline
from models.hidden_thread import HiddenThread
from models.story_arc import StoryArc
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.project_knowledge_fact import ProjectKnowledgeFact
from models.knowledge_qa_session import KnowledgeQaSession
from models.knowledge_qa_message import KnowledgeQaMessage
from models.writing_memory_staging import WritingMemoryStaging
from models.harness_enums import InterruptDecision, RunStatus
from models.structured_knowledge import (
    CharacterProfile, AbilityProfile, EventTimeline, WorldRule, CharacterAppearance,
)
from schemas.api import (
    ProjectCreate, ProjectUpdate, ProjectResponse,
    TxtImportResponse,
    ExpertCreate, ExpertUpdate, ExpertResponse,
    ChapterCreate, ChapterResponse, ChapterUpdate, ChapterFinalizeRequest,
    ChapterReviewNoteCreate, ChapterReviewNoteUpdate, ChapterReviewNoteResponse,
    ChapterStructureExtractRequest, ChapterStructureExtractResponse,
    ChapterVersionResponse, ChapterVersionListItemResponse, ChapterVersionDiffRequest, ChapterVersionDiffResponse,
    WorldEntryCreate, WorldEntryUpdate, WorldEntryResponse,
    CharacterCreate, CharacterUpdate, CharacterMergeRequest, CharacterResponse,
    CharacterEventUpsert, CharacterEventResponse,
    CharacterArcResponse,
    CharacterRelationCreate, CharacterRelationUpdate, CharacterRelationResponse,
    OutlineCreate, OutlineUpdate, OutlineResponse,
    HiddenThreadCreate, HiddenThreadUpdate, HiddenThreadResponse,
    StoryArcCreate, StoryArcUpdate, StoryArcResponse,
    GenerateRequest, ExpertTestRequest,
    DocumentCreate, DocumentUpdate, DocumentResponse,
    DocumentVersionListItemResponse, DocumentVersionResponse,
    DocumentVersionDiffRequest, DocumentVersionDiffResponse,
    GenerationRecordListItemResponse, GenerationRecordResponse,
    GenerationRecordUpdate, GenerationRecordDiffRequest, GenerationRecordDiffResponse,
    EvaluationDatasetCreate, EvaluationDatasetUpdate, EvaluationDatasetResponse,
    EvaluationCaseCreate, EvaluationCaseUpdate, EvaluationCaseResponse,
    ProjectSourceCreate, ProjectSourceUpdate, ProjectSourceResponse,
    ProjectSourceListResponse, ProjectSourceChunkResponse,
    KnowledgeQaSessionResponse, KnowledgeQaSessionUpdateRequest, KnowledgeQaMessageResponse,
    KnowledgeSearchRequest, KnowledgeAskRequest,
    EvaluationRunCreate, EvaluationRunResponse, EvaluationResultResponse,
    AiRunResponse, AiRunListItemResponse, AiRunStepResponse, AiRunContextResponse, AiRunContextCallResponse,
    HumanDecisionRequest, HumanDecisionResponse,
    ClarificationAnswerRequest, ClarificationResponse,
    WritingMemoryStagingResponse,
    AuthUser,
)
from agents.safety import validate_expert_safety
from agents.expert_templates import ALL_BUILTIN_EXPERTS, BUILTIN_EXPERTS
from agents.llm_provider import LLMConfigError, get_llm_provider
from agents.workflow import get_creative_app, CreativeState
from agents.workflow_v2 import (
    CreativeStateV2,
    WorkflowGenerationError,
    get_creative_app_v2,
    get_creative_app_v2_continue,
)
from harness.run_manager import (
    create_run, mark_running, mark_waiting_human, mark_completed, mark_failed, mark_cancelled,
    build_expert_snapshot, discard_run_artifacts, workflow_to_snapshot,
)
from harness.step_logger import start_step, finish_step, fail_step
from harness.human_interrupt_service import (
    create_interrupt, resolve_interrupt, get_interrupt_by_run, get_interrupt_by_thread,
    get_unresolved_interrupt_by_thread_step, check_interrupt_resolved,
)
from skills.runner import ExpertSkillResult, build_expert_skill_pack, build_expert_system_prompt
from api.auth import get_current_user
from api.llm_deps import get_user_llm_config
from api.rate_limiter import agent_limiter
from rag.embedding_service import generate_embedding, _update_embedding_bg
from services.diff_service import compute_diff
from services.chapter_save import finalize_chapter_content, save_chapter_content
from agents.guardrail import guardrail_to_text as _guardrail_to_text, get_blocking_issues
from services.document_save import save_document_content
from services.skill_pack_planner import SkillPackPlan, plan_direct_skill_pack
from services.txt_import import decode_txt_bytes, split_txt_into_chapters, build_import_meta
from services.structure_extraction import (
    apply_structure_extraction,
    build_fallback_structure_from_chapter,
    build_structure_preview,
    extract_structure_with_provider,
    filter_structure_targets,
)
from services.generation_record_service import (
    create_generation_record,
    get_generation_record,
    list_generation_records_for_chapter,
    list_generation_records_for_document,
    update_generation_record_status,
)
from services.evaluation import default_rubric, normalize_expected_properties, normalize_rubric, run_evaluation_case
from services.memory_staging_service import run_fact_extraction, run_story_recorder_extraction
from observability.langfuse import activate_langfuse_context, current_langfuse_trace_id, finish_langfuse_context
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


async def _discard_incomplete_generation_run(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID | None,
    thread_id: str | None,
    reason: str,
) -> None:
    """失败/断连后清空本次生成已提交的运行痕迹与内存 checkpoint。"""
    # 先丢弃当前 session 还没提交的半成品（例如刚写入的候选稿或版本），再开启
    # 一个干净事务清除之前已经提交的 run/step/LLM 日志等关联记录。
    try:
        await db.rollback()
    except Exception:
        logger.warning("生成失败后的事务回滚失败", exc_info=True)

    if run_id:
        try:
            await discard_run_artifacts(db, run_id=run_id)
            await db.commit()
            logger.info("已清理未完成生成 run_id=%s reason=%s", run_id, reason)
        except Exception:
            await db.rollback()
            logger.exception("清理未完成生成失败 run_id=%s", run_id)

    if thread_id:
        try:
            # MemorySaver 是内存级别；数据库已删时也必须删 checkpoint，防止旧
            # thread_id 被再次 resume 后产生“幽灵流程”。
            from agents.workflow import _CHECKPOINTER
            await _CHECKPOINTER.adelete_thread(thread_id)
        except Exception:
            logger.warning("清理工作流 checkpoint 失败 thread_id=%s", thread_id, exc_info=True)

# 只有 writer 角色的输出会写入章节正文。critic/editor/researcher 等专家输出
# 一般用于审校、建议或中间状态，不应直接覆盖用户正文。
WRITER_ROLES = ("writer",)
# 润色输出字数限制根据全局 token 上限推导，避免用户一次润色超长章节导致模型截断。
ENHANCE_MAX_OUTPUT_WORDS = min(5000, max(1000, int(settings.MAX_TOKENS_LIMIT * 0.65)))
ENHANCE_MIN_OUTPUT_WORDS = 20
ALLOWED_IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _format_bytes_limit(size: int) -> str:
    """把字节数格式化成用户可读的 KB/MB，用于上传限制错误提示。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:g}MB"
    if size >= 1024:
        return f"{size / 1024:g}KB"
    return f"{size}B"


def _parse_directions(text: str) -> list[str]:
    """从容错解析 LLM 输出为字符串列表。尝试 JSON 数组，失败则按换行分割。

    用于“续写方向/润色方向/修改方向”这类接口。真实模型不一定严格输出 JSON，
    所以这里做兼容，避免前端拿不到可选项。
    """
    text = text.strip()
    # 尝试提取 JSON 数组
    try:
        # 找到 [ ... ] 部分
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            items = json.loads(match.group())
            if isinstance(items, list):
                return [str(s).strip().lstrip('0123456789.、)）') for s in items if str(s).strip()]
    except (json.JSONDecodeError, ValueError):
        pass
    # 按换行分割
    lines = [l.strip().lstrip('0123456789.、)）') for l in text.split('\n') if l.strip()]
    return lines if lines else ["方向1", "方向2", "方向3"]


def _skill_pack_sse_event(pack: dict | None, fallback_expert: str = "") -> str:
    """把 skill pack 摘要包装成 SSE 事件。

    skill pack 可能包含本次专家节点注入了哪些 Skill/引用资料、是否截断等信息。
    独立事件不影响旧客户端解析 writer_output/done。
    """
    if not pack:
        return ""
    payload = {
        "expert": pack.get("expert") or fallback_expert,
        "skill": pack.get("skill", ""),
        "skill_dir": pack.get("skill_dir", ""),
        "sources": pack.get("sources", []),
        "warnings": pack.get("warnings", []),
        "token_estimate": pack.get("token_estimate", 0),
        "truncated": pack.get("truncated", False),
        "has_content": pack.get("has_content", False),
    }
    return f"event: skill_pack\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _new_skill_packs(output: dict, seen_keys: set[tuple[str, str]]) -> list[dict]:
    """从 LangGraph 节点输出中取新增 skill pack。

    同一个 workflow 可能多次 resume，同一个 pack 也可能随 state 反复出现。
    seen_keys 用于去重，避免前端展示重复的技能包。
    """
    packs = output.get("skill_packs", []) if isinstance(output, dict) else []
    result = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        key = (str(pack.get("expert", "")), str(pack.get("skill_dir", "")))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(pack)
    return result


def _embedded_clarification_payload(values: dict) -> dict | None:
    """构造随任务卡展示的 L-2 可选澄清 payload。"""
    if not values.get("embedded_clarification"):
        return None
    # 用户提交回答后，问题列表为了审计仍保留在 State 中，但此时已被标记为完成。
    # 不能仅因 questions 非空就再次下发，否则任务卡重新规划后会反复展示同一批问题。
    if not values.get("needs_clarification"):
        return None
    questions = values.get("clarification_questions", []) or []
    if not questions:
        return None
    mode = str(values.get("pre_generation_mode", "PLANNING")).upper()
    round_num = int(values.get("clarification_round", 1) or 1)
    return {
        "needs_clarification": bool(values.get("needs_clarification", False)),
        "questions": questions[:3],
        "assumptions_if_skipped": values.get("clarification_assumptions", []) or [],
        "round": round_num,
        "max_rounds": int(values.get("max_clarification_rounds", 3) or 3),
        "require_answer": mode == "STRICT" and round_num <= 1,
    }


def _task_card_clarification_status(values: dict) -> str:
    """返回任务卡中展示的生成前澄清状态。

    ``clarification_questions`` 会为了审计保留在 State，不能据此判断当前是否仍要
    澄清。这里单独下发状态，避免前端把“无需澄清”“已回答”与“FAST 跳过”混为一谈。
    """
    mode = str(values.get("pre_generation_mode", "PLANNING")).upper()
    if mode == "FAST" or not values.get("embedded_clarification"):
        return "skipped"
    if values.get("needs_clarification"):
        return "pending"
    if values.get("clarification_answers"):
        return "answered"
    if values.get("clarification_skipped"):
        return "skipped"
    return "not_needed"


def _direct_skill_pack(
    role_type: str,
    *,
    event_expert: str,
    project_id: str,
    skill_dir: str | None = None,
    chapter_id: str = "",
    draft: str = "",
    context: str = "",
    mode: str = "",
) -> tuple[ExpertSkillResult, dict]:
    """为非 LangGraph 直连路径构建 skill pack。

    continue/enhance/summarize 的部分分支不是完整工作流节点，但仍需要复用
    Skill.md 和引用资料。这个 helper 会把摘要中的 expert 字段对齐到前端步骤名。
    """
    pack = build_expert_skill_pack(
        role_type,
        skill_dir=skill_dir,
        project_id=project_id,
        chapter_id=chapter_id,
        draft=draft,
        context=context,
        mode=mode,
    )
    summary = pack.to_summary()
    summary["expert"] = event_expert
    return pack, summary


def _planned_direct_skill_pack(
    plan: SkillPackPlan | None,
    *,
    project_id: str,
    chapter_id: str = "",
    draft: str = "",
    context: str = "",
    mode: str = "",
) -> tuple[ExpertSkillResult, dict]:
    if plan is None:
        raise ValueError("No direct skill pack plan is available for this generation branch")
    pack, summary = _direct_skill_pack(
        plan.role_type,
        skill_dir=plan.skill_dir,
        event_expert=plan.event_expert,
        project_id=project_id,
        chapter_id=chapter_id,
        draft=draft,
        context=context,
        mode=mode,
    )
    summary["planner"] = plan.planner
    summary["planner_reason"] = plan.reason
    return pack, summary


def _count_non_space_chars(text: str) -> int:
    """按前端展示习惯粗略计数字数：忽略空白字符，其余字符计 1。"""
    return len(re.sub(r"\s+", "", text or ""))


def _enhance_word_budget(chapter_content: str, requested_words: int | None) -> tuple[int, int, int, int, int]:
    """Return source_words, target_words, min_words, max_words, max_tokens for polish output."""
    source_words = _count_non_space_chars(chapter_content)
    if source_words <= 0:
        raise ValueError("当前章节为空，无法润色")

    target_words = requested_words or source_words
    target_words = max(ENHANCE_MIN_OUTPUT_WORDS, target_words)

    if target_words > ENHANCE_MAX_OUTPUT_WORDS:
        raise ValueError(
            f"目标字数过长（{target_words}字）。单次润色最多支持约{ENHANCE_MAX_OUTPUT_WORDS}字，"
            "请降低目标字数或拆分章节后再润色。"
        )

    min_words = max(ENHANCE_MIN_OUTPUT_WORDS, int(target_words * 0.85))
    max_words = max(min_words, int(target_words * 1.15))
    max_words = min(max_words, ENHANCE_MAX_OUTPUT_WORDS)
    max_tokens = min(settings.MAX_TOKENS_LIMIT, max(1024, int(max_words * 1.8) + 512))
    return source_words, target_words, min_words, max_words, max_tokens


def _article_brief(req: GenerateRequest) -> str:
    """把文章/文案生成参数压缩成 prompt brief。

    小说项目依赖角色/世界观/大纲，文章项目则更依赖平台、受众、内容目标和语气。
    这个 brief 会进入文章生成、续写、润色等分支。
    """
    items = [
        ("内容类型", req.content_type or "通用文章/文案"),
        ("发布平台", req.platform or "未指定"),
        ("目标受众", req.audience or "未指定"),
        ("内容目标", req.content_goal or "未指定"),
        ("语气风格", req.tone or "未指定"),
        ("核心要点", req.key_points or "未指定"),
    ]
    if req.target_words:
        items.append(("目标字数", f"约{req.target_words}字"))
    return "\n".join(f"- {key}：{value}" for key, value in items)


def _article_system_prompt(task: str) -> str:
    """文章/文案项目统一 system prompt。

    明确告诉模型“这不是小说创作”，防止复用小说项目的续写、角色行动、
    世界观设定等表达方式。
    """
    return (
        f"你是一位专业中文内容策划和文案编辑，当前任务是{task}。"
        "你服务的是文章/文案项目，不是小说创作。"
        "禁止使用小说章节、剧情续写、角色登场、世界观设定、伏笔推进等叙事小说口吻。"
        "输出要围绕主题、受众、平台、结构、表达目标和行动引导。"
        "除非用户明确要求，不能输出解释性前缀、修改说明或项目符号清单；正文任务只输出可直接使用的正文。"
    )


def _article_generate_prompt(req: GenerateRequest, creative_context: str, current_content: str) -> str:
    """构建文章/文案正文生成 prompt。"""
    source = current_content.strip() or "（当前稿件为空，请根据 brief 生成完整内容）"
    return (
        f"## 内容 brief\n{_article_brief(req)}\n\n"
        f"## 可用上下文\n{creative_context or '无'}\n\n"
        f"## 当前稿件\n{source}\n\n"
        "请生成一篇完整、可直接发布或继续编辑的文章/文案。要求：\n"
        "1. 结构清晰，有明确开头、主体和收束。\n"
        "2. 内容必须服务于 brief 中的受众、平台和目标。\n"
        "3. 不写小说情节，不续写故事，不安排角色行动。\n"
        "4. 如果当前稿件已有内容，可以在保留主题的基础上重组和补全；不要简单接在原文后面续写。\n"
        "5. 只输出正文。"
    )


def _generation_list_item(record: GenerationRecord) -> GenerationRecordListItemResponse:
    """把 GenerationRecord ORM 转成列表项响应。

    列表页只需要摘要字段，详情内容通过单独接口读取，避免一次返回大量正文。
    """
    return GenerationRecordListItemResponse(
        id=record.id,
        project_id=record.project_id,
        chapter_id=record.chapter_id,
        document_id=record.document_id,
        run_id=record.run_id,
        mode=record.mode,
        expert_id=record.expert_id,
        direction=record.direction,
        word_count=record.word_count,
        status=record.status,
        langfuse_trace_id=record.langfuse_trace_id,
        created_at=record.created_at,
    )


def _extract_context_text_from_prompt(prompt: str | None) -> str | None:
    """从 rendered_prompt_snapshot 中抽取用户真正关心的上下文段。

    不按任意下一个 `##` 截断，因为 Context Builder 生成的上下文本身包含
    `## 本章大纲`、`## 角色资料` 等多个小节。只在已知的后续 prompt 小节处截断。
    """
    if not prompt:
        return None
    markers = ("## 上下文/设定", "## 可用上下文", "## 上下文")
    start = -1
    for marker in markers:
        idx = prompt.find(marker)
        if idx >= 0:
            start = idx + len(marker)
            break
    if start < 0:
        return None
    context = prompt[start:].lstrip(" \n:")
    stop_markers = (
        "\n## 执行重点",
        "\n## 用户补充要求",
        "\n\n## 用户本轮写作要求",
        "\n\n## 执行重点",
        "\n\n## 用户补充要求",
        "\n\n## 章节信息",
        "\n\n## 修改方向",
        "\n\n## 当前候选稿",
        "\n\n## 待检查文本",
        "\n\n## 当前稿件",
        "\n\n请根据",
        "\n\n请分析",
        "\n\n请检查",
    )
    stops = [pos for marker in stop_markers if (pos := context.find(marker)) >= 0]
    if stops:
        context = context[:min(stops)]
    context = context.strip()
    return context or None


def _prompt_snapshot_was_truncated(request_meta: dict | None) -> bool:
    """判断 LLM call log 中的 prompt 快照是否因长度限制被截断。"""
    return bool(isinstance(request_meta, dict) and request_meta.get("prompt_snapshot_truncated"))


def _evaluation_dataset_response(dataset: EvaluationDataset, case_count: int = 0, run_count: int = 0) -> EvaluationDatasetResponse:
    return EvaluationDatasetResponse(
        id=dataset.id,
        project_id=dataset.project_id,
        name=dataset.name,
        description=dataset.description,
        mode=dataset.mode,
        status=dataset.status,
        case_count=case_count,
        run_count=run_count,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def _evaluation_run_response(run: EvaluationRun, results: list[EvaluationResult] | None = None) -> EvaluationRunResponse:
    return EvaluationRunResponse(
        id=run.id,
        dataset_id=run.dataset_id,
        project_id=run.project_id,
        name=run.name,
        generation_mode=run.generation_mode,
        status=run.status,
        model_provider=run.model_provider,
        model_id=run.model_id,
        total_cases=run.total_cases,
        completed_cases=run.completed_cases,
        failed_cases=run.failed_cases,
        average_score=run.average_score,
        summary=run.summary,
        results=[EvaluationResultResponse.model_validate(result) for result in (results or [])],
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _to_uuid(value: str) -> uuid.UUID:
    """把路径参数中的字符串转换为 UUID，格式错误时返回 400。"""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 UUID 格式")


async def _verify_project_owner(project_id: uuid.UUID, user_id: str, db: AsyncSession) -> Project:
    """校验项目存在且属于当前用户，否则 404。

    业务接口都以 project_id 为边界隔离数据。返回 404 而不是 403，可以避免暴露
    “这个项目 ID 存在但不属于你”的信息。
    """
    result = await db.execute(select(Project).where(Project.id == project_id, Project.owner_id == user_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _delete_project_tree(project_id: uuid.UUID, db: AsyncSession) -> None:
    """删除项目及其项目内所有关联数据。

    这里显式删除而不是依赖数据库级 cascade，是为了兼容 PostgreSQL/SQLite
    以及历史表结构，确保桌面端本地数据库也能完整清理。
    """
    chapter_ids = select(Chapter.id).where(Chapter.project_id == project_id)
    document_ids = select(Document.id).where(Document.project_id == project_id)

    await db.execute(delete(EvaluationResult).where(EvaluationResult.project_id == project_id))
    await db.execute(delete(EvaluationRun).where(EvaluationRun.project_id == project_id))
    await db.execute(delete(EvaluationCase).where(EvaluationCase.project_id == project_id))
    await db.execute(delete(EvaluationDataset).where(EvaluationDataset.project_id == project_id))
    await db.execute(delete(GenerationRecord).where(GenerationRecord.project_id == project_id))
    await db.execute(delete(KnowledgeQaMessage).where(KnowledgeQaMessage.project_id == project_id))
    await db.execute(delete(ProjectSourceChunk).where(ProjectSourceChunk.project_id == project_id))
    await db.execute(delete(ProjectKnowledgeFact).where(ProjectKnowledgeFact.project_id == project_id))
    await db.execute(delete(CharacterAppearance).where(CharacterAppearance.project_id == project_id))
    await db.execute(delete(KnowledgeQaSession).where(KnowledgeQaSession.project_id == project_id))
    await db.execute(delete(ProjectSource).where(ProjectSource.project_id == project_id))
    await db.execute(delete(ChapterReviewNote).where(ChapterReviewNote.project_id == project_id))
    await db.execute(delete(ChapterVersion).where(ChapterVersion.chapter_id.in_(chapter_ids)))
    await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)))
    await db.execute(delete(CharacterEvent).where(CharacterEvent.project_id == project_id))
    await db.execute(delete(CharacterRelation).where(CharacterRelation.project_id == project_id))
    await db.execute(delete(Outline).where(Outline.project_id == project_id))
    await db.execute(delete(HiddenThread).where(HiddenThread.project_id == project_id))
    await db.execute(delete(WorldEntry).where(WorldEntry.project_id == project_id))
    await db.execute(delete(Character).where(Character.project_id == project_id))
    await db.execute(delete(Expert).where(Expert.project_id == project_id))
    await db.execute(delete(Chapter).where(Chapter.project_id == project_id))
    await db.execute(delete(Document).where(Document.project_id == project_id))
    await db.execute(delete(Project).where(Project.id == project_id))


async def _build_structure_context(project_id: uuid.UUID, db: AsyncSession) -> str:
    """Build compact existing project context so extraction can avoid duplicates."""
    char_result = await db.execute(
        select(Character.name).where(Character.project_id == project_id).order_by(Character.created_at.asc()).limit(40)
    )
    outline_result = await db.execute(
        select(Outline.sequence_number, Outline.title).where(Outline.project_id == project_id).order_by(Outline.sequence_number.asc()).limit(80)
    )
    world_result = await db.execute(
        select(WorldEntry.title).where(WorldEntry.project_id == project_id).order_by(WorldEntry.created_at.asc()).limit(40)
    )
    thread_result = await db.execute(
        select(HiddenThread.name).where(HiddenThread.project_id == project_id).order_by(HiddenThread.created_at.asc()).limit(40)
    )

    characters = [name for name in char_result.scalars().all() if name]
    outlines = [f"{seq}. {title}" for seq, title in outline_result.all() if title]
    world_entries = [title for title in world_result.scalars().all() if title]
    hidden_threads = [name for name in thread_result.scalars().all() if name]

    sections = [
        ("已有角色", characters),
        ("已有大纲", outlines),
        ("已有世界观", world_entries),
        ("已有暗线", hidden_threads),
    ]
    return "\n".join(f"{label}: {', '.join(values) if values else '无'}" for label, values in sections)


async def _get_project_chapter(db: AsyncSession, project_id: uuid.UUID, sequence_number: int) -> Chapter:
    result = await db.execute(
        select(Chapter).where(Chapter.project_id == project_id, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    return chapter


async def _get_chapter_review_note(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    note_id: uuid.UUID,
) -> ChapterReviewNote:
    result = await db.execute(
        select(ChapterReviewNote).where(
            ChapterReviewNote.id == note_id,
            ChapterReviewNote.project_id == project_id,
            ChapterReviewNote.chapter_id == chapter_id,
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="审阅备注不存在")
    return note


def _image_extension_from_bytes(content_type: str, data: bytes) -> str | None:
    content_type = content_type.split(";", 1)[0].strip().lower()
    ext = ALLOWED_IMAGE_TYPES.get(content_type)
    if not ext:
        return None
    if content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ext
    if content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        return ext
    if content_type == "image/gif" and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        return ext
    if content_type == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ext
    return None


@router.post("/projects/{project_id}/assets/images")
async def upload_project_image(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="图片不能超过 5MB")
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的上传大小")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 5MB")

    content_type = request.headers.get("content-type", "")
    ext = _image_extension_from_bytes(content_type, data)
    if not ext:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG、GIF、WebP 图片")

    relative_dir = os.path.join("projects", str(uid), "images")
    target_dir = os.path.join(settings.UPLOAD_DIR, relative_dir)
    os.makedirs(target_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.{ext}"
    target_path = os.path.join(target_dir, filename)
    with open(target_path, "wb") as f:
        f.write(data)

    url_path = f"/media/projects/{uid}/images/{filename}"
    return {
        "url": url_path,
        "filename": filename,
        "content_type": content_type.split(";", 1)[0].strip().lower(),
        "size": len(data),
    }


# ==================== 项目 ====================

@router.post("/projects", response_model=ProjectResponse)
async def create_project(
    req: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = Project(
        title=req.title,
        description=req.description,
        overall_outline=req.overall_outline,
        genre=(req.genre or "").strip() or None,
        style=(req.style or "").strip() or None,
        target_words=req.target_words,
        mode=req.mode,
        owner_id=user.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    for tpl in ALL_BUILTIN_EXPERTS:
        expert = Expert(
            project_id=project.id,
            is_builtin=True,
            **tpl,
        )
        db.add(expert)
    await db.commit()

    return project


@router.post("/projects/import-txt", response_model=TxtImportResponse)
async def import_txt_project(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    description: str | None = Form(default=None),
    target_words: int | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    filename = file.filename or "未命名.txt"
    if not filename.lower().endswith(".txt"):
        raise HTTPException(status_code=415, detail="仅支持 TXT 文件")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.MAX_TXT_IMPORT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"TXT 文件不能超过 {_format_bytes_limit(settings.MAX_TXT_IMPORT_BYTES)}",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的上传大小")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="TXT 文件内容为空")
    if len(data) > settings.MAX_TXT_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"TXT 文件不能超过 {_format_bytes_limit(settings.MAX_TXT_IMPORT_BYTES)}",
        )
    if target_words is not None and target_words < 1:
        raise HTTPException(status_code=400, detail="目标字数必须大于 0")

    decoded = decode_txt_bytes(data, return_info=True)
    text = decoded.text.strip()
    chapters_payload = split_txt_into_chapters(text, filename=filename)
    if not chapters_payload:
        raise HTTPException(status_code=400, detail="TXT 文件没有可导入的正文")

    project_title = (title or os.path.splitext(filename)[0] or "导入小说").strip()[:200]
    if not project_title:
        project_title = "导入小说"

    project = Project(
        title=project_title,
        description=(description or "").strip() or None,
        overall_outline=None,
        target_words=target_words or 200000,
        mode="novel",
        owner_id=user.id,
    )

    created_chapters: list[Chapter] = []
    try:
        db.add(project)
        await db.flush()

        for tpl in ALL_BUILTIN_EXPERTS:
            db.add(Expert(project_id=project.id, is_builtin=True, **tpl))

        for item in chapters_payload:
            chapter = Chapter(
                project_id=project.id,
                title=item["title"],
                sequence_number=item["sequence_number"],
            )
            db.add(chapter)
            await db.flush()
            await save_chapter_content(db, chapter, item.get("content") or "", source="manual")
            created_chapters.append(chapter)

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(project)
    for chapter in created_chapters:
        await db.refresh(chapter)

    import_meta = build_import_meta(filename=filename, data=data, chapters=chapters_payload)
    return {
        "project": project,
        "chapters": import_meta["chapters"],
        "import_meta": import_meta,
    }


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id)
        .order_by(Project.created_at.desc())
    )
    return result.scalars().all()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    result = await db.execute(select(Project).where(Project.id == uid, Project.owner_id == user.id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    req: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    project = await _verify_project_owner(uid, user.id, db)
    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    await db.commit()
    await db.refresh(project)
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    try:
        await _delete_project_tree(uid, db)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    project_media_dir = os.path.join(settings.UPLOAD_DIR, "projects", str(uid))
    try:
        if os.path.isdir(project_media_dir):
            shutil.rmtree(project_media_dir)
    except OSError as exc:
        logger.warning("删除项目媒体目录失败 %s: %s", project_media_dir, exc)

    return None


# ==================== 评测集 ====================

async def _get_evaluation_dataset(db: AsyncSession, project_id: uuid.UUID, dataset_id: uuid.UUID) -> EvaluationDataset:
    result = await db.execute(
        select(EvaluationDataset).where(
            EvaluationDataset.id == dataset_id,
            EvaluationDataset.project_id == project_id,
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset:
        raise HTTPException(status_code=404, detail="评测集不存在")
    return dataset


async def _get_evaluation_case(db: AsyncSession, project_id: uuid.UUID, dataset_id: uuid.UUID, case_id: uuid.UUID) -> EvaluationCase:
    result = await db.execute(
        select(EvaluationCase).where(
            EvaluationCase.id == case_id,
            EvaluationCase.dataset_id == dataset_id,
            EvaluationCase.project_id == project_id,
        )
    )
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="评测样本不存在")
    return case


async def _evaluation_dataset_counts(db: AsyncSession, dataset_id: uuid.UUID) -> tuple[int, int]:
    case_count = await db.scalar(select(func.count()).select_from(EvaluationCase).where(EvaluationCase.dataset_id == dataset_id))
    run_count = await db.scalar(select(func.count()).select_from(EvaluationRun).where(EvaluationRun.dataset_id == dataset_id))
    return int(case_count or 0), int(run_count or 0)


@router.get("/projects/{project_id}/eval-datasets", response_model=list[EvaluationDatasetResponse])
async def list_evaluation_datasets(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(EvaluationDataset)
        .where(EvaluationDataset.project_id == uid)
        .order_by(EvaluationDataset.created_at.desc())
    )
    datasets = result.scalars().all()
    responses: list[EvaluationDatasetResponse] = []
    for dataset in datasets:
        case_count, run_count = await _evaluation_dataset_counts(db, dataset.id)
        responses.append(_evaluation_dataset_response(dataset, case_count, run_count))
    return responses


@router.post("/projects/{project_id}/eval-datasets", response_model=EvaluationDatasetResponse)
async def create_evaluation_dataset(
    project_id: str,
    req: EvaluationDatasetCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    dataset = EvaluationDataset(
        project_id=uid,
        name=req.name.strip(),
        description=(req.description or "").strip() or None,
        mode=req.mode,
        status="active",
    )
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return _evaluation_dataset_response(dataset)


@router.patch("/projects/{project_id}/eval-datasets/{dataset_id}", response_model=EvaluationDatasetResponse)
async def update_evaluation_dataset(
    project_id: str,
    dataset_id: str,
    req: EvaluationDatasetUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    await _verify_project_owner(uid, user.id, db)
    dataset = await _get_evaluation_dataset(db, uid, did)
    if req.name is not None:
        dataset.name = req.name.strip()
    if req.description is not None:
        dataset.description = req.description.strip() or None
    if req.status is not None:
        dataset.status = req.status
    await db.commit()
    await db.refresh(dataset)
    case_count, run_count = await _evaluation_dataset_counts(db, dataset.id)
    return _evaluation_dataset_response(dataset, case_count, run_count)


@router.delete("/projects/{project_id}/eval-datasets/{dataset_id}", status_code=204)
async def delete_evaluation_dataset(
    project_id: str,
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    await _verify_project_owner(uid, user.id, db)
    await _get_evaluation_dataset(db, uid, did)
    await db.execute(delete(EvaluationDataset).where(EvaluationDataset.id == did, EvaluationDataset.project_id == uid))
    await db.commit()
    return None


@router.get("/projects/{project_id}/eval-datasets/{dataset_id}/cases", response_model=list[EvaluationCaseResponse])
async def list_evaluation_cases(
    project_id: str,
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    project = await _verify_project_owner(uid, user.id, db)
    await _get_evaluation_dataset(db, uid, did)
    result = await db.execute(
        select(EvaluationCase)
        .where(EvaluationCase.dataset_id == did, EvaluationCase.project_id == uid)
        .order_by(EvaluationCase.created_at.desc())
    )
    cases = result.scalars().all()
    for case in cases:
        if not case.rubric:
            case.rubric = default_rubric(project.mode)
    return cases


@router.post("/projects/{project_id}/eval-datasets/{dataset_id}/cases", response_model=EvaluationCaseResponse)
async def create_evaluation_case(
    project_id: str,
    dataset_id: str,
    req: EvaluationCaseCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    project = await _verify_project_owner(uid, user.id, db)
    await _get_evaluation_dataset(db, uid, did)
    case = EvaluationCase(
        dataset_id=did,
        project_id=uid,
        name=req.name.strip(),
        task_type=req.task_type.strip() or "creative_generation",
        input_text=req.input_text or "",
        actual_output=(req.actual_output or "").strip() or None,
        reference_output=(req.reference_output or "").strip() or None,
        expected_properties=normalize_expected_properties(req.expected_properties),
        rubric=normalize_rubric(req.rubric, project.mode),
        tags=req.tags or [],
        status="active",
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case


@router.patch("/projects/{project_id}/eval-datasets/{dataset_id}/cases/{case_id}", response_model=EvaluationCaseResponse)
async def update_evaluation_case(
    project_id: str,
    dataset_id: str,
    case_id: str,
    req: EvaluationCaseUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    cid = _to_uuid(case_id)
    project = await _verify_project_owner(uid, user.id, db)
    case = await _get_evaluation_case(db, uid, did, cid)
    data = req.model_dump(exclude_unset=True)
    if "name" in data and req.name is not None:
        case.name = req.name.strip()
    if "task_type" in data and req.task_type is not None:
        case.task_type = req.task_type.strip() or "creative_generation"
    if "input_text" in data and req.input_text is not None:
        case.input_text = req.input_text
    if "actual_output" in data:
        case.actual_output = (req.actual_output or "").strip() or None
    if "reference_output" in data:
        case.reference_output = (req.reference_output or "").strip() or None
    if "expected_properties" in data:
        case.expected_properties = normalize_expected_properties(req.expected_properties)
    if "rubric" in data:
        case.rubric = normalize_rubric(req.rubric, project.mode)
    if "tags" in data:
        case.tags = req.tags or []
    if req.status is not None:
        case.status = req.status
    await db.commit()
    await db.refresh(case)
    return case


@router.delete("/projects/{project_id}/eval-datasets/{dataset_id}/cases/{case_id}", status_code=204)
async def delete_evaluation_case(
    project_id: str,
    dataset_id: str,
    case_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    cid = _to_uuid(case_id)
    await _verify_project_owner(uid, user.id, db)
    await _get_evaluation_case(db, uid, did, cid)
    await db.execute(delete(EvaluationCase).where(EvaluationCase.id == cid, EvaluationCase.dataset_id == did))
    await db.commit()
    return None


@router.get("/projects/{project_id}/eval-datasets/{dataset_id}/runs", response_model=list[EvaluationRunResponse])
async def list_evaluation_runs(
    project_id: str,
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    await _verify_project_owner(uid, user.id, db)
    await _get_evaluation_dataset(db, uid, did)
    result = await db.execute(
        select(EvaluationRun)
        .where(EvaluationRun.dataset_id == did, EvaluationRun.project_id == uid)
        .order_by(EvaluationRun.created_at.desc())
    )
    return [_evaluation_run_response(run) for run in result.scalars().all()]


@router.get("/projects/{project_id}/eval-datasets/{dataset_id}/runs/{run_id}", response_model=EvaluationRunResponse)
async def get_evaluation_run(
    project_id: str,
    dataset_id: str,
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    rid = _to_uuid(run_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(EvaluationRun).where(EvaluationRun.id == rid, EvaluationRun.dataset_id == did, EvaluationRun.project_id == uid)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    results = await db.execute(
        select(EvaluationResult).where(EvaluationResult.run_id == rid).order_by(EvaluationResult.created_at.asc())
    )
    return _evaluation_run_response(run, results.scalars().all())


@router.post("/projects/{project_id}/eval-datasets/{dataset_id}/runs", response_model=EvaluationRunResponse)
async def run_evaluation_dataset(
    project_id: str,
    dataset_id: str,
    req: EvaluationRunCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    did = _to_uuid(dataset_id)
    project = await _verify_project_owner(uid, user.id, db)
    dataset = await _get_evaluation_dataset(db, uid, did)

    query = select(EvaluationCase).where(
        EvaluationCase.dataset_id == did,
        EvaluationCase.project_id == uid,
        EvaluationCase.status == "active",
    )
    if req.case_ids:
        query = query.where(EvaluationCase.id.in_(req.case_ids))
    query = query.order_by(EvaluationCase.created_at.asc())
    case_result = await db.execute(query)
    cases = case_result.scalars().all()
    if not cases:
        raise HTTPException(status_code=400, detail="评测集没有可运行的样本")

    llm_config = await get_user_llm_config(user.id, db)
    provider = get_llm_provider(llm_config)
    run_name = (req.name or f"{dataset.name} 运行 {datetime.now().strftime('%m-%d %H:%M')}").strip()
    run = EvaluationRun(
        dataset_id=did,
        project_id=uid,
        name=run_name[:200],
        generation_mode=req.generation_mode,
        status="running",
        model_provider=(llm_config or {}).get("provider", settings.LLM_PROVIDER),
        model_id=(llm_config or {}).get("model", settings.LLM_MODEL),
        total_cases=len(cases),
        completed_cases=0,
        failed_cases=0,
    )
    db.add(run)
    await db.flush()

    context = activate_langfuse_context(
        name="evaluation_run",
        user_id=user.id,
        session_id=str(project.id),
        tags=["evaluation", project.mode, req.generation_mode],
        metadata={"project_id": str(project.id), "dataset_id": str(dataset.id), "action": "evaluation_run"},
    )
    created_results: list[EvaluationResult] = []
    scores: list[float] = []
    try:
        for case in cases:
            try:
                evaluated = await run_evaluation_case(
                    provider=provider,
                    project=project,
                    case=case,
                    generation_mode=req.generation_mode,
                )
                score = evaluated.get("score")
                if isinstance(score, (int, float)):
                    scores.append(float(score))
                result = EvaluationResult(
                    run_id=run.id,
                    case_id=case.id,
                    project_id=uid,
                    generated_output=evaluated.get("generated_output"),
                    scores=evaluated.get("scores"),
                    score=score,
                    passed=evaluated.get("passed"),
                    feedback=evaluated.get("feedback"),
                    error=None,
                    latency_ms=evaluated.get("latency_ms"),
                    langfuse_trace_id=current_langfuse_trace_id(),
                )
                run.completed_cases += 1
            except Exception as exc:
                result = EvaluationResult(
                    run_id=run.id,
                    case_id=case.id,
                    project_id=uid,
                    generated_output=None,
                    scores=None,
                    score=None,
                    passed=False,
                    feedback=None,
                    error=str(exc),
                    latency_ms=None,
                    langfuse_trace_id=current_langfuse_trace_id(),
                )
                run.failed_cases += 1
            db.add(result)
            created_results.append(result)

        run.average_score = round(sum(scores) / len(scores), 2) if scores else None
        run.status = "completed" if run.failed_cases == 0 else ("partial" if run.completed_cases > 0 else "failed")
        run.summary = f"完成 {run.completed_cases}/{run.total_cases} 个样本，失败 {run.failed_cases} 个。"
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        finish_langfuse_context(context)

    await db.refresh(run)
    results = await db.execute(
        select(EvaluationResult).where(EvaluationResult.run_id == run.id).order_by(EvaluationResult.created_at.asc())
    )
    return _evaluation_run_response(run, results.scalars().all())


# ==================== 章节 ====================

@router.get("/projects/{project_id}/chapters", response_model=list[ChapterResponse])
async def list_chapters(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == uid)
        .order_by(Chapter.sequence_number.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/chapters", response_model=ChapterResponse)
async def create_chapter(
    project_id: str,
    req: ChapterCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    # 同项目内 sequence_number 不允许重复
    dup_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == req.sequence_number)
    )
    if dup_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="章节序号已存在")

    chapter = Chapter(
        project_id=uid,
        title=req.title,
        outline=req.outline,
        sequence_number=req.sequence_number,
    )
    db.add(chapter)
    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.get("/projects/{project_id}/chapters/{sequence_number}", response_model=ChapterResponse)
async def get_chapter(
    project_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    return chapter


@router.patch("/projects/{project_id}/chapters/{sequence_number}", response_model=ChapterResponse)
async def update_chapter(
    project_id: str,
    sequence_number: int,
    req: ChapterUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    if req.title is not None:
        chapter.title = req.title
    if "outline" in req.model_fields_set:
        chapter.outline = req.outline
    if req.status in {"final", "approved"}:
        # 即使前端沿用 PATCH 保存，也必须创建定稿快照，不能只改状态。
        try:
            await finalize_chapter_content(
                db,
                chapter,
                req.content if "content" in req.model_fields_set else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        content_changed = "content" in req.model_fields_set
        if content_changed:
            was_final = chapter.status in {"final", "approved"}
            await save_chapter_content(db, chapter, req.content or "", source="manual")
            # 已定稿章节再次编辑后，必须重新人工确认；不能让新的未确认文本
            # 继续作为下一章的可信上文。
            final_snapshot = await db.get(ChapterVersion, chapter.final_version_id) if chapter.final_version_id else None
            if was_final and (final_snapshot is None or chapter.content != final_snapshot.content):
                chapter.status = "draft"
                chapter.final_version_id = None
        if req.status is not None:
            chapter.status = req.status
            if req.status not in {"final", "approved"}:
                chapter.final_version_id = None

    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.post("/projects/{project_id}/chapters/{sequence_number}/finalize", response_model=ChapterResponse)
async def finalize_chapter(
    project_id: str,
    sequence_number: int,
    req: ChapterFinalizeRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """在最后人工审核中确认章节定稿。

    定稿会创建不可变版本并记录到 ``final_version_id``。后续章节生成只读取该快照，
    因此编辑器里的候选/草稿不会污染创作上下文。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    try:
        await finalize_chapter_content(db, chapter, req.content)
        await db.commit()
        await db.refresh(chapter)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        await db.rollback()
        logger.exception("章节定稿失败 project=%s chapter=%s", project_id, sequence_number)
        raise HTTPException(status_code=500, detail="章节定稿失败，请重试")
    return chapter


@router.get("/projects/{project_id}/chapters/{sequence_number}/review-notes", response_model=list[ChapterReviewNoteResponse])
async def list_chapter_review_notes(
    project_id: str,
    sequence_number: int,
    resolved: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    chapter = await _get_project_chapter(db, uid, sequence_number)

    stmt = select(ChapterReviewNote).where(
        ChapterReviewNote.project_id == uid,
        ChapterReviewNote.chapter_id == chapter.id,
    )
    if resolved is not None:
        stmt = stmt.where(ChapterReviewNote.resolved == resolved)
    result = await db.execute(
        stmt.order_by(ChapterReviewNote.resolved.asc(), ChapterReviewNote.created_at.desc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/chapters/{sequence_number}/review-notes", response_model=ChapterReviewNoteResponse)
async def create_chapter_review_note(
    project_id: str,
    sequence_number: int,
    req: ChapterReviewNoteCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    chapter = await _get_project_chapter(db, uid, sequence_number)

    note = ChapterReviewNote(
        project_id=uid,
        chapter_id=chapter.id,
        chapter_sequence_number=chapter.sequence_number,
        source_type=req.source_type,
        severity=req.severity,
        content=req.content,
        resolved=req.resolved,
        metadata_=req.metadata_,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.patch("/projects/{project_id}/chapters/{sequence_number}/review-notes/{note_id}", response_model=ChapterReviewNoteResponse)
async def update_chapter_review_note(
    project_id: str,
    sequence_number: int,
    note_id: str,
    req: ChapterReviewNoteUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    nid = _to_uuid(note_id)
    await _verify_project_owner(uid, user.id, db)
    chapter = await _get_project_chapter(db, uid, sequence_number)
    note = await _get_chapter_review_note(db, uid, chapter.id, nid)

    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(note, field, value)
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/projects/{project_id}/chapters/{sequence_number}/review-notes/{note_id}", status_code=204)
async def delete_chapter_review_note(
    project_id: str,
    sequence_number: int,
    note_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    nid = _to_uuid(note_id)
    await _verify_project_owner(uid, user.id, db)
    chapter = await _get_project_chapter(db, uid, sequence_number)
    note = await _get_chapter_review_note(db, uid, chapter.id, nid)
    await db.delete(note)
    await db.commit()
    return None


@router.get("/projects/{project_id}/chapters/{sequence_number}/context")
async def get_chapter_context(
    project_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """返回当前章节的资料统计和上下文概要。

    供章节助手面板显示“已加载：角色 X · 事件 Y · 暗线 Z”。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.chapter_context import build_chapter_context, context_to_stats

    ctx = await build_chapter_context(db, uid, sequence_number)
    return context_to_stats(ctx)


def _parse_direction_options(raw: str) -> list[dict]:
    """Parse LLM direction output into structured options."""
    import re
    # 尝试直接 JSON 解析
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            result: list[dict] = []
            for i, d in enumerate(parsed):
                if isinstance(d, str):
                    # 字符串数组（mock provider）：自动转为对象
                    result.append({"id": chr(65 + i), "title": d, "description": "", "risk": ""})
                elif isinstance(d, dict):
                    result.append({"id": d.get("id", chr(65 + i)), "title": d.get("title", ""), "description": d.get("description", ""), "risk": d.get("risk", "")})
            if result:
                return result
    except (json.JSONDecodeError, Exception):
        pass
    # 回退：按字母编号拆分
    options: list[dict] = []
    for m in re.finditer(r'(?:^|\n)\s*([A-C])\s*[.、)]?\s*(.+?)(?=\n\s*[A-C]\s*[.、)]?\s|\Z)', raw, re.S):
        letter = m.group(1)
        body = m.group(2).strip()
        lines = body.split('\n')
        title = lines[0].strip() if lines else ""
        desc = lines[1].strip() if len(lines) > 1 else ""
        risk = ""
        for line in lines[1:]:
            if '风险' in line or '注意' in line or '代价' in line:
                risk = line.strip()
                break
        options.append({"id": letter, "title": title, "description": desc, "risk": risk})
    return options[:3]


@router.post("/projects/{project_id}/chapters/{sequence_number}/directions")
async def get_chapter_directions(
    project_id: str,
    sequence_number: int,
    req: Request,
    selected_outline_ids: list[str] | None = None,
    selected_character_ids: list[str] | None = None,
    selected_world_entry_ids: list[str] | None = None,
    selected_hidden_thread_ids: list[str] | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """返回当前章节的剧情走向选项。

    调用 LLM 基于章节上下文生成 2-4 个走向，供前端 DirectionPicker 展示。
    此 API 为独立调用，不进入 LangGraph 工作流。
    可选传入用户选中的素材 ID 以影响走向建议。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    project_result = await db.execute(select(Project).where(Project.id == uid))
    project = project_result.scalar_one()
    if project.mode != "novel":
        raise HTTPException(status_code=400, detail="走向选择仅支持小说项目")

    from services.chapter_context import build_chapter_context, format_chapter_context_for_prompt

    ctx = await build_chapter_context(
        db, uid, sequence_number, intent="generate",
        selected_outline_ids=selected_outline_ids,
        selected_character_ids=selected_character_ids,
        selected_world_entry_ids=selected_world_entry_ids,
        selected_hidden_thread_ids=selected_hidden_thread_ids,
    )
    formatted = format_chapter_context_for_prompt(ctx)

    llm_config_dict = await get_user_llm_config(user.id, db)
    provider = get_llm_provider(llm_config_dict)

    prompt = (
        "你是一位小说剧情策划。基于以下章节资料，为本章提出 3 个不同的剧情走向选择。\n\n"
        "要求：\n"
        "1. 每个走向给出 id（A/B/C）、title（简短标题）、description（1-2 句话描述）、risk（潜在风险或需要注意的点）。\n"
        "2. 三个走向应有明显差异（不同风格、不同节奏、不同聚焦角色）。\n"
        "3. 只输出 JSON 数组，不要任何其他内容。\n\n"
        f"## 章节资料\n{formatted or '(暂无资料)'}"
    )
    try:
        result = await provider.generate(
            "你是一位经验丰富的小说剧情策划。输出必须是严格 JSON 数组。",
            prompt,
            temperature=0.7,
            max_tokens=1200,
        )
        options = _parse_direction_options(result)
    except Exception:
        logger.exception("走向生成失败")
        options = []

    return {"options": options}


@router.post("/projects/{project_id}/chapters/{sequence_number}/extract-structure", response_model=ChapterStructureExtractResponse)
async def extract_chapter_structure(
    project_id: str,
    sequence_number: int,
    req: ChapterStructureExtractRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    project = await _verify_project_owner(uid, user.id, db)
    if project.mode != "novel":
        raise HTTPException(status_code=400, detail="结构提炼仅支持小说项目")

    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    if not (chapter.content or "").strip() and req.extraction is None:
        raise HTTPException(status_code=400, detail="章节正文为空，无法提炼")

    extraction = filter_structure_targets(req.extraction, req.targets) if req.extraction is not None else None
    if extraction is None:
        extra_context = None
        if req.include_existing_context:
            extra_context = await _build_structure_context(uid, db)
        try:
            provider = get_llm_provider(await get_user_llm_config(user.id, db))
            extraction = await extract_structure_with_provider(
                provider,
                chapter=chapter,
                project_title=project.title,
                extra_context=extra_context,
            )
            extraction = filter_structure_targets(extraction, req.targets)
        except LLMConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("章节结构提炼失败")
            raise HTTPException(status_code=502, detail="模型服务暂时不可用，请检查模型配置或稍后重试") from exc

    if _structure_extraction_needs_fallback(extraction, req.targets):
        char_result = await db.execute(
            select(Character).where(Character.project_id == uid).order_by(Character.created_at.asc()).limit(80)
        )
        fallback = build_fallback_structure_from_chapter(
            chapter,
            existing_characters=char_result.scalars().all(),
            targets=req.targets,
        )
        extraction = _merge_structure_fallback(extraction, fallback, req.targets)

    preview = build_structure_preview(extraction)
    applied = None
    if req.mode == "apply":
        applied_result = await apply_structure_extraction(
            db,
            uid,
            extraction,
            background_tasks=background_tasks,
            commit=True,
        )
        applied = {"counts": applied_result["counts"]}

    return {
        "extraction": extraction,
        "preview": preview,
        "applied": applied,
    }


def _structure_extraction_is_empty(extraction: dict) -> bool:
    return not any(bool(value) for value in (extraction or {}).values())


def _structure_extraction_needs_fallback(extraction: dict, targets: list[str]) -> bool:
    fallback_targets = {"outlines", "characters", "character_events"}
    requested = set(targets or [])
    if not requested:
        requested = fallback_targets
    return any(
        target in requested and not (extraction or {}).get(target)
        for target in fallback_targets
    )


def _merge_structure_fallback(extraction: dict, fallback: dict, targets: list[str]) -> dict:
    requested = set(targets or [])
    merged = dict(extraction or {})
    for key, value in (fallback or {}).items():
        if requested and key not in requested:
            continue
        if key not in {"outlines", "characters", "character_events"}:
            continue
        if not merged.get(key) and value:
            merged[key] = value
    return merged


@router.delete("/projects/{project_id}/chapters/{sequence_number}")
async def delete_chapter(
    project_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    await db.execute(delete(ChapterReviewNote).where(ChapterReviewNote.chapter_id == chapter.id))
    await db.delete(chapter)
    await db.commit()
    return {"ok": True}


# ==================== 世界观条目 ====================

@router.get("/projects/{project_id}/world-entries", response_model=list[WorldEntryResponse])
async def list_world_entries(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(WorldEntry)
        .where(WorldEntry.project_id == uid)
        .order_by(WorldEntry.category.asc(), WorldEntry.created_at.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/world-entries", response_model=WorldEntryResponse)
async def create_world_entry(
    project_id: str,
    req: WorldEntryCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    entry = WorldEntry(
        project_id=uid,
        title=req.title,
        category=req.category,
        scope_type=req.scope_type,
        content=req.content,
        rules=req.rules,
        confidence=req.confidence,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    # 后台生成 embedding，不阻塞响应
    embed_text = f"{entry.title} {entry.content}"
    background_tasks.add_task(_update_embedding_bg, WorldEntry, entry.id, embed_text)

    return entry


@router.patch("/projects/{project_id}/world-entries/{entry_id}", response_model=WorldEntryResponse)
async def update_world_entry(
    project_id: str,
    entry_id: str,
    req: WorldEntryUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    eid = _to_uuid(entry_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(WorldEntry).where(WorldEntry.id == eid, WorldEntry.project_id == uid)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="世界观条目不存在")

    # 检查是否需要重新生成 embedding（title 或 content 被更新）
    needs_reembed = req.title is not None or req.content is not None

    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(entry, field, value)

    await db.commit()
    await db.refresh(entry)

    # 如果 title 或 content 变更，后台重新生成 embedding
    if needs_reembed:
        embed_text = f"{entry.title} {entry.content}"
        background_tasks.add_task(_update_embedding_bg, WorldEntry, entry.id, embed_text)

    return entry


@router.delete("/projects/{project_id}/world-entries/{entry_id}", status_code=204)
async def delete_world_entry(
    project_id: str,
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    eid = _to_uuid(entry_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(WorldEntry).where(WorldEntry.id == eid, WorldEntry.project_id == uid)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="世界观条目不存在")

    await db.delete(entry)
    await db.commit()
    return None


# ==================== 角色 ====================

@router.get("/projects/{project_id}/characters", response_model=list[CharacterResponse])
async def list_characters(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Character)
        .where(Character.project_id == uid)
        .order_by(Character.created_at.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/characters", response_model=CharacterResponse)
async def create_character(
    project_id: str,
    req: CharacterCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    character = Character(
        project_id=uid,
        name=req.name,
        role_type=req.role_type,
        scope_type=req.scope_type,
        profile=req.profile,
        faction=req.faction,
        metadata_=req.metadata_,
    )
    db.add(character)
    await db.commit()
    await db.refresh(character)

    # 后台生成 embedding，不阻塞响应
    embed_text = f"{character.name} {character.profile or ''}"
    background_tasks.add_task(_update_embedding_bg, Character, character.id, embed_text)

    return character


@router.patch("/projects/{project_id}/characters/{character_id}", response_model=CharacterResponse)
async def update_character(
    project_id: str,
    character_id: str,
    req: CharacterUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Character).where(Character.id == cid, Character.project_id == uid)
    )
    character = result.scalar_one_or_none()
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")

    # 检查是否需要重新生成 embedding（name 或 profile 被更新）
    needs_reembed = req.name is not None or req.profile is not None

    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(character, field, value)

    await db.commit()
    await db.refresh(character)

    # 如果 name 或 profile 变更，后台重新生成 embedding
    if needs_reembed:
        embed_text = f"{character.name} {character.profile or ''}"
        background_tasks.add_task(_update_embedding_bg, Character, character.id, embed_text)

    return character


@router.post("/projects/{project_id}/characters/{character_id}/merge", response_model=CharacterResponse)
async def merge_character(
    project_id: str,
    character_id: str,
    req: CharacterMergeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    source_id = _to_uuid(character_id)
    target_id = req.target_character_id
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="不能合并到同一个角色")

    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Character).where(Character.project_id == uid, Character.id.in_([source_id, target_id]))
    )
    characters = {item.id: item for item in result.scalars().all()}
    source = characters.get(source_id)
    target = characters.get(target_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="角色不存在")

    if not target.profile and source.profile:
        target.profile = source.profile
    elif source.profile and source.profile not in (target.profile or ""):
        target.profile = f"{target.profile}\n\n合并自「{source.name}」：{source.profile}" if target.profile else source.profile
    if not target.faction and source.faction:
        target.faction = source.faction

    target.metadata_ = _merge_character_metadata(target.metadata_, source.metadata_, source.name)
    target.appearance_count = max(target.appearance_count or 0, source.appearance_count or 0)

    await _merge_character_events(db, uid, source_id, target_id)
    await _merge_character_relations(db, uid, source_id, target_id)
    await db.delete(source)
    await db.flush()

    event_count_result = await db.execute(
        select(func.count()).select_from(CharacterEvent).where(
            CharacterEvent.project_id == uid,
            CharacterEvent.character_id == target_id,
            CharacterEvent.appearance_type != "absent",
        )
    )
    target.appearance_count = max(target.appearance_count or 0, int(event_count_result.scalar_one() or 0))

    await db.commit()
    await db.refresh(target)

    embed_text = f"{target.name} {target.profile or ''}"
    background_tasks.add_task(_update_embedding_bg, Character, target.id, embed_text)
    return target


def _merge_character_metadata(target_meta: dict | None, source_meta: dict | None, source_name: str) -> dict:
    merged = dict(target_meta or {})
    for key, value in (source_meta or {}).items():
        merged.setdefault(key, value)
    aliases = list(merged.get("aliases") or [])
    if source_name not in aliases:
        aliases.append(source_name)
    merged["aliases"] = aliases
    return merged


async def _merge_character_events(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    source_result = await db.execute(
        select(CharacterEvent).where(CharacterEvent.project_id == project_id, CharacterEvent.character_id == source_id)
    )
    target_result = await db.execute(
        select(CharacterEvent).where(CharacterEvent.project_id == project_id, CharacterEvent.character_id == target_id)
    )
    target_by_chapter = {event.chapter_sequence_number: event for event in target_result.scalars().all()}
    for source_event in source_result.scalars().all():
        target_event = target_by_chapter.get(source_event.chapter_sequence_number)
        if target_event is None:
            source_event.character_id = target_id
            target_by_chapter[source_event.chapter_sequence_number] = source_event
            continue

        target_event.appearance_type = _stronger_appearance_type(target_event.appearance_type, source_event.appearance_type)
        target_event.appeared = target_event.appeared or source_event.appeared
        target_event.event_summary = _merge_optional_text(target_event.event_summary, source_event.event_summary)
        target_event.actions = _merge_string_lists(target_event.actions, source_event.actions)
        target_event.state_change = _merge_optional_text(target_event.state_change, source_event.state_change)
        target_event.location = target_event.location or source_event.location
        target_event.emotion = target_event.emotion or source_event.emotion
        target_event.importance = max(target_event.importance or 0, source_event.importance or 0)
        await db.delete(source_event)


async def _merge_character_relations(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    relation_result = await db.execute(
        select(CharacterRelation).where(
            CharacterRelation.project_id == project_id,
            (CharacterRelation.source_character_id == source_id) | (CharacterRelation.target_character_id == source_id),
        )
    )
    for relation in relation_result.scalars().all():
        if relation.source_character_id == source_id:
            relation.source_character_id = target_id
        if relation.target_character_id == source_id:
            relation.target_character_id = target_id
        if relation.source_character_id == relation.target_character_id:
            await db.delete(relation)


def _stronger_appearance_type(a: str | None, b: str | None) -> str:
    order = {"absent": 0, "mentioned": 1, "appeared": 2}
    return a if order.get(a or "absent", 0) >= order.get(b or "absent", 0) else (b or "absent")


def _merge_optional_text(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b or b in a:
        return a
    return f"{a}\n{b}"


def _merge_string_lists(a: list | None, b: list | None) -> list | None:
    merged: list[str] = []
    for value in list(a or []) + list(b or []):
        text = str(value).strip()
        if text and text not in merged:
            merged.append(text)
    return merged or None


@router.delete("/projects/{project_id}/characters/{character_id}", status_code=204)
async def delete_character(
    project_id: str,
    character_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Character).where(Character.id == cid, Character.project_id == uid)
    )
    character = result.scalar_one_or_none()
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")

    await db.delete(character)
    await db.commit()
    return None


# ==================== 角色章节轨迹 ====================

@router.get("/projects/{project_id}/character-events", response_model=list[CharacterEventResponse])
async def list_character_events(
    project_id: str,
    character_id: str | None = Query(default=None),
    sequence_number: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    stmt = select(CharacterEvent).where(CharacterEvent.project_id == uid)
    if character_id:
        stmt = stmt.where(CharacterEvent.character_id == _to_uuid(character_id))
    if sequence_number is not None:
        stmt = stmt.where(CharacterEvent.chapter_sequence_number == sequence_number)
    result = await db.execute(stmt.order_by(CharacterEvent.chapter_sequence_number.asc(), CharacterEvent.created_at.asc()))
    return result.scalars().all()


@router.get("/projects/{project_id}/characters/{character_id}/chapter-events", response_model=list[CharacterEventResponse])
async def list_character_chapter_events(
    project_id: str,
    character_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)
    await _get_project_character(uid, cid, db)
    result = await db.execute(
        select(CharacterEvent)
        .where(CharacterEvent.project_id == uid, CharacterEvent.character_id == cid)
        .order_by(CharacterEvent.chapter_sequence_number.asc())
    )
    return result.scalars().all()


@router.put("/projects/{project_id}/characters/{character_id}/chapter-events/{sequence_number}", response_model=CharacterEventResponse)
async def upsert_character_chapter_event(
    project_id: str,
    character_id: str,
    sequence_number: int,
    req: CharacterEventUpsert,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)
    character = await _get_project_character(uid, cid, db)

    result = await db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == uid,
            CharacterEvent.character_id == cid,
            CharacterEvent.chapter_sequence_number == sequence_number,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        event = CharacterEvent(project_id=uid, character_id=cid, chapter_sequence_number=sequence_number)
        db.add(event)

    event.appearance_type = req.appearance_type
    event.appeared = req.appearance_type == "appeared"
    event.event_summary = req.event_summary
    event.actions = req.actions
    event.state_change = req.state_change
    event.location = req.location
    event.emotion = req.emotion
    event.importance = req.importance

    await db.flush()
    await _refresh_character_appearance_count(db, character)
    await db.commit()
    await db.refresh(event)
    return event


@router.delete("/projects/{project_id}/characters/{character_id}/chapter-events/{sequence_number}", status_code=204)
async def delete_character_chapter_event(
    project_id: str,
    character_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)
    character = await _get_project_character(uid, cid, db)
    result = await db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == uid,
            CharacterEvent.character_id == cid,
            CharacterEvent.chapter_sequence_number == sequence_number,
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="角色章节事件不存在")

    await db.delete(event)
    await db.flush()
    await _refresh_character_appearance_count(db, character)
    await db.commit()
    return None


async def _get_project_character(project_id: uuid.UUID, character_id: uuid.UUID, db: AsyncSession) -> Character:
    result = await db.execute(
        select(Character).where(Character.id == character_id, Character.project_id == project_id)
    )
    character = result.scalar_one_or_none()
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")
    return character


async def _refresh_character_appearance_count(db: AsyncSession, character: Character) -> None:
    result = await db.execute(
        select(func.count(CharacterEvent.id)).where(
            CharacterEvent.project_id == character.project_id,
            CharacterEvent.character_id == character.id,
            CharacterEvent.appearance_type.in_(["appeared", "mentioned"]),
        )
    )
    character.appearance_count = int(result.scalar_one() or 0)


# ==================== 角色弧线 (K-5) ====================

@router.get("/projects/{project_id}/characters/{character_id}/arc", response_model=CharacterArcResponse)
async def get_character_arc(
    project_id: str,
    character_id: str,
    to_chapter: int | None = Query(default=None, description="只返回到第N章为止的数据"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """获取角色弧线聚合视图。

    聚合来源：
    - CharacterEvent（手动录入 + AI 抽取确认）
    - WritingMemoryStaging（已确认的 CHARACTER/EVENT 类型）
    """
    from schemas.api import CharacterArcItem
    from models.harness_enums import MemoryStagingStatus

    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)

    character = await _get_project_character(uid, cid, db)

    items: list[CharacterArcItem] = []

    # 1. CharacterEvent
    event_result = await db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == uid,
            CharacterEvent.character_id == cid,
            CharacterEvent.appeared == True,  # noqa: E712
        ).order_by(CharacterEvent.chapter_sequence_number.asc())
    )
    for evt in event_result.scalars().all():
        if to_chapter and evt.chapter_sequence_number and evt.chapter_sequence_number > to_chapter:
            continue
        items.append(CharacterArcItem(
            chapter_sequence_number=evt.chapter_sequence_number,
            source_type="CharacterEvent",
            title=evt.event_summary or f"第{evt.chapter_sequence_number}章出场",
            summary=evt.event_summary or "",
            state_change=evt.state_change,
            emotion=evt.emotion,
            importance=evt.importance or 3,
            confidence="confirmed",
        ))

    # 2. WritingMemoryStaging
    staging_result = await db.execute(
        select(WritingMemoryStaging).where(
            WritingMemoryStaging.project_id == uid,
            WritingMemoryStaging.memory_type.in_(["CHARACTER", "EVENT"]),
            WritingMemoryStaging.status == MemoryStagingStatus.CONFIRMED.value,
        ).order_by(WritingMemoryStaging.chapter_sequence_number.asc().nulls_last())
    )
    for sm in staging_result.scalars().all():
        if to_chapter and sm.chapter_sequence_number and sm.chapter_sequence_number > to_chapter:
            continue
        payload = sm.payload or {}
        char_name = payload.get("character_name") or payload.get("name") or ""
        if char_name.lower() != character.name.lower():
            continue
        items.append(CharacterArcItem(
            chapter_sequence_number=sm.chapter_sequence_number,
            source_type="WritingMemory",
            title=sm.title,
            summary=payload.get("description") or sm.title,
            state_change=payload.get("state_change"),
            importance=3,
            confidence="ai_extracted",
        ))

    items.sort(key=lambda x: x.chapter_sequence_number or 0)

    chapters = [i.chapter_sequence_number for i in items if i.chapter_sequence_number]
    chapter_range = f"第{min(chapters)}-{max(chapters)}章" if chapters else ""

    return CharacterArcResponse(
        character_id=cid,
        character_name=character.name,
        role_type=character.role_type or "supporting",
        items=items,
        chapter_range=chapter_range,
    )


# ==================== 角色关系 ====================

@router.get("/projects/{project_id}/character-relations", response_model=list[CharacterRelationResponse])
async def list_character_relations(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(CharacterRelation)
        .where(CharacterRelation.project_id == uid)
        .order_by(CharacterRelation.created_at.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/character-relations", response_model=CharacterRelationResponse)
async def create_character_relation(
    project_id: str,
    req: CharacterRelationCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    # 校验两个角色都属于该项目
    source_id = _to_uuid(str(req.source_character_id))
    target_id = _to_uuid(str(req.target_character_id))

    source_result = await db.execute(
        select(Character).where(Character.id == source_id, Character.project_id == uid)
    )
    if not source_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="源角色不存在或不属于该项目")

    target_result = await db.execute(
        select(Character).where(Character.id == target_id, Character.project_id == uid)
    )
    if not target_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="目标角色不存在或不属于该项目")

    relation = CharacterRelation(
        project_id=uid,
        source_character_id=source_id,
        target_character_id=target_id,
        description=req.description,
    )
    db.add(relation)
    await db.commit()
    await db.refresh(relation)
    return relation


@router.patch("/projects/{project_id}/character-relations/{relation_id}", response_model=CharacterRelationResponse)
async def update_character_relation(
    project_id: str,
    relation_id: str,
    req: CharacterRelationUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    rid = _to_uuid(relation_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(CharacterRelation).where(CharacterRelation.id == rid, CharacterRelation.project_id == uid)
    )
    relation = result.scalar_one_or_none()
    if not relation:
        raise HTTPException(status_code=404, detail="角色关系不存在")

    if req.description is not None:
        relation.description = req.description

    await db.commit()
    await db.refresh(relation)
    return relation


@router.delete("/projects/{project_id}/character-relations/{relation_id}", status_code=204)
async def delete_character_relation(
    project_id: str,
    relation_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    rid = _to_uuid(relation_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(CharacterRelation).where(CharacterRelation.id == rid, CharacterRelation.project_id == uid)
    )
    relation = result.scalar_one_or_none()
    if not relation:
        raise HTTPException(status_code=404, detail="角色关系不存在")

    await db.delete(relation)
    await db.commit()
    return None


# ==================== 大纲 ====================

@router.get("/projects/{project_id}/outlines", response_model=list[OutlineResponse])
async def list_outlines(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Outline)
        .where(Outline.project_id == uid)
        .order_by(Outline.sequence_number.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/outlines", response_model=OutlineResponse)
async def create_outline(
    project_id: str,
    req: OutlineCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    # 同项目内 sequence_number 不允许重复
    dup_result = await db.execute(
        select(Outline).where(Outline.project_id == uid, Outline.sequence_number == req.sequence_number)
    )
    if dup_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="大纲序号已存在")

    # 校验 story_arc_id 是否存在且属于当前项目
    story_arc_id = None
    if req.story_arc_id:
        story_arc_id = _to_uuid(req.story_arc_id)
        arc_check = await db.execute(
            select(StoryArc).where(
                StoryArc.id == story_arc_id,
                StoryArc.project_id == uid
            )
        )
        if not arc_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="story_arc_id 不存在或不属于该项目")

    outline = Outline(
        project_id=uid,
        sequence_number=req.sequence_number,
        title=req.title,
        summary=req.summary,
        turning_point=req.turning_point,
        story_arc_id=story_arc_id,
        arc_position=req.arc_position,
        pacing=req.pacing,
        tension_level=req.tension_level,
        target_scene_count=req.target_scene_count,
    )
    db.add(outline)
    await db.commit()
    await db.refresh(outline)
    return outline


@router.patch("/projects/{project_id}/outlines/{outline_id}", response_model=OutlineResponse)
async def update_outline(
    project_id: str,
    outline_id: str,
    req: OutlineUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    oid = _to_uuid(outline_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Outline).where(Outline.id == oid, Outline.project_id == uid)
    )
    outline = result.scalar_one_or_none()
    if not outline:
        raise HTTPException(status_code=404, detail="大纲条目不存在")

    update_data = req.model_dump(exclude_unset=True)
    # story_arc_id 需要 UUID 转换并校验
    if "story_arc_id" in update_data:
        if update_data["story_arc_id"]:
            story_arc_id = _to_uuid(update_data["story_arc_id"])
            # 校验 story_arc_id 是否存在且属于当前项目
            arc_check = await db.execute(
                select(StoryArc).where(
                    StoryArc.id == story_arc_id,
                    StoryArc.project_id == uid
                )
            )
            if not arc_check.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="story_arc_id 不存在或不属于该项目")
            update_data["story_arc_id"] = story_arc_id
        else:
            update_data["story_arc_id"] = None
    for field, value in update_data.items():
        setattr(outline, field, value)

    await db.commit()
    await db.refresh(outline)
    return outline


@router.delete("/projects/{project_id}/outlines/{outline_id}", status_code=204)
async def delete_outline(
    project_id: str,
    outline_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    oid = _to_uuid(outline_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Outline).where(Outline.id == oid, Outline.project_id == uid)
    )
    outline = result.scalar_one_or_none()
    if not outline:
        raise HTTPException(status_code=404, detail="大纲条目不存在")

    await db.delete(outline)
    await db.commit()
    return None


# ==================== 暗线 ====================

@router.get("/projects/{project_id}/hidden-threads", response_model=list[HiddenThreadResponse])
async def list_hidden_threads(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(HiddenThread)
        .where(HiddenThread.project_id == uid)
        .order_by(HiddenThread.created_at.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/hidden-threads", response_model=HiddenThreadResponse)
async def create_hidden_thread(
    project_id: str,
    req: HiddenThreadCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    hidden_thread = HiddenThread(
        project_id=uid,
        name=req.name,
        description=req.description,
        chapter_nums=req.chapter_nums,
        status=req.status,
        thread_type=req.thread_type,
        planted_chapter=req.planted_chapter,
        reveal_chapter=req.reveal_chapter,
    )
    db.add(hidden_thread)
    await db.commit()
    await db.refresh(hidden_thread)
    return hidden_thread


@router.patch("/projects/{project_id}/hidden-threads/{thread_id}", response_model=HiddenThreadResponse)
async def update_hidden_thread(
    project_id: str,
    thread_id: str,
    req: HiddenThreadUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    tid = _to_uuid(thread_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(HiddenThread).where(HiddenThread.id == tid, HiddenThread.project_id == uid)
    )
    hidden_thread = result.scalar_one_or_none()
    if not hidden_thread:
        raise HTTPException(status_code=404, detail="暗线不存在")

    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(hidden_thread, field, value)

    await db.commit()
    await db.refresh(hidden_thread)
    return hidden_thread


@router.delete("/projects/{project_id}/hidden-threads/{thread_id}", status_code=204)
async def delete_hidden_thread(
    project_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    tid = _to_uuid(thread_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(HiddenThread).where(HiddenThread.id == tid, HiddenThread.project_id == uid)
    )
    hidden_thread = result.scalar_one_or_none()
    if not hidden_thread:
        raise HTTPException(status_code=404, detail="暗线不存在")

    await db.delete(hidden_thread)
    await db.commit()
    return None


# ==================== 长线结构 (Story Arc) ====================

@router.get("/projects/{project_id}/story-arcs", response_model=list[StoryArcResponse])
async def list_story_arcs(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(StoryArc)
        .where(StoryArc.project_id == uid)
        .order_by(StoryArc.order_index.asc(), StoryArc.created_at.asc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/story-arcs", response_model=StoryArcResponse)
async def create_story_arc(
    project_id: str,
    req: StoryArcCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    parent_arc_id = None
    if req.parent_arc_id:
        parent_arc_id = _to_uuid(req.parent_arc_id)
        # 验证 parent arc 存在且属于同项目
        parent_result = await db.execute(
            select(StoryArc).where(StoryArc.id == parent_arc_id, StoryArc.project_id == uid)
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="父级长线不存在或不属于该项目")

    arc = StoryArc(
        project_id=uid,
        parent_arc_id=parent_arc_id,
        arc_type=req.arc_type,
        name=req.name,
        summary=req.summary,
        goal=req.goal,
        main_conflict=req.main_conflict,
        start_chapter=req.start_chapter,
        end_chapter=req.end_chapter,
        order_index=req.order_index,
        status=req.status,
    )
    db.add(arc)
    await db.commit()
    await db.refresh(arc)
    return arc


@router.patch("/projects/{project_id}/story-arcs/{arc_id}", response_model=StoryArcResponse)
async def update_story_arc(
    project_id: str,
    arc_id: str,
    req: StoryArcUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    aid = _to_uuid(arc_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(StoryArc).where(StoryArc.id == aid, StoryArc.project_id == uid)
    )
    arc = result.scalar_one_or_none()
    if not arc:
        raise HTTPException(status_code=404, detail="长线不存在")

    update_data = req.model_dump(exclude_unset=True)

    # parent_arc_id 需要转 UUID 并校验
    if "parent_arc_id" in update_data:
        if update_data["parent_arc_id"]:
            parent_arc_id = _to_uuid(update_data["parent_arc_id"])
            # 禁止自引用
            if str(parent_arc_id) == arc_id:
                raise HTTPException(status_code=400, detail="parent_arc_id 不能指向自身")
            # 校验父级是否存在且属于当前项目
            parent_check = await db.execute(
                select(StoryArc).where(
                    StoryArc.id == parent_arc_id,
                    StoryArc.project_id == uid
                )
            )
            if not parent_check.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="parent_arc_id 不存在或不属于该项目")
            update_data["parent_arc_id"] = parent_arc_id
        else:
            update_data["parent_arc_id"] = None

    for field, value in update_data.items():
        setattr(arc, field, value)

    await db.commit()
    await db.refresh(arc)
    return arc


@router.delete("/projects/{project_id}/story-arcs/{arc_id}", status_code=204)
async def delete_story_arc(
    project_id: str,
    arc_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    aid = _to_uuid(arc_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(StoryArc).where(StoryArc.id == aid, StoryArc.project_id == uid)
    )
    arc = result.scalar_one_or_none()
    if not arc:
        raise HTTPException(status_code=404, detail="长线不存在")

    # 检查是否有子 arc
    child_result = await db.execute(
        select(StoryArc.id).where(StoryArc.parent_arc_id == aid).limit(1)
    )
    if child_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该长线下有子级，请先删除子级")

    await db.delete(arc)
    await db.commit()
    return None


# ==================== 专家 ====================

@router.get("/projects/{project_id}/experts", response_model=list[ExpertResponse])
async def list_experts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(select(Expert).where(Expert.project_id == uid))
    return result.scalars().all()


@router.post("/projects/{project_id}/experts/sync-v2")
async def sync_v2_experts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """为已有项目补齐 Expert System v2 内置专家，并标记旧大师 deprecated。
    幂等：按 (project_id, expert_key) 去重，已存在的不重复创建。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.expert_sync import sync_v2_experts as _sync

    result = await _sync(db, uid)
    await db.commit()
    return result


@router.post("/projects/{project_id}/experts", response_model=ExpertResponse)
async def create_expert(
    project_id: str,
    req: ExpertCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    errors = validate_expert_safety(req)
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    expert = Expert(
        project_id=uid,
        name=req.name,
        description=req.description,
        role_type=req.role_type,
        skill_dir=req.skill_dir,
        system_prompt=req.system_prompt,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        workflow_position=req.workflow_position,
        context_scope=req.context_scope,
        trigger=req.trigger,
        color=req.color,
    )
    db.add(expert)
    await db.commit()
    await db.refresh(expert)
    return expert


@router.patch("/projects/{project_id}/experts/{expert_id}", response_model=ExpertResponse)
async def update_expert(
    project_id: str,
    expert_id: str,
    req: ExpertUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    eid = _to_uuid(expert_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(Expert).where(Expert.id == eid, Expert.project_id == uid)
    )
    expert = result.scalar_one_or_none()
    if not expert:
        raise HTTPException(status_code=404, detail="专家不存在")

    update_data = req.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(expert, field, value)

    await db.commit()
    await db.refresh(expert)
    return expert


# ==================== 生成（LangGraph Workflow） ====================

@router.post("/projects/{project_id}/documents/generate")
@router.post("/projects/{project_id}/chapters/generate")
async def generate_chapter(
    request: Request,
    project_id: str,
    req: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """SSE 流式生成章节/稿件候选内容。

    小说模式由 LangGraph Workflow 驱动：
    ContextLoader -> Writer -> Critic -> ConsistencyChecker -> Editor -> HumanReview。
    文章模式走文章/文案专属 prompt 与事件名，不进入小说工作流。

    SSE 事件格式与前端 parseSSEStream 兼容：
    - event: progress          — 进度通知
    - event: writer_output     — Writer 节点完成
    - event: content_output    — 文章/文案正文或反馈输出
    - event: critic_output     — Critic 节点完成
    - event: consistency_check — 一致性检查完成
    - event: content_suggestions — 文章/文案方向建议
    - event: editor_output     — Editor 节点完成
    - event: done              — 全部完成
    - event: error             — 出错

    落库策略：
    - generate 只负责流式返回候选内容/建议，不直接更新 Chapter.content，不 create_version
    - 前端通过 PATCH /chapters/{sn} 保存采纳的内容，或通过 HITL approve 路径落库
    - reject/cancel/disconnect 时绝不 commit 章节正文或版本
    - summarize 不落库（只是反馈，不改原文）
    """
    # Rate limiting：同一用户短时间内频繁发起生成会消耗大量模型资源，
    # 因此统一用 agent_limiter 做接口级限流。
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    project_result = await db.execute(select(Project).where(Project.id == uid))
    project = project_result.scalar_one()
    is_article_project = project.mode == "article"
    if "/documents/" in request.url.path and not is_article_project:
        raise HTTPException(status_code=400, detail="Document generation only available for article projects")

    # 章节/稿件定位：小说走 Chapter，文章走 Document。
    # 同一个接口同时挂在 /chapters/generate 和 /documents/generate，
    # 通过 project.mode 和 path 判断当前请求属于哪种内容对象。
    target_chapter_id = None
    target_document_id = None
    requested_content_id = req.document_id if is_article_project and req.document_id else req.chapter_id
    if requested_content_id:
        item_id = _to_uuid(requested_content_id)
        if is_article_project:
            doc_result = await db.execute(
                select(Document).where(Document.id == item_id, Document.project_id == uid)
            )
            if not doc_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="文档不存在")
            target_document_id = item_id
        else:
            ch_result = await db.execute(
                select(Chapter).where(Chapter.id == item_id, Chapter.project_id == uid)
            )
            if not ch_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="章节不存在")
            target_chapter_id = item_id
    elif req.chapter_num is not None:
        if is_article_project:
            doc_result = await db.execute(
                select(Document).where(
                    Document.project_id == uid,
                    Document.position == req.chapter_num,
                )
            )
            doc = doc_result.scalar_one_or_none()
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            target_document_id = doc.id
        else:
            ch_result = await db.execute(
                select(Chapter).where(
                    Chapter.project_id == uid,
                    Chapter.sequence_number == req.chapter_num,
                )
            )
            ch = ch_result.scalar_one_or_none()
            if not ch:
                raise HTTPException(status_code=404, detail="章节不存在")
            target_chapter_id = ch.id

    async def event_stream():
        """SSE 生成器。

        FastAPI 会边迭代边把字符串推给前端。这里不能把所有结果先存在内存再返回，
        否则用户看不到实时进度，也无法通过断开 SSE 取消生成。
        """
        langfuse_context = activate_langfuse_context(
            name="generate_content",
            user_id=str(user.id),
            session_id=str(uid),
            tags=["article" if is_article_project else "novel", req.mode],
            metadata={
                "project_id": str(uid),
                "chapter_id": str(target_chapter_id) if target_chapter_id else None,
                "document_id": str(target_document_id) if target_document_id else None,
                "mode": req.mode,
                "endpoint": request.url.path,
            },
        )
        try:
            # 同一次生成只保存一条候选 GenerationRecord。
            # 多个分支都可能调用 _save_generation_history，用 flag 防止重复落库。
            generation_record_saved = False
            ai_run = None
            workflow_thread_id: str | None = None

            async def _save_generation_history(
                content: str,
                *,
                expert_id: str | uuid.UUID | None = None,
                review_results: dict | None = None,
                skill_packs: list[dict] | None = None,
                run_id: str | None = None,
            ) -> str | None:
                """保存候选生成记录。

                注意这里只保存“候选内容”，不修改 Chapter.content / Document.content。
                用户批准或前端显式保存后，才会创建版本并更新正式正文。
                """
                nonlocal generation_record_saved
                if generation_record_saved:
                    return None
                try:
                    record = await create_generation_record(
                        db,
                        project_id=uid,
                        chapter_id=target_chapter_id,
                        document_id=target_document_id,
                        mode=req.mode,
                        expert_id=expert_id,
                        content=content,
                        req=req,
                        review_results=review_results,
                        skill_packs=skill_packs,
                        langfuse_trace_id=current_langfuse_trace_id(),
                        run_id=run_id,
                    )
                    if not record:
                        return None
                    await db.commit()
                    generation_record_saved = True
                    return str(record.id)
                except Exception:
                    await db.rollback()
                    logger.exception("AI生成历史保存失败")
                    return None

            def _generation_record_event(record_id: str | None) -> str:
                """把候选记录 ID 发给前端，前端可展示生成历史或 diff。"""
                if not record_id:
                    return ""
                return (
                    "event: generation_record\n"
                    f"data: {json.dumps({'id': record_id, 'status': 'candidate', 'langfuse_trace_id': current_langfuse_trace_id()}, ensure_ascii=False)}\n\n"
                )

            yield f"event: progress\ndata: {json.dumps({'message': '开始生成', 'mode': req.mode, 'chapter_num': req.chapter_num}, ensure_ascii=False)}\n\n"

            # 检查客户端是否已断开连接（取消时前端会 abort SSE 连接）。
            # 一旦断开，停止继续调用模型/写事件，避免用户取消后后端还在烧 token。
            async def _check_cancelled():
                if await request.is_disconnected():
                    logger.info("客户端已断开连接，取消生成")
                    await _discard_incomplete_generation_run(
                        db,
                        run_id=str(ai_run.id) if ai_run is not None else None,
                        thread_id=workflow_thread_id,
                        reason="客户端断开连接",
                    )
                    return True
                return False

            # 获取当前章节/稿件内容（enhance/continue/summarize 都需要）。
            # continue 以当前内容为续写基础；enhance 以当前内容为改写对象；
            # summarize 则只做反馈，不更新正文。
            chapter_content = ""
            if is_article_project and target_document_id:
                doc_result = await db.execute(
                    select(Document).where(Document.id == target_document_id)
                )
                document_obj = doc_result.scalar_one_or_none()
                if document_obj:
                    chapter_content = document_obj.content or ""
            elif target_chapter_id:
                ch_result = await db.execute(
                    select(Chapter).where(Chapter.id == target_chapter_id)
                )
                chapter_obj = ch_result.scalar_one_or_none()
                if chapter_obj:
                    chapter_content = chapter_obj.content or ""

            llm_config_dict = await get_user_llm_config(user.id, db)
            provider = get_llm_provider(llm_config_dict)

            # 确定章节序号（供 ChapterContextService 按章加载上下文）。
            # 如果请求只传 chapter_id/document_id，需要反查 sequence_number/position。
            chapter_num = req.chapter_num or 0
            if not chapter_num and not is_article_project and target_chapter_id:
                ch_num_result = await db.execute(
                    select(Chapter.sequence_number).where(Chapter.id == target_chapter_id)
                )
                ch_num_row = ch_num_result.scalar_one_or_none()
                if ch_num_row is not None:
                    chapter_num = ch_num_row

            # 加载创作上下文（大纲/角色/世界观/暗线/前文），供 enhance/continue/summarize 使用
            from agents.workflow import context_loader_node
            ctx_state = CreativeState(
                project_id=str(uid),
                chapter_id="" if is_article_project else (str(target_chapter_id) if target_chapter_id else ""),
                chapter_num=chapter_num,
                mode=req.mode,
                context="",
                draft="",
                original_text="",
                critiques=[],
                consistency_report={},
                edited_draft="",
                revision_count=0,
                writer_prompt="",
                critic_prompt="",
                editor_prompt="",
                consistency_prompt="",
                llm_config=llm_config_dict,
                selected_outline_ids=req.selected_outline_ids or [],
                selected_character_ids=req.selected_character_ids or [],
                selected_world_entry_ids=req.selected_world_entry_ids or [],
                selected_hidden_thread_ids=req.selected_hidden_thread_ids or [],
                include_knowledge_sources=req.include_knowledge_sources,
                include_previous_summary=req.include_previous_summary,
                target_words=req.target_words or 0,
                selected_direction=req.selected_direction or "",
                user_note=req.user_note or "",
                skill_packs=[],
            )
            try:
                ctx_result = await context_loader_node(ctx_state)
                creative_context = ctx_result.get("context", "")
                # 文章模式：将小说素材术语映射为文章语义
                if is_article_project and creative_context:
                    from services.article_context import remap_context_for_article
                    creative_context = remap_context_for_article(creative_context)
            except Exception:
                logger.exception("上下文加载失败")
                creative_context = ""

            # ==================== enhance 模式 ====================
            if req.mode == "enhance":
                # ── I-5: 补 AiRun + workflow 快照（仍走旧直接分支）──
                from services.novel_orchestrator import resolve_workflow as _resolve_wf_enhance
                _wf_enhance = _resolve_wf_enhance(project_mode=project.mode, mode="enhance")
                _enhance_run = await create_run(
                    db,
                    project_id=uid,
                    chapter_id=target_chapter_id,
                    document_id=target_document_id,
                    run_type="CHAPTER_REWRITE",
                    mode="enhance",
                    user_goal=req.user_note,
                    model_config_snapshot=llm_config_dict,
                    workflow_key=_wf_enhance.workflow_key if _wf_enhance else None,
                    workflow_version=_wf_enhance.version if _wf_enhance else None,
                    workflow_snapshot=workflow_to_snapshot(_wf_enhance),
                    expert_snapshot=[],
                )
                ai_run = _enhance_run
                await db.flush()
                _enhance_run_id = str(_enhance_run.id)
                yield f"event: run_created\ndata: {json.dumps({'run_id': _enhance_run_id, 'status': 'CREATED'}, ensure_ascii=False)}\n\n"
                await mark_running(db, _enhance_run)
                yield f"event: run_status\ndata: {json.dumps({'run_id': _enhance_run_id, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

                if not req.enhance_direction:
                    # 第一步：分析当前内容，输出 3 个编辑/润色方向。
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'editor', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    if is_article_project:
                        plan = plan_direct_skill_pack(
                            project_mode=project.mode,
                            generate_mode=req.mode,
                            action="enhance_suggest",
                        )
                        pack, summary = _planned_direct_skill_pack(
                            plan,
                            project_id=str(uid),
                            chapter_id=str(target_document_id or ""),
                            draft=chapter_content,
                            context=creative_context,
                            mode=req.mode,
                        )
                        yield _skill_pack_sse_event(summary)
                        result = await provider.generate(
                            build_expert_system_prompt(
                                "editor",
                                "你是一位专业内容编辑。请分析当前文章/文案，给出3个“改写优化”方向。"
                                "方向必须聚焦结构、标题吸引力、表达清晰度、受众说服力、平台适配、行动引导；"
                                "禁止给出小说续写、剧情转折、角色行动、世界观设定。"
                                "每个方向用一句话概括，只输出JSON字符串数组。",
                                pack,
                            ),
                            f"## 内容 brief\n{_article_brief(req)}\n\n## 当前稿件\n{chapter_content or '(空稿件)'}",
                        )
                    else:
                        base_system_prompt = (
                            "你是一位专业文学编辑。请分析用户提供的当前章节，给出3个“润色/改写”方向。"
                            "方向必须聚焦文风、语气、节奏、氛围、描写密度、人物心理、对话质感等编辑维度；"
                            "禁止给出续写、转折、新剧情、新角色登场、后续事件安排。"
                            "每个方向用一句话概括，只输出JSON字符串数组。"
                        )
                        plan = plan_direct_skill_pack(
                            project_mode=project.mode,
                            generate_mode=req.mode,
                            action="enhance_suggest",
                        )
                        pack, summary = _planned_direct_skill_pack(
                            plan,
                            project_id=str(uid),
                            chapter_id=str(target_chapter_id or ""),
                            draft=chapter_content,
                            context=creative_context,
                            mode=req.mode,
                        )
                        yield _skill_pack_sse_event(summary)
                        result = await provider.generate(
                            build_expert_system_prompt("editor", base_system_prompt, pack),
                            f"## 当前章节\n{chapter_content or '(空章节)'}",
                        )
                    directions = _parse_directions(result)[:3]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'editor', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    yield f"event: enhance_directions\ndata: {json.dumps({'directions': directions}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'message': '请选择润色方向'}, ensure_ascii=False)}\n\n"
                    return

                # 第二步：按选定方向润色
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'editor', 'step': 'running'}, ensure_ascii=False)}\n\n"
                try:
                    source_words, target_words, min_words, max_words, enhance_max_tokens = _enhance_word_budget(
                        chapter_content,
                        req.target_words,
                    )
                except ValueError as exc:
                    yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
                    return

                direct_skill_packs = []
                if is_article_project:
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="enhance_apply",
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_document_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    user_prompt = (
                        f"## 内容 brief\n{_article_brief(req)}\n\n"
                        f"## 可用上下文\n{creative_context or '无'}\n\n"
                        f"## 改写优化方向\n{req.enhance_direction}\n\n"
                        f"## 用户补充\n{req.user_note or '无'}\n\n"
                        f"## 字数控制\n原稿约{source_words}字；本次目标约{target_words}字，输出必须控制在{min_words}-{max_words}字之间。\n\n"
                        f"## 原文案/文章（只能改写这一份稿件）\n{chapter_content}\n\n"
                        "请输出“完整改写优化后的文章/文案正文”。\n"
                        "硬性要求：\n"
                        "1. 只改写当前稿件，不要在原文末尾之后继续扩写新主题。\n"
                        "2. 不使用小说章节、剧情、角色、世界观、伏笔等表达。\n"
                        "3. 可以重组结构、压缩冗余、增强说服力、优化标题感和行动引导。\n"
                        "4. 严格遵守字数控制；如果接近上限，主动压缩句子并自然收束。\n"
                        "5. 最后一句必须完整，不能半截截断。\n"
                        "6. 不要输出解释、修改说明或“改写后文本”等前缀，只输出正文。"
                    )
                    system_prompt = build_expert_system_prompt(
                        "editor",
                        _article_system_prompt("改写优化当前文章/文案"),
                        pack,
                    )
                    direct_skill_packs = [summary]
                else:
                    user_prompt = (
                        f"## 上下文\n{creative_context}\n\n"
                        f"## 润色方向\n{req.enhance_direction}\n\n"
                        f"## 用户补充\n{req.user_note or '无'}\n\n"
                        f"## 字数控制\n原文章节约{source_words}字；本次润色目标约{target_words}字，输出必须控制在{min_words}-{max_words}字之间。\n\n"
                        f"## 原文章节（只能改写这一段文本）\n{chapter_content}\n\n"
                        "请输出“完整润色后的章节正文”。\n"
                        "硬性要求：\n"
                        "1. 只改写原文章节已有内容，不能在原文结尾之后继续写。\n"
                        "2. 不新增剧情事件、不新增场景、不新增人物出场、不改变事实因果和章节结尾。\n"
                        "3. 允许调整句式、节奏、文风、氛围、描写密度、心理刻画和对话质感。\n"
                        "4. 严格遵守字数控制；如果接近上限，主动压缩句子并自然收束，不要输出半句话或未完成段落。\n"
                        "5. 最后一句必须是完整句子，必须以自然标点结束。\n"
                        "6. 不要输出解释、标题、修改说明、项目符号或“润色后文本”等前缀，只输出正文。"
                    )
                    system_prompt = (
                        "你是一位专业文学编辑，不是续写作者。你的任务是重写并润色用户提供的当前章节，"
                        "保持原剧情、原事实、原场景边界和原结尾，不得续写后续内容。"
                        "输出必须控制字数，并以完整自然的句子结束。"
                    )
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="enhance_apply",
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_chapter_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    system_prompt = build_expert_system_prompt("editor", system_prompt, pack)
                    direct_skill_packs = [summary]
                writer_content = ""
                async for chunk in provider.generate_stream(
                    system_prompt,
                    user_prompt,
                    temperature=0.35,
                    max_tokens=enhance_max_tokens,
                ):
                    if await _check_cancelled():
                        return
                    writer_content += chunk
                    output_event = "content_output" if is_article_project else "writer_output"
                    yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'editor', 'step': 'success'}, ensure_ascii=False)}\n\n"

                record_id = await _save_generation_history(writer_content, skill_packs=direct_skill_packs)
                yield _generation_record_event(record_id)
                await mark_completed(db, _enhance_run)
                await db.commit()
                yield f"event: done\ndata: {json.dumps({'message': '润色完成'}, ensure_ascii=False)}\n\n"
                return

            # ==================== continue 模式（无 expert_id） ====================
            elif req.mode == "continue" and not req.expert_id:
                if not req.turn_direction:
                    # 第一步：分析当前写作，输出 5 个方向建议
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    if is_article_project:
                        plan = plan_direct_skill_pack(
                            project_mode=project.mode,
                            generate_mode=req.mode,
                            action="continue_suggest",
                        )
                        pack, summary = _planned_direct_skill_pack(
                            plan,
                            project_id=str(uid),
                            chapter_id=str(target_document_id or ""),
                            draft=chapter_content,
                            context=creative_context,
                            mode=req.mode,
                        )
                        yield _skill_pack_sse_event(summary)
                        result = await provider.generate(
                            build_expert_system_prompt(
                                "writer",
                                "你是一位专业内容策划。请基于当前文章/文案和 brief，给出5个可执行的内容方向或标题角度。"
                                "建议必须聚焦选题、结构、卖点、受众痛点、平台表达和行动引导；"
                                "禁止小说剧情、角色、续写、转折等叙事建议。每个建议用一句话概括，只输出JSON字符串数组。",
                                pack,
                            ),
                            f"## 内容 brief\n{_article_brief(req)}\n\n## 当前稿件\n{chapter_content or '(空稿件)'}",
                        )
                    else:
                        base_system_prompt = "你是一位创意写作顾问。分析当前章节的写作进展，给出5个下一步情节发展方向的建议。每个建议用一句话概括，用JSON数组格式输出。"
                        plan = plan_direct_skill_pack(
                            project_mode=project.mode,
                            generate_mode=req.mode,
                            action="continue_suggest",
                        )
                        pack, summary = _planned_direct_skill_pack(
                            plan,
                            project_id=str(uid),
                            chapter_id=str(target_chapter_id or ""),
                            draft=chapter_content,
                            context=creative_context,
                            mode=req.mode,
                        )
                        yield _skill_pack_sse_event(summary)
                        result = await provider.generate(
                            build_expert_system_prompt("twister", base_system_prompt, pack),
                            chapter_content or "(空章节)",
                        )
                    suggestions = _parse_directions(result)[:5]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    suggestion_event = "content_suggestions" if is_article_project else "turn_suggestions"
                    yield f"event: {suggestion_event}\ndata: {json.dumps({'suggestions': suggestions}, ensure_ascii=False)}\n\n"
                    done_message = "请选择内容方向" if is_article_project else "请选择转折方向"
                    yield f"event: done\ndata: {json.dumps({'message': done_message}, ensure_ascii=False)}\n\n"
                    return

                # 第二步：按选定方向生成/续写
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                direct_skill_packs = []
                if is_article_project:
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="continue_generate",
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_document_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    user_prompt = (
                        f"## 内容 brief\n{_article_brief(req)}\n\n"
                        f"## 可用上下文\n{creative_context or '无'}\n\n"
                        f"## 选定内容方向\n{req.turn_direction}\n\n"
                        f"## 用户补充\n{req.user_note or '无'}\n\n"
                        f"## 当前稿件\n{chapter_content or '(空稿件)'}\n\n"
                        "请根据以上信息生成完整文章/文案正文。不要把内容简单接在当前稿件后面，而是围绕方向输出一版完整可用稿。"
                    )
                    system_prompt = build_expert_system_prompt(
                        "writer",
                        _article_system_prompt("生成文章/文案内容"),
                        pack,
                    )
                    direct_skill_packs = [summary]
                    done_message = "内容生成完成"
                    writer_content = ""
                    async for chunk in provider.generate_stream(system_prompt, user_prompt):
                        if await _check_cancelled():
                            return
                        writer_content += chunk
                        yield f"event: content_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    record_id = await _save_generation_history(writer_content, skill_packs=direct_skill_packs)
                    yield _generation_record_event(record_id)
                    yield f"event: done\ndata: {json.dumps({'message': done_message}, ensure_ascii=False)}\n\n"
                    return
                else:
                    # ── Novel continue 第二阶段：切 v2 LangGraph（continue_fast，无 HITL）──
                    from services.novel_orchestrator import resolve_workflow as _resolve_wf_continue
                    wf_continue = _resolve_wf_continue(project_mode="novel", mode="continue")
                    thread_id = f"{uid}:{target_chapter_id or 'no-chapter'}:{uuid.uuid4().hex[:8]}"
                    workflow_thread_id = thread_id
                    ai_run = await create_run(
                        db,
                        project_id=uid,
                        chapter_id=target_chapter_id,
                        run_type="CHAPTER_CONTINUE",
                        mode="continue",
                        user_goal=req.user_note,
                        model_config_snapshot=llm_config_dict,
                        workflow_key=wf_continue.workflow_key if wf_continue else None,
                        workflow_version=wf_continue.version if wf_continue else None,
                        workflow_snapshot=workflow_to_snapshot(wf_continue),
                        expert_snapshot=[],
                    )
                    await db.flush()
                    run_id_str = str(ai_run.id)
                    await mark_running(db, ai_run)
                    yield f"event: run_created\ndata: {json.dumps({'run_id': run_id_str, 'status': 'CREATED'}, ensure_ascii=False)}\n\n"
                    yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

                    app = get_creative_app_v2_continue()
                    config = {"configurable": {"thread_id": thread_id}}
                    initial_state: CreativeStateV2 = {
                        "project_id": str(uid),
                        "chapter_id": str(target_chapter_id or ""),
                        "chapter_num": chapter_num,
                        "mode": "continue",
                        "context": "",
                        "context_summary": {},
                        "draft": chapter_content or "",
                        "writer_draft": "",
                        "original_text": "",
                        "critiques": [],
                        "consistency_report": {},
                        "edited_draft": "",
                        "revision_count": 0,
                        "writer_prompt": "",
                        "critic_prompt": "",
                        "editor_prompt": "",
                        "consistency_prompt": "",
                        "llm_config": llm_config_dict,
                        "selected_outline_ids": req.selected_outline_ids or [],
                        "selected_character_ids": req.selected_character_ids or [],
                        "selected_world_entry_ids": req.selected_world_entry_ids or [],
                        "selected_hidden_thread_ids": req.selected_hidden_thread_ids or [],
                        "include_knowledge_sources": req.include_knowledge_sources,
                        "include_previous_summary": req.include_previous_summary,
                        "target_words": req.target_words or 0,
                        "selected_direction": req.turn_direction or "",
                        "user_note": req.user_note or "",
                        "skill_packs": [],
                        "harness_run_id": run_id_str,
                        "harness_step_id": None,
                        "chapter_task_card": {},
                        "structural_critique": {},
                        "edit_report": {},
                        "workflow_key": wf_continue.workflow_key if wf_continue else "",
                    }

                    _CONTINUE_STEP_NODE_MAP = {
                        "context_loader": ("build_context", 1),
                        "chapter_architect": ("plan_chapter", 2),
                        "chapter_writer": ("generate_draft", 3),
                        "continuity_checker": ("consistency_check", 4),
                    }
                    _CONTINUE_NODE_EVENT_MAP = {
                        "chapter_architect": "architect_output",
                        "chapter_writer": "writer_output",
                        "continuity_checker": "consistency_check",
                    }
                    _CONTINUE_STREAM_NODES = {"chapter_writer"}
                    writer_content = ""
                    workflow_skill_packs: list[dict] = []
                    seen_skill_pack_keys: set[tuple[str, str]] = set()
                    current_stream_node = None
                    active_steps: dict[str, object] = {}

                    try:
                        async for event in app.astream_events(initial_state, config=config, version="v2"):
                            if await _check_cancelled():
                                return
                            kind = event.get("event")
                            if kind == "on_chain_start":
                                node_name = event.get("name", "")
                                if node_name in _CONTINUE_NODE_EVENT_MAP:
                                    yield f"event: progress\ndata: {json.dumps({'message': f'{node_name} 节点执行中'}, ensure_ascii=False)}\n\n"
                                if node_name:
                                    yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                                    if node_name in _CONTINUE_STREAM_NODES:
                                        current_stream_node = node_name
                                if node_name in _CONTINUE_STEP_NODE_MAP and node_name not in active_steps:
                                    step_name, step_order = _CONTINUE_STEP_NODE_MAP[node_name]
                                    try:
                                        step = await start_step(db, run_id=ai_run.id, step_order=step_order, step_name=step_name, agent_name=node_name)
                                        active_steps[node_name] = step
                                        ai_run.current_step = step_name
                                        await db.commit()
                                        yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': step_name, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"
                                    except Exception:
                                        logger.warning("harness start_step 失败 node=%s", node_name, exc_info=True)

                            elif kind == "on_chain_end":
                                node_name = event.get("name", "")
                                output = event.get("data", {}).get("output", {})
                                if node_name:
                                    yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"
                                    if node_name in _CONTINUE_STREAM_NODES:
                                        current_stream_node = None
                                if node_name in _CONTINUE_STEP_NODE_MAP and node_name in active_steps:
                                    step = active_steps.pop(node_name)
                                    try:
                                        await finish_step(db, step, output={})
                                        yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': _CONTINUE_STEP_NODE_MAP[node_name][0], 'status': 'SUCCESS'}, ensure_ascii=False)}\n\n"
                                    except Exception:
                                        logger.warning("harness finish_step 失败 node=%s", node_name, exc_info=True)

                                for pack in _new_skill_packs(output, seen_skill_pack_keys):
                                    workflow_skill_packs.append(pack)
                                    yield _skill_pack_sse_event(pack, fallback_expert=node_name)

                                if node_name == "chapter_architect":
                                    card = output.get("chapter_task_card", {}) if isinstance(output, dict) else {}
                                    yield f"event: architect_output\ndata: {json.dumps({'task_card': card}, ensure_ascii=False)}\n\n"
                                elif node_name == "chapter_writer":
                                    draft = output.get("draft", "") if isinstance(output, dict) else ""
                                    writer_content = draft
                                    if draft:
                                        writer_payload = {"content": draft}
                                        initial_draft = output.get("writer_draft", "") if isinstance(output, dict) else ""
                                        if initial_draft:
                                            writer_payload["initial_draft"] = initial_draft
                                        yield f"event: writer_output\ndata: {json.dumps(writer_payload, ensure_ascii=False)}\n\n"
                                elif node_name == "continuity_checker":
                                    guardrail = output.get("consistency_report", {}) if isinstance(output, dict) else {}
                                    if not isinstance(guardrail, dict):
                                        guardrail = {}
                                    report_text = _guardrail_to_text(guardrail)
                                    yield f"event: consistency_check\ndata: {json.dumps({'report': report_text, 'guardrail_result': guardrail}, ensure_ascii=False)}\n\n"

                            elif kind == "on_llm_stream":
                                chunk_data = event.get("data", {})
                                chunk = chunk_data.get("chunk")
                                if chunk and current_stream_node:
                                    token = ""
                                    if hasattr(chunk, "choices") and chunk.choices:
                                        delta = chunk.choices[0].delta
                                        token = getattr(delta, "content", "") or ""
                                    elif isinstance(chunk, dict):
                                        choices = chunk.get("choices", [])
                                        if choices:
                                            token = choices[0].get("delta", {}).get("content", "") or ""
                                    if token:
                                        if current_stream_node == "chapter_writer":
                                            writer_content += token
                                        yield f"event: writer_output\ndata: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"

                        record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
                        yield _generation_record_event(record_id)
                        await mark_completed(db, ai_run)
                        await db.commit()
                        yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'COMPLETED'}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'message': '续写完成'}, ensure_ascii=False)}\n\n"
                    except WorkflowGenerationError as e:
                        logger.warning("continue v2 关键生成节点失败: %s", e)
                        await _discard_incomplete_generation_run(
                            db,
                            run_id=run_id_str,
                            thread_id=thread_id,
                            reason=str(e),
                        )
                        yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        logger.exception("continue v2 生成失败")
                        await _discard_incomplete_generation_run(
                            db,
                            run_id=run_id_str,
                            thread_id=thread_id,
                            reason=str(e),
                        )
                        yield f"event: error\ndata: {json.dumps({'message': f'生成失败: {str(e)}'}, ensure_ascii=False)}\n\n"
                    return

            # ==================== continue + expert_id 模式 ====================
            elif req.mode == "continue" and req.expert_id:
                eid = _to_uuid(req.expert_id)
                result = await db.execute(
                    select(Expert).where(Expert.id == eid, Expert.project_id == uid)
                )
                expert = result.scalar_one_or_none()
                if not expert:
                    yield f"event: error\ndata: {json.dumps({'message': '专家不存在'}, ensure_ascii=False)}\n\n"
                    return
                if not expert.is_enabled:
                    yield f"event: error\ndata: {json.dumps({'message': '专家已禁用'}, ensure_ascii=False)}\n\n"
                    return

                writer_content = ""
                is_writer = expert.role_type in WRITER_ROLES
                yield f"event: agent_start\ndata: {json.dumps({'agent': expert.name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                yield f"event: progress\ndata: {json.dumps({'message': f'专家 {expert.name} 生成中'}, ensure_ascii=False)}\n\n"
                expert_system_prompt = expert.system_prompt
                direct_skill_packs = []
                if not is_article_project:
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="expert_generate",
                        expert_role_type=expert.role_type,
                        expert_skill_dir=expert.skill_dir,
                        expert_name=expert.name,
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_chapter_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    expert_system_prompt = build_expert_system_prompt(
                        expert.role_type,
                        expert.system_prompt,
                        pack,
                        skill_dir=expert.skill_dir,
                    )
                    direct_skill_packs = [summary]
                expert_user_prompt = (
                    _article_generate_prompt(req, creative_context, chapter_content)
                    if is_article_project
                    else "继续创作"
                )
                async for chunk in provider.generate_stream(expert_system_prompt, expert_user_prompt):
                    if await _check_cancelled():
                        return
                    if is_writer:
                        writer_content += chunk
                    output_event = "content_output" if is_article_project else "writer_output"
                    yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': expert.name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                record_id = await _save_generation_history(
                    writer_content,
                    expert_id=eid if is_writer else None,
                    skill_packs=direct_skill_packs,
                )
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"
                return

            # ==================== summarize 模式 ====================
            elif req.mode == "summarize":
                # ── I-5: 补 AiRun + workflow 快照（仍走旧直接分支）──
                from services.novel_orchestrator import resolve_workflow as _resolve_wf_summarize
                _wf_summarize = _resolve_wf_summarize(project_mode=project.mode, mode="summarize")
                _summarize_run = await create_run(
                    db,
                    project_id=uid,
                    chapter_id=target_chapter_id,
                    document_id=target_document_id,
                    run_type="SUMMARY_GENERATION",
                    mode="summarize",
                    user_goal=req.user_note,
                    model_config_snapshot=llm_config_dict,
                    workflow_key=_wf_summarize.workflow_key if _wf_summarize else None,
                    workflow_version=_wf_summarize.version if _wf_summarize else None,
                    workflow_snapshot=workflow_to_snapshot(_wf_summarize),
                    expert_snapshot=[],
                )
                ai_run = _summarize_run
                await db.flush()
                _summarize_run_id = str(_summarize_run.id)
                yield f"event: run_created\ndata: {json.dumps({'run_id': _summarize_run_id, 'status': 'CREATED'}, ensure_ascii=False)}\n\n"
                await mark_running(db, _summarize_run)
                yield f"event: run_status\ndata: {json.dumps({'run_id': _summarize_run_id, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

                yield f"event: agent_start\ndata: {json.dumps({'agent': 'reader', 'step': 'running'}, ensure_ascii=False)}\n\n"
                if is_article_project:
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="summarize",
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_document_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    summarize_system_prompt = (
                        "你是一位目标受众研究员和内容编辑。请从目标受众视角评价文章/文案："
                        "是否清楚、有吸引力、可信、有行动动力，哪里啰嗦，哪里需要补充证据。"
                        "禁止使用小说章节、剧情、角色等评价口吻。用中文输出。"
                    )
                    summarize_prompt = (
                        f"## 内容 brief\n{_article_brief(req)}\n\n"
                        f"## 当前稿件\n{chapter_content or '(空稿件)'}\n\n"
                        "请从目标受众视角给出反馈。"
                    )
                    summarize_system_prompt = build_expert_system_prompt("summarizer", summarize_system_prompt, pack)
                    done_message = "受众反馈完成"
                else:
                    summarize_system_prompt = "你是一位普通读者。从阅读体验角度评价以下章节，给出真实感受：哪些段落吸引人、哪里节奏拖沓、角色是否立体、情节是否合理，以及是否与已知设定一致。用中文输出。"
                    summarize_prompt = f"## 上下文\n{creative_context}\n\n## 当前章节内容\n{chapter_content or '(空章节)'}\n\n请从读者视角分析这段内容，严格按照上下文中的设定进行评价："
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="summarize",
                    )
                    pack, summary = _planned_direct_skill_pack(
                        plan,
                        project_id=str(uid),
                        chapter_id=str(target_chapter_id or ""),
                        draft=chapter_content,
                        context=creative_context,
                        mode=req.mode,
                    )
                    yield _skill_pack_sse_event(summary)
                    summarize_system_prompt = build_expert_system_prompt("summarizer", summarize_system_prompt, pack)
                    done_message = "读者反馈完成"
                async for chunk in provider.generate_stream(
                    summarize_system_prompt,
                    summarize_prompt,
                ):
                    output_event = "content_output" if is_article_project else "writer_output"
                    yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'reader', 'step': 'success'}, ensure_ascii=False)}\n\n"
                # 不落库（只是反馈，不改原文）
                await mark_completed(db, _summarize_run)
                await db.commit()
                yield f"event: done\ndata: {json.dumps({'message': done_message}, ensure_ascii=False)}\n\n"
                return

            # ==================== full_pipeline 模式 ====================
            if is_article_project:
                from services.article_review import (
                    run_structure_review, run_audience_review,
                    run_platform_review, run_risk_review,
                )

                # Step 1: content_writer — 流式生成正文候选
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'content_writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                plan = plan_direct_skill_pack(
                    project_mode=project.mode,
                    generate_mode=req.mode,
                    action="full_pipeline_writer",
                )
                pack, summary = _planned_direct_skill_pack(
                    plan,
                    project_id=str(uid),
                    chapter_id=str(target_document_id or ""),
                    draft=chapter_content,
                    context=creative_context,
                    mode=req.mode,
                )
                yield _skill_pack_sse_event(summary)
                writer_content = ""
                async for chunk in provider.generate_stream(
                    build_expert_system_prompt(
                        "writer",
                        _article_system_prompt("生成完整文章/文案"),
                        pack,
                    ),
                    _article_generate_prompt(req, creative_context, chapter_content),
                    temperature=0.65,
                    max_tokens=min(settings.MAX_TOKENS_LIMIT, max(1024, int((req.target_words or 1200) * 1.8) + 512)),
                ):
                    if await _check_cancelled():
                        return
                    writer_content += chunk
                    yield f"event: content_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'content_writer', 'step': 'success'}, ensure_ascii=False)}\n\n"

                # Step 2: structure_review — 结构/标题检查
                yield f"event: progress\ndata: {json.dumps({'message': '结构/标题审校中'}, ensure_ascii=False)}\n\n"
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'structure_review', 'step': 'running'}, ensure_ascii=False)}\n\n"
                structure_result = await run_structure_review(provider, req, writer_content)
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'structure_review', 'step': 'success'}, ensure_ascii=False)}\n\n"
                yield f"event: article_review\ndata: {json.dumps({'review_type': 'structure', 'result': structure_result}, ensure_ascii=False)}\n\n"

                # Step 3: audience_review — 受众匹配检查
                yield f"event: progress\ndata: {json.dumps({'message': '受众匹配审校中'}, ensure_ascii=False)}\n\n"
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'audience_review', 'step': 'running'}, ensure_ascii=False)}\n\n"
                audience_result = await run_audience_review(provider, req, writer_content)
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'audience_review', 'step': 'success'}, ensure_ascii=False)}\n\n"
                yield f"event: article_review\ndata: {json.dumps({'review_type': 'audience', 'result': audience_result}, ensure_ascii=False)}\n\n"

                # Step 4: platform_review — 平台适配/CTA 检查
                yield f"event: progress\ndata: {json.dumps({'message': '平台/CTA审校中'}, ensure_ascii=False)}\n\n"
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'platform_review', 'step': 'running'}, ensure_ascii=False)}\n\n"
                platform_result = await run_platform_review(provider, req, writer_content)
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'platform_review', 'step': 'success'}, ensure_ascii=False)}\n\n"
                yield f"event: article_review\ndata: {json.dumps({'review_type': 'platform', 'result': platform_result}, ensure_ascii=False)}\n\n"

                # Step 5: risk_review — 风险/事实性提醒
                yield f"event: progress\ndata: {json.dumps({'message': '风险/事实性审校中'}, ensure_ascii=False)}\n\n"
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'risk_review', 'step': 'running'}, ensure_ascii=False)}\n\n"
                risk_result = await run_risk_review(provider, req, writer_content)
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'risk_review', 'step': 'success'}, ensure_ascii=False)}\n\n"
                yield f"event: article_review\ndata: {json.dumps({'review_type': 'risk', 'result': risk_result}, ensure_ascii=False)}\n\n"

                record_id = await _save_generation_history(
                    writer_content,
                    review_results={
                        "structure": structure_result,
                        "audience": audience_result,
                        "platform": platform_result,
                        "risk": risk_result,
                    },
                    skill_packs=[summary],
                )
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '内容生成与审校完成'}, ensure_ascii=False)}\n\n"
                return

            # 查询项目所有启用的专家（含内置和自定义）
            exp_result = await db.execute(
                select(Expert).where(Expert.project_id == uid, Expert.is_enabled == True)
            )
            enabled_experts = exp_result.scalars().all()

            app = get_creative_app_v2(
                planning_review=bool(req.planning_review),
                pre_generation_mode=req.pre_generation_mode or "PLANNING",
            )
            thread_id = f"{uid}:{target_chapter_id or target_document_id or 'no-chapter'}:{uuid.uuid4().hex[:8]}"
            workflow_thread_id = thread_id

            # ── Harness: 创建 AI Run ──
            run_type = {
                "full_pipeline": "CHAPTER_DRAFT",
                "continue": "CHAPTER_CONTINUE",
                "enhance": "CHAPTER_REWRITE",
                "summarize": "SUMMARY_GENERATION",
            }.get(req.mode, "CHAPTER_DRAFT")

            # ── Expert System v2: 解析 workflow 定义并写入快照 ──
            from services.novel_orchestrator import resolve_workflow
            wf = resolve_workflow(project_mode="novel", mode=req.mode)

            ai_run = await create_run(
                db,
                project_id=uid,
                chapter_id=target_chapter_id,
                document_id=target_document_id,
                run_type=run_type,
                mode=req.mode,
                user_goal=req.user_note,
                model_config_snapshot=llm_config_dict,
                workflow_key=wf.workflow_key if wf else None,
                workflow_version=wf.version if wf else None,
                workflow_snapshot=workflow_to_snapshot(wf),
                expert_snapshot=build_expert_snapshot(enabled_experts),
            )
            await db.flush()
            run_id_str = str(ai_run.id)
            yield f"event: run_created\ndata: {json.dumps({'run_id': run_id_str, 'status': 'CREATED'}, ensure_ascii=False)}\n\n"
            await mark_running(db, ai_run)
            yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

            # 从启用的专家中提取各角色的 system_prompt
            writer_prompt = ""
            critic_prompt = ""
            consistency_prompt = ""
            for exp in enabled_experts:
                if exp.role_type == "writer" and not writer_prompt:
                    writer_prompt = exp.system_prompt
                elif exp.role_type == "critic" and not critic_prompt:
                    critic_prompt = exp.system_prompt
                    if not consistency_prompt:
                        consistency_prompt = exp.system_prompt

            initial_state: CreativeStateV2 = {
                "project_id": str(uid),
                "chapter_id": str(target_chapter_id or target_document_id) if (target_chapter_id or target_document_id) else "",
                "chapter_num": chapter_num,
                "mode": req.mode,
                "context": "",
                "context_summary": {},
                "draft": "",
                "writer_draft": "",
                "original_text": "",
                "critiques": [],
                "consistency_report": {},
                "edited_draft": "",
                "revision_count": 0,
                "writer_prompt": writer_prompt,
                "critic_prompt": critic_prompt,
                "editor_prompt": "",
                "consistency_prompt": consistency_prompt,
                "llm_config": llm_config_dict,
                "selected_outline_ids": req.selected_outline_ids or [],
                "selected_character_ids": req.selected_character_ids or [],
                "selected_world_entry_ids": req.selected_world_entry_ids or [],
                "selected_hidden_thread_ids": req.selected_hidden_thread_ids or [],
                "include_knowledge_sources": req.include_knowledge_sources,
                "include_previous_summary": req.include_previous_summary,
                "excluded_context_keys": [],
                "target_words": req.target_words or 0,
                "selected_direction": req.selected_direction or "",
                "user_note": req.user_note or "",
                "skill_packs": [],
                # ── Harness 注入：节点内 LLM call log 用（不传 db，避免 msgpack 序列化失败）──
                "harness_run_id": run_id_str,
                "harness_step_id": None,
                # ── v2 新增 ──
                "chapter_task_card": {},
                "structural_critique": {},
                "edit_report": {},
                "workflow_key": wf.workflow_key if wf else "",
                # ── L-1: task card review ──
                "planning_review": bool(req.planning_review),
                "task_card_reviewed": False,
                "modified_task_card": {},
                # ── L-2: 嵌入式澄清 ──
                "clarification_answers": {},
                "clarification_round": 0,
                "embedded_clarification": True,
                "task_card_refresh_requested": False,
                "context_refresh_requested": False,
                # ── M-1: 生成前交互模式 ──
                "pre_generation_mode": req.pre_generation_mode or "PLANNING",
                "max_clarification_rounds": req.max_clarification_rounds or 3,
                # ── M-3: 澄清与任务卡解耦 ──
                "clarification_questions": [],
                "clarification_assumptions": [],
                "clarification_summary": "",
                "needs_clarification": False,
                "clarification_skipped": False,
                "requirements_complete": False,
            }

            config = {"configurable": {"thread_id": thread_id}}

            # 节点到 SSE 事件的映射（v2 节点名）
            NODE_EVENT_MAP = {
                "chapter_architect": "architect_output",
                "chapter_writer": "writer_output",
                "structural_critic": "critic_output",
                "narrative_editor": "editor_output",
                "continuity_checker": "consistency_check",
            }

            # 流式输出的节点（逐 token 发送）
            STREAM_NODES = {"chapter_writer"}

            writer_content = ""
            workflow_skill_packs: list[dict] = []
            seen_skill_pack_keys: set[tuple[str, str]] = set()
            current_stream_node = None  # 追踪当前正在流式输出的节点

            # ── Harness: step 追踪 ──
            STEP_NODE_MAP = {
                "context_loader": ("build_context", 1),
                "chapter_architect": ("plan_chapter", 2),
                "chapter_writer": ("generate_draft", 3),
                "structural_critic": ("critique", 4),
                "narrative_editor": ("edit_draft", 5),
                "continuity_checker": ("consistency_check", 6),
                "human_review": ("human_review", 7),
            }
            active_steps: dict[str, object] = {}  # node_name -> AiRunStep
            revision_round = 0

            # 逐节点流式执行
            async for event in app.astream_events(initial_state, config=config, version="v2"):
                # 客户端取消时立即停止，不继续跑后续节点
                if await _check_cancelled():
                    return
                kind = event.get("event")

                if kind == "on_chain_start":
                    node_name = event.get("name", "")
                    if node_name in NODE_EVENT_MAP:
                        yield f"event: progress\ndata: {json.dumps({'message': f'{node_name} 节点执行中'}, ensure_ascii=False)}\n\n"
                    if node_name:
                        yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                        if node_name in STREAM_NODES:
                            current_stream_node = node_name
                    # ── Harness: step start ──
                    # 跳过已激活的 node：LangGraph 的 astream_events 会对同一节点
                    # 触发多次 on_chain_start（链 + 节点），避免重复 INSERT step。
                    if node_name in STEP_NODE_MAP and node_name not in active_steps:
                        step_name, step_order = STEP_NODE_MAP[node_name]
                        try:
                            step = await start_step(
                                db,
                                run_id=ai_run.id,
                                step_order=step_order,
                                step_name=step_name,
                                agent_name=node_name,
                                revision_round=revision_round,
                            )
                            active_steps[node_name] = step
                            ai_run.current_step = step_name
                            # commit 而非仅 flush：节点可能用独立 session 操作同一 DB，
                            # 未 commit 的行在 finish_step 的 UPDATE 时不可见（StaleDataError）。
                            await db.commit()
                            # step_id 关联由 LoggedLLMProvider._resolve_running_step_id 查询完成，
                            # 不再通过 aupdate_state 注入（并行/动态节点下有竞态）。
                            yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': step_name, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"
                        except Exception:
                            logger.warning("harness start_step 失败 node=%s", node_name, exc_info=True)

                elif kind == "on_chain_end":
                    node_name = event.get("name", "")
                    output = event.get("data", {}).get("output", {})
                    if node_name:
                        yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"
                        if node_name in STREAM_NODES:
                            current_stream_node = None
                    # ── Harness: step finish ──
                    if node_name in STEP_NODE_MAP and node_name in active_steps:
                        step = active_steps.pop(node_name)
                        try:
                            output_snapshot: dict = {}
                            if isinstance(output, dict):
                                if node_name == "chapter_writer":
                                    output_snapshot = {"content_hash": str(hash(output.get("draft", "")))[:128]}
                                elif node_name == "chapter_architect":
                                    _card = output.get("chapter_task_card", {})
                                    output_snapshot = {"has_task_card": bool(_card), "scene_count": len(_card.get("scenes", [])) if isinstance(_card, dict) else 0}
                                elif node_name == "context_loader":
                                    output_snapshot = {"context_len": len(output.get("context", ""))}
                                elif node_name == "structural_critic":
                                    _crit = output.get("structural_critique", {})
                                    output_snapshot = {"p0_count": len(_crit.get("p0", [])) if isinstance(_crit, dict) else 0}
                                elif node_name == "narrative_editor":
                                    output_snapshot = {"content_hash": str(hash(output.get("draft", "")))[:128]}
                                elif node_name == "continuity_checker":
                                    _gr = output.get("consistency_report", {})
                                    _issues = _gr.get("issues", []) if isinstance(_gr, dict) else []
                                    output_snapshot = {"issue_count": len(_issues), "overall_severity": _gr.get("overall_severity", "info") if isinstance(_gr, dict) else "info"}
                            await finish_step(db, step, output=output_snapshot)
                            yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': STEP_NODE_MAP[node_name][0], 'status': 'SUCCESS'}, ensure_ascii=False)}\n\n"
                        except Exception:
                            logger.warning("harness finish_step 失败 node=%s", node_name, exc_info=True)

                    for pack in _new_skill_packs(output, seen_skill_pack_keys):
                        workflow_skill_packs.append(pack)
                        yield _skill_pack_sse_event(pack, fallback_expert=node_name)

                    if node_name == "context_loader":
                        yield f"event: progress\ndata: {json.dumps({'message': '上下文加载完成'}, ensure_ascii=False)}\n\n"

                    elif node_name == "clarification_planner":
                        # M-3: planner 节点结束后，如果 graph 下一步是 human_clarification，
                        # 立即发澄清事件。human_clarification 本身是 interrupt_before 节点，
                        # 不会真的进入 on_chain_end。
                        if req.planning_review:
                            _hc_state = await app.aget_state(config)
                            _hc_next = _hc_state.next if _hc_state else ()
                            if "human_clarification" in _hc_next:
                                _hc_values = _hc_state.values if _hc_state else {}
                                _hc_mode = _hc_values.get("pre_generation_mode", "PLANNING")
                                _hc_round = int(_hc_values.get("clarification_round", 1) or 1)
                                _hc_max_rounds = int(_hc_values.get("max_clarification_rounds", 3) or 3)
                                _hc_questions = _hc_values.get("clarification_questions", [])
                                _hc_assumptions = _hc_values.get("clarification_assumptions", [])
                                _hc_require_answer = (_hc_mode == "STRICT" and _hc_round <= 1)
                                await mark_waiting_human(db, ai_run, step_name="human_clarification", thread_id=thread_id)
                                interrupt = await create_interrupt(
                                    db,
                                    run=ai_run,
                                    thread_id=thread_id,
                                    step_name="human_clarification",
                                    payload={
                                        "type": "clarification_questions",
                                        "questions": _hc_questions,
                                        "round": _hc_round,
                                        "max_rounds": _hc_max_rounds,
                                        "assumptions_if_skipped": _hc_assumptions,
                                        "pre_generation_mode": _hc_mode,
                                        "require_answer": _hc_require_answer,
                                    },
                                )
                                await db.commit()
                                yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                                _clar_payload = {
                                    "run_id": run_id_str,
                                    "interrupt_id": str(interrupt.id) if interrupt else None,
                                    "thread_id": thread_id,
                                    "round": _hc_round,
                                    "max_rounds": _hc_max_rounds,
                                    "questions": _hc_questions,
                                    "assumptions_if_skipped": _hc_assumptions,
                                    "pre_generation_mode": _hc_mode,
                                    "require_answer": _hc_require_answer,
                                }
                                yield f"event: clarification_required\ndata: {json.dumps(_clar_payload, ensure_ascii=False)}\n\n"
                                return

                    elif node_name == "chapter_architect":
                        card = output.get("chapter_task_card", {}) if isinstance(output, dict) else {}
                        yield f"event: architect_output\ndata: {json.dumps({'task_card': card}, ensure_ascii=False)}\n\n"

                        # ── L-1: task_card_review — 任务卡预览中断（M-3: 不再含 clarification） ──
                        if req.planning_review:
                            _tc_state = await app.aget_state(config)
                            _tc_next = _tc_state.next if _tc_state else ()
                            if "task_card_review" in _tc_next:
                                _tc_values = _tc_state.values if _tc_state else {}
                                _tc_mode = _tc_values.get("pre_generation_mode", "PLANNING")
                                _embedded_payload = _embedded_clarification_payload(_tc_values)
                                _clarification_status = _task_card_clarification_status(_tc_values)
                                await mark_waiting_human(db, ai_run, step_name="task_card_review", thread_id=thread_id)
                                interrupt = await create_interrupt(
                                    db,
                                    run=ai_run,
                                    thread_id=thread_id,
                                    step_name="task_card_review",
                                    payload={
                                        "task_card": card,
                                        "clarification": _embedded_payload,
                                        "clarification_status": _clarification_status,
                                        "context_summary": _tc_values.get("context_summary", {}),
                                    },
                                )
                                await db.commit()
                                yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                                yield f"event: task_card_review_required\ndata: {json.dumps({'task_card': card, 'thread_id': thread_id, 'pre_generation_mode': _tc_mode, 'clarification': _embedded_payload, 'clarification_status': _clarification_status, 'context_summary': _tc_values.get('context_summary', {})}, ensure_ascii=False)}\n\n"
                                return

                    elif node_name == "chapter_writer":
                        draft = output.get("draft", "") if isinstance(output, dict) else ""
                        writer_content = draft
                        if draft:
                            writer_payload = {"content": draft}
                            initial_draft = output.get("writer_draft", "") if isinstance(output, dict) else ""
                            if initial_draft:
                                writer_payload["initial_draft"] = initial_draft
                            yield f"event: writer_output\ndata: {json.dumps(writer_payload, ensure_ascii=False)}\n\n"

                    elif node_name == "structural_critic":
                        critiques = output.get("critiques", []) if isinstance(output, dict) else []
                        critique = output.get("structural_critique", {}) if isinstance(output, dict) else {}
                        yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques, 'structural_critique': critique}, ensure_ascii=False)}\n\n"

                    elif node_name == "narrative_editor":
                        draft = output.get("draft", "") if isinstance(output, dict) else ""
                        writer_content = draft  # editor 覆盖 draft，更新 writer_content
                        yield f"event: editor_output\ndata: {json.dumps({'content': draft}, ensure_ascii=False)}\n\n"

                    elif node_name == "continuity_checker":
                        guardrail = output.get("consistency_report", {}) if isinstance(output, dict) else {}
                        if not isinstance(guardrail, dict):
                            guardrail = {}
                        report_text = _guardrail_to_text(guardrail)
                        yield f"event: consistency_check\ndata: {json.dumps({'report': report_text, 'guardrail_result': guardrail}, ensure_ascii=False)}\n\n"

                    elif node_name == "human_review":
                        record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
                        yield _generation_record_event(record_id)
                        await mark_waiting_human(db, ai_run, step_name="human_review", thread_id=thread_id)

                        # ── Harness G: 提取 blocking_issues（HIGH severity guardrail）──
                        _hr_state = await app.aget_state(config)
                        _hr_guardrail = _hr_state.values.get("consistency_report", {}) if _hr_state else {}
                        _hr_blocking = get_blocking_issues(_hr_guardrail) if isinstance(_hr_guardrail, dict) else []

                        # ── Harness E: 创建 human_interrupt 记录 ──
                        interrupt = await create_interrupt(
                            db,
                            run=ai_run,
                            thread_id=thread_id,
                            step_name="human_review",
                            payload={
                                "generation_record_id": record_id,
                                "content_hash": hash(writer_content) if writer_content else None,
                                "blocking_issues": _hr_blocking,
                            },
                        )

                        await db.commit()
                        yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                        yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                        return

                elif kind == "on_llm_stream":
                    # 流式 token 输出
                    chunk_data = event.get("data", {})
                    chunk = chunk_data.get("chunk")
                    if chunk and current_stream_node:
                        token = ""
                        if hasattr(chunk, "choices") and chunk.choices:
                            delta = chunk.choices[0].delta
                            token = getattr(delta, "content", "") or ""
                        elif isinstance(chunk, dict):
                            choices = chunk.get("choices", [])
                            if choices:
                                token = choices[0].get("delta", {}).get("content", "") or ""
                        if token:
                            if current_stream_node == "chapter_writer":
                                writer_content += token
                            yield f"event: writer_output\ndata: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"

            # 检查工作流是否在 human_review 处暂停（HITL）
            # interrupt_before 使节点不执行，on_chain_start 不会为被中断节点触发，
            # 所以必须通过检查工作流状态来判断是否暂停
            workflow_state = await app.aget_state(config)
            next_nodes = workflow_state.next if workflow_state else []
            if "human_review" in next_nodes:
                record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
                yield _generation_record_event(record_id)
                await mark_waiting_human(db, ai_run, step_name="human_review", thread_id=thread_id)

                # ── Harness G: 提取 blocking_issues（HIGH severity guardrail）──
                _fb_guardrail = workflow_state.values.get("consistency_report", {}) if workflow_state else {}
                _fb_blocking = get_blocking_issues(_fb_guardrail) if isinstance(_fb_guardrail, dict) else []

                # ── Harness E: 创建 human_interrupt 记录 ──
                interrupt = await create_interrupt(
                    db,
                    run=ai_run,
                    thread_id=thread_id,
                    step_name="human_review",
                    payload={
                        "generation_record_id": record_id,
                        "content_hash": hash(writer_content) if writer_content else None,
                        "blocking_issues": _fb_blocking,
                    },
                )

                await db.commit()
                yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                return

            record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
            yield _generation_record_event(record_id)
            await mark_completed(db, ai_run)
            await db.commit()
            yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'COMPLETED'}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"

        except WorkflowGenerationError as e:
            logger.warning("关键生成节点失败: %s", e)
            await _discard_incomplete_generation_run(
                db,
                run_id=str(ai_run.id) if ai_run is not None else None,
                thread_id=workflow_thread_id,
                reason=str(e),
            )
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("生成失败")
            await _discard_incomplete_generation_run(
                db,
                run_id=str(ai_run.id) if ai_run is not None else None,
                thread_id=workflow_thread_id,
                reason=str(e),
            )
            yield f"event: error\ndata: {json.dumps({'message': f'生成失败: {str(e)}'}, ensure_ascii=False)}\n\n"
        finally:
            finish_langfuse_context(langfuse_context)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ==================== 写作记忆 staging ====================

@router.get("/projects/{project_id}/memory-staging", response_model=list[WritingMemoryStagingResponse])
async def list_memory_staging(
    project_id: str,
    status: str | None = Query(default=None, pattern=r"^(GENERATED|CONFIRMED|REJECTED)$"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出项目的写作记忆 staging 记录，可选按 status 过滤。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    query = select(WritingMemoryStaging).where(WritingMemoryStaging.project_id == uid)
    if status:
        query = query.where(WritingMemoryStaging.status == status)
    query = query.order_by(WritingMemoryStaging.created_at.desc())
    result = await db.execute(query)
    return [WritingMemoryStagingResponse.model_validate(item) for item in result.scalars().all()]


async def _transition_staging_status(
    db: AsyncSession,
    project_id: uuid.UUID,
    staging_id: str,
    target_status: str,
    user: AuthUser,
) -> WritingMemoryStaging:
    """通用状态转换：confirm → CONFIRMED, reject → REJECTED。

    跨状态规则：
    - GENERATED → CONFIRMED/REJECTED  OK
    - 同状态 → 幂等返回
    - CONFIRMED → REJECTED 或 REJECTED → CONFIRMED → 409
    """
    await _verify_project_owner(project_id, user.id, db)
    sid = _to_uuid(staging_id)
    result = await db.execute(
        select(WritingMemoryStaging).where(
            WritingMemoryStaging.id == sid,
            WritingMemoryStaging.project_id == project_id,
        )
    )
    staging = result.scalar_one_or_none()
    if not staging:
        raise HTTPException(status_code=404, detail="记忆记录不存在")

    if staging.status == target_status:
        # 幂等
        return staging

    # 跨状态冲突检查
    if staging.status in ("CONFIRMED", "REJECTED") and target_status != staging.status:
        raise HTTPException(
            status_code=409,
            detail=f"记忆记录当前状态为 {staging.status}，不可转为 {target_status}",
        )

    staging.status = target_status
    staging.reviewed_at = datetime.now(timezone.utc)
    staging.reviewed_by = user.id
    return staging


@router.post("/projects/{project_id}/memory-staging/{staging_id}/confirm", response_model=WritingMemoryStagingResponse)
async def confirm_memory_staging(
    project_id: str,
    staging_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """确认写作记忆 staging，写入 workbench 正式表。

    H3a: confirm 时按 memory_type 写入 Character/WorldEntry/CharacterEvent/HiddenThread。
    PLOT_FACT 不写正式表，只保持 CONFIRMED staging。
    """
    from services.memory_staging_service import confirm_staging_item

    staging = await _transition_staging_status(
        db, _to_uuid(project_id), staging_id, "CONFIRMED", user
    )
    # 写正式表（与 staging 状态更新同一事务）
    await confirm_staging_item(db, staging, background_tasks=background_tasks)
    await db.commit()
    await db.refresh(staging)
    return WritingMemoryStagingResponse.model_validate(staging)


@router.post("/projects/{project_id}/memory-staging/{staging_id}/reject", response_model=WritingMemoryStagingResponse)
async def reject_memory_staging(
    project_id: str,
    staging_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """拒绝写作记忆 staging。"""
    staging = await _transition_staging_status(
        db, _to_uuid(project_id), staging_id, "REJECTED", user
    )
    await db.commit()
    await db.refresh(staging)
    return WritingMemoryStagingResponse.model_validate(staging)


# ==================== AI Runs ====================

@router.get("/ai-runs/{run_id}", response_model=AiRunResponse)
async def get_ai_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询单个 AI Run 详情。校验 run 的 project 属于当前用户。"""
    rid = _to_uuid(run_id)
    result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)
    return AiRunResponse.model_validate(run)


@router.get("/ai-runs/{run_id}/steps", response_model=list[AiRunStepResponse])
async def get_ai_run_steps(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询 Run 的步骤列表，按 step_order 升序。含每步 llm_call_count。
    排序稳定：step_order 相同时用 started_at（nulls last），再兜底 created_at。"""
    from models.ai_run_step import AiRunStep
    from models.llm_call_log import LlmCallLog
    from sqlalchemy import func

    rid = _to_uuid(run_id)
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    result = await db.execute(
        select(
            AiRunStep,
            func.count(LlmCallLog.id).label("llm_call_count"),
        )
        .outerjoin(LlmCallLog, LlmCallLog.step_id == AiRunStep.id)
        .where(AiRunStep.run_id == rid)
        .group_by(AiRunStep.id)
        .order_by(
            AiRunStep.step_order,
            AiRunStep.started_at.nulls_last(),
            AiRunStep.created_at,
        )
    )
    return [
        AiRunStepResponse(
            id=row.AiRunStep.id,
            run_id=row.AiRunStep.run_id,
            step_order=row.AiRunStep.step_order,
            step_name=row.AiRunStep.step_name,
            agent_name=row.AiRunStep.agent_name,
            status=row.AiRunStep.status,
            error_message=row.AiRunStep.error_message,
            started_at=row.AiRunStep.started_at,
            ended_at=row.AiRunStep.ended_at,
            llm_call_count=row.llm_call_count,
        )
        for row in result.all()
    ]


@router.get("/ai-runs/{run_id}/context", response_model=AiRunContextResponse)
async def get_ai_run_context(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查看一次生成实际使用过的上下文快照。

    数据来自 llm_call_logs：优先展示从 rendered_prompt_snapshot 中抽出的
    `## 上下文` / `## 可用上下文` 段，同时保留 context_package_snapshot 摘要。
    """
    rid = _to_uuid(run_id)
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    result = await db.execute(
        select(LlmCallLog, AiRunStep.step_name)
        .outerjoin(AiRunStep, LlmCallLog.step_id == AiRunStep.id)
        .where(LlmCallLog.run_id == rid)
        .order_by(LlmCallLog.created_at.asc())
    )
    calls = [
        AiRunContextCallResponse(
            id=row.LlmCallLog.id,
            run_id=row.LlmCallLog.run_id,
            step_id=row.LlmCallLog.step_id,
            step_name=row.step_name,
            agent_name=row.LlmCallLog.agent_name,
            provider=row.LlmCallLog.provider,
            model=row.LlmCallLog.model,
            context_snapshot=row.LlmCallLog.context_package_snapshot,
            context_text=_extract_context_text_from_prompt(row.LlmCallLog.rendered_prompt_snapshot),
            prompt_snapshot=row.LlmCallLog.rendered_prompt_snapshot,
            prompt_truncated=_prompt_snapshot_was_truncated(row.LlmCallLog.request),
            error_message=row.LlmCallLog.error_message,
            created_at=row.LlmCallLog.created_at,
        )
        for row in result.all()
    ]
    return AiRunContextResponse(
        run_id=run.id,
        project_id=run.project_id,
        mode=run.mode,
        status=run.status,
        workflow_key=run.workflow_key,
        calls=calls,
    )


@router.get("/projects/{project_id}/ai-runs", response_model=list[AiRunListItemResponse])
async def list_project_ai_runs(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询项目的 AI Run 列表，按 created_at 降序。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(AiRun)
        .where(AiRun.project_id == uid)
        .order_by(AiRun.created_at.desc())
        .limit(limit)
    )
    return [AiRunListItemResponse.model_validate(run) for run in result.scalars().all()]


@router.post("/ai-runs/{run_id}/human-decisions", response_model=HumanDecisionResponse)
async def create_human_decision(
    run_id: str,
    request: HumanDecisionRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """为 AI Run 提交人工审核决策。

    新的 human-decisions API，支持幂等性保护。
    使用 run_id 而不是 thread_id 作为主要标识。

    Args:
        run_id: AI Run ID
        request: 决策请求（decision + feedback）
        db: 数据库会话
        user: 当前用户

    Returns:
        决策响应，包含 run_id、interrupt_id、status、message
    """
    rid = _to_uuid(run_id)
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    # 查找最新的 interrupt
    interrupt = await get_interrupt_by_run(db, str(run.id))
    if not interrupt:
        raise HTTPException(status_code=404, detail="未找到对应的 human interrupt")

    # 幂等性检查：如果已解析，直接返回
    if interrupt.resolved:
        return HumanDecisionResponse(
            run_id=str(run.id),
            interrupt_id=str(interrupt.id),
            status="already_resolved",
            message=f"该 interrupt 已于 {interrupt.resolved_at} 解析为 {interrupt.decision}",
        )

    # 解析 interrupt
    decision_enum = InterruptDecision(request.decision)
    await resolve_interrupt(db, interrupt, decision=decision_enum, feedback=request.feedback)

    # 根据决策更新 run 状态
    if decision_enum == InterruptDecision.APPROVE:
        await mark_completed(db, run)
        message = "已批准，run 标记为 COMPLETED"
    elif decision_enum == InterruptDecision.REJECT:
        await mark_failed(db, run, error_message="用户拒绝")
        message = "已拒绝，run 标记为 FAILED"
    elif decision_enum == InterruptDecision.EDIT:
        # EDIT 保持 WAITING_HUMAN 状态，等待编辑后再次提交
        message = "已记录编辑决策，等待用户修改后继续"
    elif decision_enum == InterruptDecision.REGENERATE:
        # REGENERATE 将触发重新生成（需要调用 resume API）
        message = "已记录重新生成决策，需要调用 resume API 继续"
    else:
        message = "决策已记录"

    await db.commit()

    return HumanDecisionResponse(
        run_id=str(run.id),
        interrupt_id=str(interrupt.id),
        status="resolved",
        message=message,
    )


# ==================== Clarification Loop API ====================

@router.get("/ai-runs/{run_id}/clarification", response_model=ClarificationResponse, deprecated=True)
async def get_clarification(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """[N-1 deprecated] 获取指定 run 的最新 clarification interrupt 状态。

    M-3 后澄清链路统一走 resume action=submit_clarification/skip_clarification，
    本接口仅供历史 run 查询保留，不再用于主流程。

    返回问题列表、已有回答、轮次等信息。
    如果没有 clarification interrupt，返回 status="none"。
    """
    rid = _to_uuid(run_id)
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    from services.clarification_interrupt import get_latest_clarification_interrupt, format_clarification_response

    interrupt = await get_latest_clarification_interrupt(db, run.id)
    if not interrupt:
        return ClarificationResponse(
            run_id=str(run.id),
            status="none",
            resolved=False,
        )
    data = format_clarification_response(interrupt)
    return ClarificationResponse(**data)


@router.post("/ai-runs/{run_id}/clarification-answers", response_model=ClarificationResponse, deprecated=True)
async def submit_clarification_answers(
    run_id: str,
    req: ClarificationAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """[N-1 deprecated] 提交澄清回答或跳过澄清。

    M-3 后澄清链路统一走 resume action=submit_clarification/skip_clarification，
    本接口仅供历史 run 保留，不再用于主流程。

    action=submit: 把 answers 写入 payload，resolve interrupt
    action=skip: 标记跳过，resolve interrupt（使用 assumptions_if_skipped）

    幂等性：已 resolved 的 interrupt 返回当前状态（不报错）。
    """
    rid = _to_uuid(run_id)
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    from services.clarification_interrupt import (
        get_latest_clarification_interrupt, submit_clarification_answers as _submit,
        skip_clarification as _skip, format_clarification_response,
    )

    interrupt = await get_latest_clarification_interrupt(db, run.id)
    if not interrupt:
        raise HTTPException(status_code=404, detail="未找到 clarification interrupt")

    if interrupt.resolved:
        # 幂等：已 resolved 返回当前状态
        data = format_clarification_response(interrupt)
        return ClarificationResponse(**data)

    try:
        if req.action == "skip":
            await _skip(db, interrupt)
        else:
            await _submit(db, interrupt, answers=req.answers)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await db.commit()

    data = format_clarification_response(interrupt)
    return ClarificationResponse(**data)


# ==================== 工作流恢复（HITL） ====================

@router.post("/projects/{project_id}/documents/resume")
@router.post("/projects/{project_id}/chapters/resume")
async def resume_chapter_generation(
    request: Request,
    project_id: str,
    thread_id: str = Query(..., description="HITL 暂停时返回的 thread_id"),
    action: str = Query(default="approve", pattern=r"^(approve|reject|review|revise|approve_task_card|reject_task_card|refresh_task_card|refresh_task_card_context|submit_clarification|skip_clarification)$"),
    feedback: str | None = None,
    task_card: str | None = Query(default=None, description="任务卡 JSON（approve_task_card 时传入）"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """恢复 HITL 暂停的工作流。

    用户对 Editor 输出做出决策后，恢复工作流执行：
    - approve: 结束流程，落库最终内容
    - reject: 终止流程，不落库
    - review: 审核当前候选稿，返回可选修改方向
    - revise: 将用户选择的修改方向注入状态，重新从 Writer 开始
    - approve_task_card: 用户确认/修改任务卡后继续执行（L-1）
    - reject_task_card: 用户取消任务卡预览，终止流程（L-1）
    - refresh_task_card: 提交内嵌澄清回答，重新生成任务卡（L-2）
    - refresh_task_card_context: 应用上下文排除列表，重新构建上下文和任务卡（L-3）
    - submit_clarification: 提交生成前澄清答案，继续 v2 规划
    - skip_clarification: 跳过澄清，按默认假设继续 v2 规划

    这个接口和 generate_chapter 配套：generate 遇到 interrupt_before 节点时返回
    thread_id；resume 用 thread_id 从 LangGraph checkpointer 取回状态并继续执行。
    """
    # Rate limiting
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    project_result = await db.execute(select(Project).where(Project.id == uid))
    project = project_result.scalar_one()
    if "/documents/" in request.url.path and project.mode != "article":
        raise HTTPException(status_code=400, detail="Document generation only available for article projects")

    # 查询项目启用的专家，用于 HITL 恢复时构建正确的图。
    # 旧 workflow 的图结构依赖 enabled_experts；resume 时必须用同一套专家配置。
    exp_result = await db.execute(
        select(Expert).where(Expert.project_id == uid, Expert.is_enabled == True)
    )
    enabled_experts = exp_result.scalars().all()

    # ── v2 判断：按 AiRun.workflow_key 决定走 v2 还是旧 workflow ──
    # 旧 workflow 没有 workflow_key，v2 run 会写 workflow_key。
    # 不能只靠 action 判断，因为 approve/revise 两套流程都有。
    _resume_run_check = (
        await db.execute(select(AiRun).where(AiRun.thread_id == thread_id, AiRun.project_id == uid))
    ).scalar_one_or_none()
    if _resume_run_check and _resume_run_check.workflow_key:
        # L-1: 从 state 读取 planning_review 配置，保证 resume 图与生成时一致。
        # 如果 generate 时图里有 task_card_review/human_clarification，
        # resume 时也必须编译出相同 interrupt 节点，否则 LangGraph 下一跳会错位。
        _checkpoint_config = {"configurable": {"thread_id": thread_id}}
        _planning_actions = {"approve_task_card", "reject_task_card", "refresh_task_card", "refresh_task_card_context", "submit_clarification", "skip_clarification"}
        _planning = action in _planning_actions
        _pre_generation_mode = "PLANNING"
        _embedded_clarification = True
        try:
            _pre_state = await get_creative_app_v2(planning_review=True, pre_generation_mode="PLANNING").aget_state(_checkpoint_config)
            if _pre_state and _pre_state.values:
                _planning = bool(_pre_state.values.get("planning_review", False)) or _planning
                _pre_generation_mode = _pre_state.values.get("pre_generation_mode", "PLANNING") or "PLANNING"
                if "embedded_clarification" in _pre_state.values:
                    _embedded_clarification = bool(_pre_state.values.get("embedded_clarification"))
                else:
                    # 兼容早期 L-2 run：如果 checkpoint 已停在 task_card_review，
                    # 说明它原本就是嵌入式澄清拓扑；旧 human_clarification run
                    # 则继续按旧图恢复。
                    _embedded_clarification = "task_card_review" in set(_pre_state.next or ())
        except Exception:
            pass
        app = get_creative_app_v2(
            planning_review=_planning,
            pre_generation_mode=_pre_generation_mode,
            embedded_clarification=_embedded_clarification,
        )
    else:
        app = get_creative_app(enabled_experts=enabled_experts)
    config = {"configurable": {"thread_id": thread_id}}

    # 兼容两种传参方式：老前端可能用 query 参数 feedback/task_card，
    # 新前端可能把它们放在 JSON body。
    body_data: dict = {}
    try:
        body_raw = await request.body()
        if body_raw:
            parsed_body = json.loads(body_raw)
            if isinstance(parsed_body, dict):
                body_data = parsed_body
    except Exception:
        body_data = {}

    resume_feedback = feedback
    body_feedback = body_data.get("feedback")
    if resume_feedback is None and isinstance(body_feedback, str):
        resume_feedback = body_feedback

    resume_task_card: str | dict | None = task_card
    body_task_card = body_data.get("task_card")
    if resume_task_card is None and isinstance(body_task_card, (str, dict)):
        resume_task_card = body_task_card

    async def event_stream():
        """resume 的 SSE 生成器。

        它会根据 action 修改 LangGraph state，然后继续 astream_events。
        如果 action=approve，则直接把当前候选稿落库；如果 action=revise，
        则把反馈写入 critiques 并重新跑 writer/editor/checker。
        """
        langfuse_context = activate_langfuse_context(
            name="resume_generation",
            user_id=str(user.id),
            session_id=str(uid),
            tags=["article" if project.mode == "article" else "novel", action],
            metadata={
                "project_id": str(uid),
                "thread_id": thread_id,
                "action": action,
                "endpoint": request.url.path,
            },
        )
        _resume_run_id: str | None = None
        try:
            # 获取当前状态。MemorySaver 中没有 thread_id 时，说明服务重启或状态过期。
            state = await app.aget_state(config)
            if not state or not state.values:
                yield f"event: error\ndata: {json.dumps({'message': '工作流状态不存在或已过期'}, ensure_ascii=False)}\n\n"
                return

            state_project_id = state.values.get("project_id")
            if state_project_id and str(state_project_id) != str(uid):
                logger.warning(
                    "拒绝跨项目恢复工作流: thread_id=%s requested_project=%s state_project=%s",
                    thread_id,
                    uid,
                    state_project_id,
                )
                yield f"event: error\ndata: {json.dumps({'message': '工作流不属于当前项目'}, ensure_ascii=False)}\n\n"
                return

            # 规划阶段的恢复动作只能在其对应的 interrupt 前执行。否则调用方可以
            # 越过澄清或任务卡审核，强行把状态写入错误的图节点，破坏后续恢复语义。
            expected_interrupts = {
                "approve": "human_review",
                "reject": "human_review",
                "review": "human_review",
                "revise": "human_review",
                "approve_task_card": "task_card_review",
                "reject_task_card": "task_card_review",
                "refresh_task_card": "task_card_review",
                "refresh_task_card_context": "task_card_review",
                "submit_clarification": "human_clarification",
                "skip_clarification": "human_clarification",
            }
            expected_interrupt = expected_interrupts.get(action)
            if expected_interrupt and expected_interrupt not in set(state.next or ()):
                yield f"event: error\ndata: {json.dumps({'message': f'当前工作流不在 {expected_interrupt} 审核点，不能执行 {action}'}, ensure_ascii=False)}\n\n"
                return

            # ── Harness: 按 thread_id 反查 run_id（resume 路径关联 run）──
            # 这样 approve/revise 后的 GenerationRecord、版本和 LLM 日志都能串回同一次 AiRun。
            _resume_run = (
                await db.execute(select(AiRun).where(AiRun.thread_id == thread_id, AiRun.project_id == uid))
            ).scalar_one_or_none()
            _resume_run_id = str(_resume_run.id) if _resume_run else None

            async def _discard_resume_run(reason: str) -> None:
                await _discard_incomplete_generation_run(
                    db,
                    run_id=_resume_run_id,
                    thread_id=thread_id,
                    reason=reason,
                )

            if _resume_run and str(_resume_run.status) in {"CANCELLED", "COMPLETED", "FAILED"}:
                yield f"event: error\ndata: {json.dumps({'message': f'该 AI 任务已处于 {_resume_run.status} 状态，不能重复恢复'}, ensure_ascii=False)}\n\n"
                return

            # ── Harness E: 幂等性检查 - 防止重复 approve ──
            # approve 会产生正式版本和写作记忆抽取，重复执行会造成重复版本。
            if _resume_run and action == "approve":
                interrupt = await get_interrupt_by_thread(db, thread_id)
                if interrupt and interrupt.resolved:
                    yield f"event: error\ndata: {json.dumps({'message': f'该任务已于 {interrupt.resolved_at} 处理为 {interrupt.decision}，不可重复操作'}, ensure_ascii=False)}\n\n"
                    return

            async def _save_resume_generation_history(content: str, values: dict) -> str | None:
                target_content_id = values.get("chapter_id", "")
                if not content.strip() or not target_content_id:
                    return None
                try:
                    content_id = _to_uuid(target_content_id)
                    record = await create_generation_record(
                        db,
                        project_id=uid,
                        chapter_id=None if project.mode == "article" else content_id,
                        document_id=content_id if project.mode == "article" else None,
                        mode=values.get("mode", "full_pipeline"),
                        content=content,
                        skill_packs=values.get("skill_packs") or None,
                        langfuse_trace_id=current_langfuse_trace_id(),
                        run_id=_resume_run_id,
                    )
                    if not record:
                        return None
                    await db.commit()
                    return str(record.id)
                except Exception:
                    await db.rollback()
                    logger.exception("HITL生成历史保存失败")
                    return None

            def _generation_record_event(record_id: str | None) -> str:
                if not record_id:
                    return ""
                return (
                    "event: generation_record\n"
                    f"data: {json.dumps({'id': record_id, 'status': 'candidate', 'langfuse_trace_id': current_langfuse_trace_id()}, ensure_ascii=False)}\n\n"
                )

            def _append_note_once(existing: str, segment: str) -> str:
                """Append a user-facing note segment without duplicating it across task-card refreshes."""
                existing = (existing or "").strip()
                segment = (segment or "").strip()
                if not segment:
                    return existing
                if segment in existing:
                    return existing
                return f"{existing}\n{segment}".strip() if existing else segment

            async def _stream_planning_resume():
                """Continue the planning graph until the next human-facing checkpoint.

                Used by M-3 clarification submit/skip. It resumes the graph from
                human_clarification, then stops at either another clarification
                checkpoint or the task-card review checkpoint.
                """
                if _resume_run:
                    await mark_running(db, _resume_run)
                    await db.commit()
                    yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

                resume_stream = app.astream_events(None, config=config, version="v2")
                resume_iter = resume_stream.__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(resume_iter.__anext__(), timeout=180)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        aclose = getattr(resume_stream, "aclose", None)
                        if aclose:
                            await aclose()
                        await _discard_resume_run("规划阶段超时")
                        yield f"event: error\ndata: {json.dumps({'message': '规划阶段超过 180 秒没有响应，请稍后重试或检查模型服务'}, ensure_ascii=False)}\n\n"
                        return
                    if await request.is_disconnected():
                        logger.info("客户端已断开连接，取消规划恢复")
                        await _discard_resume_run("客户端断开连接")
                        return

                    kind = event.get("event")
                    if kind == "on_chain_start":
                        node_name = event.get("name", "")
                        if node_name:
                            yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                    elif kind == "on_chain_end":
                        node_name = event.get("name", "")
                        output = event.get("data", {}).get("output", {})
                        if node_name:
                            yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                        if node_name == "clarification_planner":
                            _hc_state = await app.aget_state(config)
                            _hc_next = _hc_state.next if _hc_state else ()
                            if "human_clarification" in _hc_next:
                                _hc_values = _hc_state.values if _hc_state else {}
                                _hc_mode = _hc_values.get("pre_generation_mode", "PLANNING")
                                _hc_round = int(_hc_values.get("clarification_round", 1) or 1)
                                _hc_max_rounds = int(_hc_values.get("max_clarification_rounds", 3) or 3)
                                _hc_questions = _hc_values.get("clarification_questions", [])
                                _hc_assumptions = _hc_values.get("clarification_assumptions", [])
                                _hc_require_answer = (_hc_mode == "STRICT" and _hc_round <= 1)
                                if _resume_run:
                                    await mark_waiting_human(db, _resume_run, step_name="human_clarification", thread_id=thread_id)
                                    interrupt = await create_interrupt(
                                        db,
                                        run=_resume_run,
                                        thread_id=thread_id,
                                        step_name="human_clarification",
                                        payload={
                                            "type": "clarification_questions",
                                            "questions": _hc_questions,
                                            "round": _hc_round,
                                            "max_rounds": _hc_max_rounds,
                                            "assumptions_if_skipped": _hc_assumptions,
                                            "pre_generation_mode": _hc_mode,
                                            "require_answer": _hc_require_answer,
                                        },
                                    )
                                    await db.commit()
                                else:
                                    interrupt = None
                                yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                                _clar_payload = {
                                    "run_id": _resume_run_id,
                                    "interrupt_id": str(interrupt.id) if interrupt else None,
                                    "thread_id": thread_id,
                                    "round": _hc_round,
                                    "max_rounds": _hc_max_rounds,
                                    "questions": _hc_questions,
                                    "assumptions_if_skipped": _hc_assumptions,
                                    "pre_generation_mode": _hc_mode,
                                    "require_answer": _hc_require_answer,
                                }
                                yield f"event: clarification_required\ndata: {json.dumps(_clar_payload, ensure_ascii=False)}\n\n"
                                return

                        elif node_name == "chapter_architect":
                            card = output.get("chapter_task_card", {}) if isinstance(output, dict) else {}
                            yield f"event: architect_output\ndata: {json.dumps({'task_card': card}, ensure_ascii=False)}\n\n"
                            _tc_state = await app.aget_state(config)
                            _tc_next = _tc_state.next if _tc_state else ()
                            if "task_card_review" in _tc_next:
                                _tc_values = _tc_state.values if _tc_state else {}
                                _tc_mode = _tc_values.get("pre_generation_mode", "PLANNING")
                                _embedded_payload = _embedded_clarification_payload(_tc_values)
                                _clarification_status = _task_card_clarification_status(_tc_values)
                                if _tc_values.get("task_card_refresh_requested"):
                                    # workflow_v2 的路由依据该 flag 选择 architect；
                                    # architect 完成后清掉它，确保下一次确认进入 writer。
                                    await app.aupdate_state(
                                        config,
                                        {"task_card_refresh_requested": False, "task_card_reviewed": False},
                                        as_node="chapter_architect",
                                    )
                                if _resume_run:
                                    await mark_waiting_human(db, _resume_run, step_name="task_card_review", thread_id=thread_id)
                                _task_card_payload = {
                                    "task_card": card,
                                    "clarification": _embedded_payload,
                                    "clarification_status": _clarification_status,
                                    "context_summary": _tc_values.get("context_summary", {}),
                                }
                                interrupt = await create_interrupt(
                                    db,
                                    run=_resume_run,
                                    thread_id=thread_id,
                                    step_name="task_card_review",
                                    payload=_task_card_payload,
                                )
                                await db.commit()
                                yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                                yield f"event: task_card_review_required\ndata: {json.dumps({'task_card': card, 'thread_id': thread_id, 'pre_generation_mode': _tc_mode, 'clarification': _embedded_payload, 'clarification_status': _clarification_status, 'context_summary': _tc_values.get('context_summary', {})}, ensure_ascii=False)}\n\n"
                                return

                yield f"event: done\ndata: {json.dumps({'message': '规划阶段已完成'}, ensure_ascii=False)}\n\n"

            if action == "reject":
                # ── Harness E: reject 时解析 interrupt ──
                if _resume_run:
                    interrupt = await get_interrupt_by_thread(db, thread_id)
                    if interrupt and not interrupt.resolved:
                        await resolve_interrupt(db, interrupt, decision=InterruptDecision.REJECT, feedback=resume_feedback)
                        await db.commit()
                yield f"event: done\ndata: {json.dumps({'message': '已拒绝，流程终止'}, ensure_ascii=False)}\n\n"
                return

            if action == "refresh_task_card_context":
                raw_keys = body_data.get("excluded_context_keys", [])
                if not isinstance(raw_keys, list) or any(not isinstance(value, str) for value in raw_keys):
                    yield f"event: error\ndata: {json.dumps({'message': 'excluded_context_keys 必须是字符串数组'}, ensure_ascii=False)}\n\n"
                    return
                excluded_keys = sorted({value.strip() for value in raw_keys if value.strip()})
                if len(excluded_keys) > 200:
                    yield f"event: error\ndata: {json.dumps({'message': '上下文排除项不能超过 200 条'}, ensure_ascii=False)}\n\n"
                    return

                await app.aupdate_state(config, {
                    "excluded_context_keys": excluded_keys,
                    "context_refresh_requested": True,
                    "task_card_refresh_requested": False,
                    "task_card_reviewed": False,
                }, as_node="task_card_review")
                if _resume_run:
                    await mark_running(db, _resume_run)
                    await db.commit()
                async for chunk in _stream_planning_resume():
                    yield chunk
                return

            # ── L-1: reject_task_card — 用户取消任务卡预览 ──
            if action == "refresh_task_card":
                # L-2：任务卡上的澄清回答只允许从 task_card_review 回环到 architect。
                current = state.values
                raw_answers = body_data.get("clarification_answers", {})
                user_note_from_body = str(body_data.get("user_note", "") or "").strip()
                if not isinstance(raw_answers, dict):
                    yield f"event: error\ndata: {json.dumps({'message': 'clarification_answers 必须是 JSON 对象'}, ensure_ascii=False)}\n\n"
                    return

                answers = {
                    str(key): str(value).strip()
                    for key, value in raw_answers.items()
                    if value is not None and str(value).strip()
                }
                if not answers and not user_note_from_body:
                    yield f"event: error\ndata: {json.dumps({'message': '请至少回答一个澄清问题或填写补充要求'}, ensure_ascii=False)}\n\n"
                    return

                current_round = int(current.get("clarification_round", 0) or 0)
                max_rounds = max(1, int(current.get("max_clarification_rounds", 3) or 3))
                if current_round >= max_rounds:
                    yield f"event: error\ndata: {json.dumps({'message': f'澄清已达到最大轮数 {max_rounds}，请直接确认当前任务卡'}, ensure_ascii=False)}\n\n"
                    return

                merged_answers = dict(current.get("clarification_answers", {}) or {})
                merged_answers.update(answers)
                from agents.clarification import build_clarification_summary

                summary = build_clarification_summary(
                    current.get("clarification_questions", []) or [],
                    merged_answers,
                )
                updated_note = _append_note_once(
                    current.get("user_note", "") or "",
                    f"[用户补充] {user_note_from_body}" if user_note_from_body else "",
                )
                update_state = {
                    "clarification_answers": merged_answers,
                    "clarification_summary": _append_note_once(
                        current.get("clarification_summary", "") or "", summary,
                    ),
                    "clarification_round": current_round + 1,
                    "user_note": updated_note,
                    "requirements_complete": True,
                    "needs_clarification": False,
                    "clarification_skipped": False,
                    "task_card_reviewed": False,
                    "task_card_refresh_requested": True,
                }
                await app.aupdate_state(config, update_state, as_node="task_card_review")

                if _resume_run:
                    await mark_running(db, _resume_run)
                    await db.commit()
                # 故意不 resolve task_card_review interrupt：后续 architect 再次停在同一
                # 审核点时，create_interrupt 会复用这条未解决记录并替换 payload。
                async for chunk in _stream_planning_resume():
                    yield chunk
                return

            if action == "reject_task_card":
                if _resume_run:
                    interrupt = await get_unresolved_interrupt_by_thread_step(db, thread_id, "task_card_review")
                    if interrupt and not interrupt.resolved:
                        await resolve_interrupt(db, interrupt, decision=InterruptDecision.REJECT, feedback="用户取消任务卡预览")
                    await mark_cancelled(db, _resume_run)
                    await db.commit()
                yield f"event: done\ndata: {json.dumps({'message': '已取消任务卡预览'}, ensure_ascii=False)}\n\n"
                return

            # ── M-3: submit_clarification — 用户回答澄清后继续 graph ──
            if action == "submit_clarification":
                current = state.values
                answers = body_data.get("clarification_answers", {}) if body_data else {}
                user_note_from_body = (body_data.get("user_note", "") if body_data else "") or ""
                if not isinstance(answers, dict):
                    answers = {}

                mode = current.get("pre_generation_mode", "PLANNING")
                current_round = int(current.get("clarification_round", 1) or 1)
                if mode == "STRICT" and current_round <= 1 and not answers and not user_note_from_body:
                    yield f"event: error\ndata: {json.dumps({'message': '严格模式首轮请至少回答一个问题或填写补充要求'}, ensure_ascii=False)}\n\n"
                    return

                prev_answers = dict(current.get("clarification_answers", {}) or {})
                prev_answers.update({str(k): str(v) for k, v in answers.items() if v is not None})

                from agents.clarification import build_clarification_summary
                questions = current.get("clarification_questions", []) or []
                summary = build_clarification_summary(questions, prev_answers)
                updated_summary = _append_note_once(current.get("clarification_summary", "") or "", summary)
                updated_note = _append_note_once(
                    current.get("user_note", "") or "",
                    f"[用户补充] {user_note_from_body}" if user_note_from_body else "",
                )

                await app.aupdate_state(config, {
                    "clarification_answers": prev_answers,
                    "clarification_summary": updated_summary,
                    "user_note": updated_note,
                    "requirements_complete": False,
                    "needs_clarification": False,
                    "clarification_skipped": False,
                }, as_node="human_clarification")

                if _resume_run:
                    interrupt = await get_interrupt_by_thread(db, thread_id)
                    if interrupt and not interrupt.resolved:
                        await resolve_interrupt(db, interrupt, decision=InterruptDecision.SUBMIT_CLARIFICATION, feedback=summary)
                        await db.commit()

                async for chunk in _stream_planning_resume():
                    yield chunk
                return

            # ── M-3: skip_clarification — 非 STRICT 首轮可跳过澄清，继续进入任务卡 ──
            if action == "skip_clarification":
                current = state.values
                mode = current.get("pre_generation_mode", "PLANNING")
                current_round = int(current.get("clarification_round", 1) or 1)
                if mode == "STRICT" and current_round <= 1:
                    yield f"event: error\ndata: {json.dumps({'message': '严格模式首轮需要先回答澄清问题'}, ensure_ascii=False)}\n\n"
                    return

                await app.aupdate_state(config, {
                    "requirements_complete": True,
                    "needs_clarification": False,
                    "clarification_skipped": True,
                    "clarification_questions": [],
                }, as_node="human_clarification")

                if _resume_run:
                    interrupt = await get_interrupt_by_thread(db, thread_id)
                    if interrupt and not interrupt.resolved:
                        await resolve_interrupt(db, interrupt, decision=InterruptDecision.SKIP_CLARIFICATION, feedback="用户跳过生成前澄清")
                        await db.commit()

                async for chunk in _stream_planning_resume():
                    yield chunk
                return

            # ── L-1: approve_task_card — 用户确认/修改任务卡后继续执行 ──
            if action == "approve_task_card":
                current_values = state.values
                logger.info(
                    "task_card approve requested: thread_id=%s current_next=%s",
                    thread_id,
                    tuple(state.next or ()),
                )
                modified_card = None
                if resume_task_card:
                    try:
                        modified_card = (
                            json.loads(resume_task_card)
                            if isinstance(resume_task_card, str)
                            else resume_task_card
                        )
                    except (TypeError, json.JSONDecodeError):
                        yield f"event: error\ndata: {json.dumps({'message': 'task_card JSON 解析失败'}, ensure_ascii=False)}\n\n"
                        return
                    if not isinstance(modified_card, dict):
                        yield f"event: error\ndata: {json.dumps({'message': 'task_card 必须是 JSON 对象'}, ensure_ascii=False)}\n\n"
                        return

                update_state: dict = {
                    "task_card_reviewed": True,
                    "task_card_refresh_requested": False,
                    "context_refresh_requested": False,
                }
                if modified_card:
                    update_state["modified_task_card"] = modified_card
                    update_state["chapter_task_card"] = modified_card  # 覆盖 architect 输出

                # 从 body_data 读取用户补充要求
                user_note = (body_data.get("user_note", "") or "").strip()
                if user_note:
                    update_state["user_note"] = _append_note_once(
                        current_values.get("user_note", "") or "",
                        f"[用户补充] {user_note}",
                    )

                try:
                    await asyncio.wait_for(
                        app.aupdate_state(config, update_state, as_node="task_card_review"),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "task_card approve aupdate_state timed out: thread_id=%s current_next=%s",
                        thread_id,
                        tuple(state.next or ()),
                    )
                    yield f"event: error\ndata: {json.dumps({'message': '任务卡确认状态写入超时，请刷新后重试'}, ensure_ascii=False)}\n\n"
                    return
                post_state = await app.aget_state(config)
                post_next = tuple(post_state.next or ()) if post_state else ()
                logger.info(
                    "task_card approve resume checkpoint: thread_id=%s next=%s",
                    thread_id,
                    post_next,
                )
                yield f"event: progress\ndata: {json.dumps({'message': '任务卡已确认，开始创作正文'}, ensure_ascii=False)}\n\n"
                if not post_next:
                    await _discard_resume_run("任务卡确认后没有可继续执行的节点")
                    yield f"event: error\ndata: {json.dumps({'message': '任务卡已确认，但工作流没有可继续执行的节点，请重新生成'}, ensure_ascii=False)}\n\n"
                    return
                if "chapter_writer" not in post_next:
                    await _discard_resume_run("任务卡确认后下一步不是正文创作节点")
                    yield f"event: error\ndata: {json.dumps({'message': f'任务卡已确认，但下一步不是正文创作节点：{list(post_next)}'}, ensure_ascii=False)}\n\n"
                    return

                # 解析 interrupt
                if _resume_run:
                    interrupt = await get_unresolved_interrupt_by_thread_step(db, thread_id, "task_card_review")
                    if interrupt and not interrupt.resolved:
                        await resolve_interrupt(db, interrupt, decision=InterruptDecision.APPROVE,
                                                feedback=f"任务卡已确认{', 已修改' if modified_card else ''}")
                        await db.commit()

                # 继续执行（从 chapter_writer 开始）
                if _resume_run:
                    await mark_running(db, _resume_run)
                    await db.commit()
                    yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"

                resumed_content = ""
                resume_skill_packs = list(current_values.get("skill_packs") or [])
                seen_skill_pack_keys = {
                    (str(pack.get("expert", "")), str(pack.get("skill_dir", "")))
                    for pack in resume_skill_packs
                    if isinstance(pack, dict)
                }

                resume_stream = app.astream_events(None, config=config, version="v2")
                resume_iter = resume_stream.__aiter__()
                while True:
                    wait_started_at = asyncio.get_running_loop().time()
                    while True:
                        try:
                            event = await asyncio.wait_for(resume_iter.__anext__(), timeout=25)
                            break
                        except StopAsyncIteration:
                            event = None
                            break
                        except asyncio.TimeoutError:
                            elapsed = asyncio.get_running_loop().time() - wait_started_at
                            if elapsed >= 180:
                                logger.warning(
                                    "task_card approve resume timed out waiting for workflow event: thread_id=%s next=%s",
                                    thread_id,
                                    post_next,
                                )
                                aclose = getattr(resume_stream, "aclose", None)
                                if aclose:
                                    await aclose()
                                await _discard_resume_run("正文创作阶段超时")
                                yield f"event: error\ndata: {json.dumps({'message': '正文创作节点超过 180 秒没有响应，请稍后重试或检查模型服务'}, ensure_ascii=False)}\n\n"
                                return
                            yield f"event: progress\ndata: {json.dumps({'message': '正文创作/审稿仍在执行，请稍候'}, ensure_ascii=False)}\n\n"
                            if await request.is_disconnected():
                                logger.info("客户端已断开连接，取消恢复生成")
                                await _discard_resume_run("客户端断开连接")
                                return
                    if event is None:
                        break
                    if await request.is_disconnected():
                        logger.info("客户端已断开连接，取消恢复生成")
                        await _discard_resume_run("客户端断开连接")
                        return
                    kind = event.get("event")
                    if kind == "on_chain_start":
                        node_name = event.get("name", "")
                        if node_name:
                            yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                            if node_name == "chapter_writer":
                                yield f"event: progress\ndata: {json.dumps({'message': '写手正在生成正文，通常需要 1-3 分钟'}, ensure_ascii=False)}\n\n"
                    elif kind == "on_chain_end":
                        node_name = event.get("name", "")
                        output = event.get("data", {}).get("output", {})
                        if node_name:
                            yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                        for pack in _new_skill_packs(output, seen_skill_pack_keys):
                            resume_skill_packs.append(pack)
                            yield _skill_pack_sse_event(pack, fallback_expert=node_name)

                        if node_name in {"writer", "chapter_writer"}:
                            draft = output.get("draft", "") if isinstance(output, dict) else ""
                            if draft:
                                resumed_content = draft
                            writer_payload = {"content": draft}
                            initial_draft = output.get("writer_draft", "") if isinstance(output, dict) else ""
                            if initial_draft:
                                writer_payload["initial_draft"] = initial_draft
                            yield f"event: writer_output\ndata: {json.dumps(writer_payload, ensure_ascii=False)}\n\n"
                        elif node_name in {"critic", "structural_critic"}:
                            critiques = output.get("critiques", []) if isinstance(output, dict) else []
                            yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"
                        elif node_name in {"consistency_checker", "continuity_checker"}:
                            guardrail = output.get("consistency_report", {}) if isinstance(output, dict) else {}
                            if not isinstance(guardrail, dict):
                                guardrail = {}
                            report_text = _guardrail_to_text(guardrail)
                            yield f"event: consistency_check\ndata: {json.dumps({'report': report_text, 'guardrail_result': guardrail}, ensure_ascii=False)}\n\n"
                        elif node_name in {"editor", "narrative_editor"}:
                            edited = ""
                            if isinstance(output, dict):
                                edited = output.get("edited_draft", "") or output.get("draft", "")
                            if edited:
                                resumed_content = edited
                            yield f"event: editor_output\ndata: {json.dumps({'content': edited}, ensure_ascii=False)}\n\n"
                        elif node_name == "human_review":
                            record_id = await _save_resume_generation_history(
                                resumed_content,
                                {**current_values, **update_state, "skill_packs": resume_skill_packs},
                            )
                            if _resume_run:
                                await mark_waiting_human(db, _resume_run, step_name="human_review", thread_id=thread_id)
                                await db.commit()
                                yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                            yield _generation_record_event(record_id)
                            yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                            return

                # 流程自然结束（无 human_review 中断）
                workflow_state = await app.aget_state(config)
                next_nodes = workflow_state.next if workflow_state else []
                if "human_review" in next_nodes:
                    record_id = await _save_resume_generation_history(
                        resumed_content,
                        {**current_values, **update_state, "skill_packs": resume_skill_packs},
                    )
                    if _resume_run:
                        await mark_waiting_human(db, _resume_run, step_name="human_review", thread_id=thread_id)
                        await db.commit()
                        yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
                    yield _generation_record_event(record_id)
                    yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                    return

                record_id = await _save_resume_generation_history(
                    resumed_content,
                    {**current_values, **update_state, "skill_packs": resume_skill_packs},
                )
                if _resume_run:
                    await mark_completed(db, _resume_run)
                    await db.commit()
                    yield f"event: run_status\ndata: {json.dumps({'run_id': _resume_run_id, 'status': 'COMPLETED'}, ensure_ascii=False)}\n\n"
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '任务卡确认，生成完成'}, ensure_ascii=False)}\n\n"
                return

            if action == "review":
                current_values = state.values
                revision_count = current_values.get("revision_count", 0)
                if revision_count >= 3:
                    yield f"event: error\ndata: {json.dumps({'message': '已达到3次修改上限，请重新生成章节'}, ensure_ascii=False)}\n\n"
                    return

                candidate = current_values.get("edited_draft", "") or current_values.get("draft", "")
                if not candidate:
                    yield f"event: error\ndata: {json.dumps({'message': '没有可审核的生成内容'}, ensure_ascii=False)}\n\n"
                    return

                llm = get_llm_provider(current_values.get("llm_config"))
                result = await llm.generate(
                    (
                        "你是一位严谨的小说审稿编辑。请审核候选章节，给出3个可执行的修改方向。"
                        "方向必须聚焦当前候选稿的改进，例如节奏、冲突、人物动机、情绪层次、场景细节、设定一致性；"
                        "不要要求续写后续剧情。只输出JSON字符串数组。"
                    ),
                    (
                        f"## 当前候选稿\n{candidate}\n\n"
                        f"## 已有审校意见\n{chr(10).join(current_values.get('critiques', [])) or '无'}\n\n"
                        f"## 一致性检查\n{_guardrail_to_text(current_values.get('consistency_report', {})) or '无'}"
                    ),
                    temperature=0.3,
                    max_tokens=1024,
                )
                directions = _parse_directions(result)[:3]
                yield (
                    "event: revision_suggestions\n"
                    f"data: {json.dumps({'directions': directions, 'revision_count': revision_count, 'max_revisions': 3}, ensure_ascii=False)}\n\n"
                )
                yield f"event: done\ndata: {json.dumps({'message': '请选择修改方向'}, ensure_ascii=False)}\n\n"
                return

            if action == "revise":
                # 更新状态，增加修订计数并注入反馈
                current_values = state.values
                current_revision_count = current_values.get("revision_count", 0)
                if current_revision_count >= 3:
                    yield f"event: error\ndata: {json.dumps({'message': '已达到3次修改上限，请重新生成章节'}, ensure_ascii=False)}\n\n"
                    return
                revision_count = current_revision_count + 1

                update_state = {
                    "revision_count": revision_count,
                }
                if resume_feedback:
                    update_state["critiques"] = [f"[用户选择的修改方向] {resume_feedback}"]

                await app.aupdate_state(config, update_state, as_node="human_review")

                revised_content = ""
                resume_skill_packs = list(current_values.get("skill_packs") or [])
                seen_skill_pack_keys = {
                    (str(pack.get("expert", "")), str(pack.get("skill_dir", "")))
                    for pack in resume_skill_packs
                    if isinstance(pack, dict)
                }

                # 继续流式执行
                async for event in app.astream_events(None, config=config, version="v2"):
                    if await request.is_disconnected():
                        logger.info("客户端已断开连接，取消恢复生成")
                        await _discard_resume_run("客户端断开连接")
                        return
                    kind = event.get("event")
                    if kind == "on_chain_start":
                        node_name = event.get("name", "")
                        if node_name:
                            yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
                    elif kind == "on_chain_end":
                        node_name = event.get("name", "")
                        output = event.get("data", {}).get("output", {})
                        if node_name:
                            yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                        for pack in _new_skill_packs(output, seen_skill_pack_keys):
                            resume_skill_packs.append(pack)
                            yield _skill_pack_sse_event(pack, fallback_expert=node_name)

                        if node_name in {"writer", "chapter_writer"}:
                            draft = output.get("draft", "") if isinstance(output, dict) else ""
                            if draft:
                                revised_content = draft
                            writer_payload = {"content": draft}
                            initial_draft = output.get("writer_draft", "") if isinstance(output, dict) else ""
                            if initial_draft:
                                writer_payload["initial_draft"] = initial_draft
                            yield f"event: writer_output\ndata: {json.dumps(writer_payload, ensure_ascii=False)}\n\n"
                        elif node_name in {"critic", "structural_critic"}:
                            critiques = output.get("critiques", []) if isinstance(output, dict) else []
                            yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"
                        elif node_name in {"consistency_checker", "continuity_checker"}:
                            guardrail = output.get("consistency_report", {}) if isinstance(output, dict) else {}
                            if not isinstance(guardrail, dict):
                                guardrail = {}
                            report_text = _guardrail_to_text(guardrail)
                            yield f"event: consistency_check\ndata: {json.dumps({'report': report_text, 'guardrail_result': guardrail}, ensure_ascii=False)}\n\n"
                        elif node_name in {"editor", "narrative_editor"}:
                            edited = ""
                            if isinstance(output, dict):
                                edited = output.get("edited_draft", "") or output.get("draft", "")
                            if edited:
                                revised_content = edited
                            yield f"event: editor_output\ndata: {json.dumps({'content': edited}, ensure_ascii=False)}\n\n"
                        elif node_name == "human_review":
                            record_id = await _save_resume_generation_history(
                                revised_content,
                                {**current_values, **update_state, "skill_packs": resume_skill_packs},
                            )
                            yield _generation_record_event(record_id)
                            yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                            return

                workflow_state = await app.aget_state(config)
                next_nodes = workflow_state.next if workflow_state else []
                if "human_review" in next_nodes:
                    record_id = await _save_resume_generation_history(
                        revised_content,
                        {**current_values, **update_state, "skill_packs": resume_skill_packs},
                    )
                    yield _generation_record_event(record_id)
                    yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                    return

                record_id = await _save_resume_generation_history(
                    revised_content,
                    {**current_values, **update_state, "skill_packs": resume_skill_packs},
                )
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '修订完成'}, ensure_ascii=False)}\n\n"
                return

            # approve: 恢复执行到结束
            await app.aupdate_state(config, {}, as_node="human_review")

            current_values = state.values
            # 优先使用 edited_draft（经编辑润色），若无则使用 draft（原始创作）
            raw_content = current_values.get("edited_draft", "") or current_values.get("draft", "")
            target_content_id = current_values.get("chapter_id", "")
            content_written = False
            memory_extraction_payload: dict | None = None

            # 落库：小说写 Chapter，文章写 Document。这里故意不提前 commit：正文、
            # 版本、GenerationRecord 状态和 AiRun 完成状态必须作为一个原子事务提交。
            if raw_content and target_content_id:
                try:
                    content_id = _to_uuid(target_content_id)
                    if project.mode == "article":
                        doc_result = await db.execute(
                            select(Document).where(Document.id == content_id, Document.project_id == uid)
                        )
                        document = doc_result.scalar_one_or_none()
                        if document:
                            await save_document_content(db, document, raw_content, source="ai_approve", set_status="draft")
                            content_written = True
                    else:
                        ch_result = await db.execute(
                            select(Chapter).where(Chapter.id == content_id, Chapter.project_id == uid)
                        )
                        chapter = ch_result.scalar_one_or_none()
                        if chapter:
                            await save_chapter_content(
                                db, chapter, raw_content, source="ai_approve",
                                set_status="draft", run_id=_resume_run_id,
                            )
                            content_written = True
                            # ── Phase F: 联动 GenerationRecord.accepted_version_id ──
                            _ver = None
                            _gr = None
                            if _resume_run_id:
                                _gr = (
                                    await db.execute(
                                        select(GenerationRecord).where(
                                            GenerationRecord.run_id == _to_uuid(_resume_run_id)
                                        )
                                    )
                                ).scalar_one_or_none()
                                if _gr:
                                    # 只查本次 Run 创建的版本，避免正文未变化时误绑定旧版本。
                                    _ver = (
                                        await db.execute(
                                            select(ChapterVersion)
                                            .where(
                                                ChapterVersion.chapter_id == chapter.id,
                                                ChapterVersion.run_id == _to_uuid(_resume_run_id),
                                            )
                                            .order_by(ChapterVersion.version_number.desc())
                                            .limit(1)
                                        )
                                    ).scalar_one_or_none()
                                    if _ver:
                                        await update_generation_record_status(
                                            db, _gr, "applied",
                                            accepted_version_id=str(_ver.id),
                                        )
                            # ── Phase I-6 / H2: 后台抽取写作记忆 ──
                            # v2 run（有 workflow_key）用 story-recorder + memory-curator
                            # 旧 run / 无 workflow_key 用旧 FactExtractionAgent
                            # SQLite :memory: 测试环境跳过（独立 session 看不到内存表）
                            from db.session import get_engine as _get_engine_for_guard
                            _is_sqlite = _get_engine_for_guard().dialect.name == "sqlite"
                            if _resume_run_id and not _is_sqlite:
                                _accepted_vid = (
                                    str(_gr.accepted_version_id) if _gr and _gr.accepted_version_id
                                    else (str(_ver.id) if _ver else None)
                                )
                                if _accepted_vid:
                                    memory_extraction_payload = {
                                        "use_story_recorder": bool(_resume_run and _resume_run.workflow_key),
                                        "project_id": str(uid),
                                        "chapter_id": str(chapter.id),
                                        "chapter_version_id": _accepted_vid,
                                        "chapter_sequence_number": chapter.sequence_number,
                                        "run_id": _resume_run_id,
                                        "content": raw_content,
                                        "context": current_values.get("context", ""),
                                        "llm_config": current_values.get("llm_config"),
                                    }
                except Exception:
                    logger.exception("落库失败")
                    await _discard_resume_run("正式内容保存失败")
                    yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                    return

            # 只有正文/版本、审计关联和运行状态都准备就绪，才一次性提交。
            if _resume_run:
                interrupt = await get_unresolved_interrupt_by_thread_step(db, thread_id, "human_review")
                if interrupt and not interrupt.resolved:
                    await resolve_interrupt(db, interrupt, decision=InterruptDecision.APPROVE, feedback=resume_feedback)
                await mark_completed(db, _resume_run)
                await db.commit()
            elif content_written:
                await db.commit()

            # commit 成功后再启动独立 session 的记忆抽取，避免后台任务读取到未提交版本。
            if memory_extraction_payload:
                if memory_extraction_payload["use_story_recorder"]:
                    asyncio.create_task(run_story_recorder_extraction(
                        project_id=memory_extraction_payload["project_id"],
                        chapter_id=memory_extraction_payload["chapter_id"],
                        chapter_version_id=memory_extraction_payload["chapter_version_id"],
                        chapter_sequence_number=memory_extraction_payload["chapter_sequence_number"],
                        run_id=memory_extraction_payload["run_id"],
                        content=memory_extraction_payload["content"],
                        context=memory_extraction_payload["context"],
                        llm_config=memory_extraction_payload["llm_config"],
                    ))
                else:
                    asyncio.create_task(run_fact_extraction(
                        project_id=memory_extraction_payload["project_id"],
                        chapter_id=memory_extraction_payload["chapter_id"],
                        chapter_version_id=memory_extraction_payload["chapter_version_id"],
                        chapter_sequence_number=memory_extraction_payload["chapter_sequence_number"],
                        run_id=memory_extraction_payload["run_id"],
                        content=memory_extraction_payload["content"],
                        context=memory_extraction_payload["context"],
                        llm_config=memory_extraction_payload["llm_config"],
                    ))

            yield f"event: done\ndata: {json.dumps({'message': '已批准，内容已保存'}, ensure_ascii=False)}\n\n"

        except WorkflowGenerationError as e:
            logger.warning("恢复时关键生成节点失败: %s", e)
            await _discard_incomplete_generation_run(
                db,
                run_id=_resume_run_id,
                thread_id=thread_id,
                reason=str(e),
            )
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("恢复工作流失败")
            await _discard_incomplete_generation_run(
                db,
                run_id=_resume_run_id,
                thread_id=thread_id,
                reason=str(e),
            )
            yield f"event: error\ndata: {json.dumps({'message': f'恢复失败: {str(e)}'}, ensure_ascii=False)}\n\n"
        finally:
            finish_langfuse_context(langfuse_context)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ==================== 章节版本 ====================

@router.get("/projects/{project_id}/chapters/{sequence_number}/versions", response_model=list[ChapterVersionListItemResponse])
async def list_chapter_versions(
    project_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    ch_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = ch_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    result = await db.execute(
        select(
            ChapterVersion.id,
            ChapterVersion.chapter_id,
            ChapterVersion.word_count,
            ChapterVersion.version_number,
            ChapterVersion.source,
            ChapterVersion.run_id,
            ChapterVersion.parent_version_id,
            ChapterVersion.rollback_from_version_id,
            ChapterVersion.created_at,
        )
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.version_number.desc())
    )
    return [
        ChapterVersionListItemResponse(
            id=row[0],
            chapter_id=row[1],
            word_count=row[2],
            version_number=row[3],
            source=row[4],
            run_id=row[5],
            parent_version_id=row[6],
            rollback_from_version_id=row[7],
            created_at=row[8],
        )
        for row in result.all()
    ]


@router.get("/projects/{project_id}/chapters/{sequence_number}/versions/{version_id}", response_model=ChapterVersionResponse)
async def get_chapter_version(
    project_id: str,
    sequence_number: int,
    version_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    ch_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = ch_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    vid = _to_uuid(version_id)
    result = await db.execute(
        select(ChapterVersion).where(ChapterVersion.id == vid, ChapterVersion.chapter_id == chapter.id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")
    return version


@router.post("/projects/{project_id}/chapters/{sequence_number}/versions/{version_id}/restore", response_model=ChapterResponse)
async def restore_chapter_version(
    project_id: str,
    sequence_number: int,
    version_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """回滚章节到指定版本。

    与文档 restore 一致：不覆盖旧版本，而是用旧版本内容创建一个新版本（source=rollback），
    并在 rollback_from_version_id 记录回滚来源。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    ch_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = ch_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    vid = _to_uuid(version_id)
    result = await db.execute(
        select(ChapterVersion).where(ChapterVersion.id == vid, ChapterVersion.chapter_id == chapter.id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")

    await save_chapter_content(
        db, chapter, version.content or "", source="rollback",
        rollback_from_version_id=str(version.id),
    )
    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.post("/projects/{project_id}/chapters/{sequence_number}/versions/diff", response_model=ChapterVersionDiffResponse)
async def diff_chapter_versions(
    project_id: str,
    sequence_number: int,
    req: ChapterVersionDiffRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    ch_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = ch_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    result_a = await db.execute(
        select(ChapterVersion).where(ChapterVersion.id == req.version_id_a, ChapterVersion.chapter_id == chapter.id)
    )
    ver_a = result_a.scalar_one_or_none()
    if not ver_a:
        raise HTTPException(status_code=404, detail="版本 A 不存在")

    if req.current_content is not None:
        diff = compute_diff(ver_a.content or "", req.current_content)
        return ChapterVersionDiffResponse(
            version_a=ver_a.version_number,
            version_b=0,
            diff=diff,
        )

    result_b = await db.execute(
        select(ChapterVersion).where(ChapterVersion.id == req.version_id_b, ChapterVersion.chapter_id == chapter.id)
    )
    ver_b = result_b.scalar_one_or_none()
    if not ver_b:
        raise HTTPException(status_code=404, detail="版本 B 不存在")

    diff = compute_diff(ver_a.content or "", ver_b.content or "")
    return ChapterVersionDiffResponse(
        version_a=ver_a.version_number,
        version_b=ver_b.version_number,
        diff=diff,
    )


# ==================== AI 生成历史 ====================

@router.get("/projects/{project_id}/chapters/{sequence_number}/generations", response_model=list[GenerationRecordListItemResponse])
async def list_chapter_generations(
    project_id: str,
    sequence_number: int,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    project = await _verify_project_owner(uid, user.id, db)
    if project.mode == "article":
        raise HTTPException(status_code=400, detail="文章项目请使用文档生成历史接口")

    result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    records = await list_generation_records_for_chapter(db, project_id=uid, chapter_id=chapter.id)
    return [_generation_list_item(record) for record in records]


@router.get("/projects/{project_id}/generations/{generation_id}", response_model=GenerationRecordResponse)
async def get_generation_history_record(
    project_id: str,
    generation_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    record = await get_generation_record(db, project_id=uid, record_id=_to_uuid(generation_id))
    if not record:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    return record


@router.patch("/projects/{project_id}/generations/{generation_id}", response_model=GenerationRecordResponse)
async def update_generation_history_record(
    project_id: str,
    generation_id: str,
    req: GenerationRecordUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    record = await get_generation_record(db, project_id=uid, record_id=_to_uuid(generation_id))
    if not record:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    await update_generation_record_status(db, record, req.status)
    await db.commit()
    await db.refresh(record)
    return record


@router.post("/projects/{project_id}/generations/{generation_id}/diff", response_model=GenerationRecordDiffResponse)
async def diff_generation_history_record(
    project_id: str,
    generation_id: str,
    req: GenerationRecordDiffRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    record = await get_generation_record(db, project_id=uid, record_id=_to_uuid(generation_id))
    if not record:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    return GenerationRecordDiffResponse(
        generation_id=record.id,
        diff=compute_diff(record.content or "", req.current_content),
    )


# ==================== 专家测试 ====================

@router.post("/projects/{project_id}/experts/{expert_id}/test")
async def test_expert(
    project_id: str,
    expert_id: str,
    req: ExpertTestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """测试专家 Agent：用一段文本试运行，看输出效果（纯 SSE，不落库）"""
    # Rate limiting
    agent_limiter.check(f"test:{user.id}")

    uid = _to_uuid(project_id)
    eid = _to_uuid(expert_id)
    result = await db.execute(
        select(Expert).where(Expert.id == eid, Expert.project_id == uid)
    )
    expert = result.scalar_one_or_none()
    if not expert:
        raise HTTPException(status_code=404, detail="专家不存在")

    provider = get_llm_provider(await get_user_llm_config(user.id, db))

    async def event_stream():
        try:
            yield f"event: agent_start\ndata: {json.dumps({'agent': expert.name, 'role': expert.role_type}, ensure_ascii=False)}\n\n"
            async for chunk in provider.generate_stream(expert.system_prompt, req.test_text):
                yield f"event: agent_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
            yield f"event: agent_done\ndata: {json.dumps({'agent': expert.name}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'message': '测试完成'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("测试失败")
            yield f"event: error\ndata: {json.dumps({'message': f'测试失败: {str(e)}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ==================== 导出 ====================

@router.get("/projects/{project_id}/export")
async def export_project(
    project_id: str,
    format: str = Query(default="txt", pattern=r"^(txt|md)$"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    project = await _verify_project_owner(uid, user.id, db)

    if project.mode == "article":
        result = await db.execute(
            select(Document)
            .where(Document.project_id == uid)
            .order_by(Document.position.asc())
        )
        items = result.scalars().all()
        if format == "md":
            lines = [f"# {project.title}"]
            for doc in items:
                lines.append(f"\n## {doc.position}. {doc.title}")
                if doc.content:
                    lines.append(doc.content)
            body = "\n".join(lines)
            media_type = "text/markdown; charset=utf-8"
        else:
            lines = [project.title]
            for doc in items:
                lines.append(f"\n{doc.position}. {doc.title}")
                if doc.content:
                    lines.append(doc.content)
            body = "\n".join(lines)
            media_type = "text/plain; charset=utf-8"
    else:
        ch_result = await db.execute(
            select(Chapter)
            .where(Chapter.project_id == uid)
            .order_by(Chapter.sequence_number.asc())
        )
        chapters = ch_result.scalars().all()

        if format == "md":
            lines = [f"# {project.title}"]
            for ch in chapters:
                lines.append(f"\n## 第{ch.sequence_number}章 {ch.title}")
                if ch.content:
                    lines.append(ch.content)
            body = "\n".join(lines)
            media_type = "text/markdown; charset=utf-8"
        else:
            lines = [project.title]
            for ch in chapters:
                lines.append(f"\n第{ch.sequence_number}章 {ch.title}")
                if ch.content:
                    lines.append(ch.content)
            body = "\n".join(lines)
            media_type = "text/plain; charset=utf-8"

    filename = f"{project.title}.{format}"
    encoded_filename = quote(filename)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


# ==================== Document（文章模式 API — 独立 Document 表） ====================


async def _verify_article_project(project_id: str, user: AuthUser, db: AsyncSession):
    """Verify project exists, belongs to user, and is article mode. Returns (uid, project)."""
    uid = _to_uuid(project_id)
    project = await _verify_project_owner(uid, user.id, db)
    if project.mode != "article":
        raise HTTPException(status_code=400, detail="Document API only available for article projects")
    return uid, project


@router.get("/projects/{project_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    result = await db.execute(
        select(Document).where(Document.project_id == uid).order_by(Document.position)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/documents", response_model=DocumentResponse)
async def create_document(
    project_id: str,
    req: DocumentCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)

    # Compute next position if not specified.
    position = req.position
    if position is None:
        result = await db.execute(
            select(func.coalesce(func.max(Document.position), 0)).where(Document.project_id == uid)
        )
        position = (result.scalar() or 0) + 1

    doc = Document(
        project_id=uid,
        title=req.title,
        position=position,
        status="draft",
        word_count=0,
    )
    db.add(doc)
    await db.flush()

    if "content" in req.model_fields_set:
        await save_document_content(db, doc, req.content or "", source="manual")

    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/projects/{project_id}/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    project_id: str,
    document_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@router.patch("/projects/{project_id}/documents/{document_id}", response_model=DocumentResponse)
async def update_document(
    project_id: str,
    document_id: str,
    req: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    if req.title is not None:
        doc.title = req.title
    if "content" in req.model_fields_set:
        await save_document_content(db, doc, req.content or "", source="manual")
    if req.status is not None:
        doc.status = req.status

    await db.commit()
    await db.refresh(doc)
    return doc


@router.delete("/projects/{project_id}/documents/{document_id}")
async def delete_document(
    project_id: str,
    document_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    await db.delete(doc)
    await db.commit()
    return {"ok": True}


# ==================== Document Version API ====================


@router.get("/projects/{project_id}/documents/{document_id}/versions", response_model=list[DocumentVersionListItemResponse])
async def list_document_versions(
    project_id: str,
    document_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="文档不存在")

    result = await db.execute(
        select(
            DocumentVersion.id,
            DocumentVersion.document_id,
            DocumentVersion.word_count,
            DocumentVersion.version_number,
            DocumentVersion.source,
            DocumentVersion.created_at,
        )
        .where(DocumentVersion.document_id == did)
        .order_by(DocumentVersion.version_number.desc())
    )
    return [
        DocumentVersionListItemResponse(
            id=row[0],
            document_id=row[1],
            word_count=row[2],
            version_number=row[3],
            source=row[4],
            created_at=row[5],
        )
        for row in result.all()
    ]


@router.get("/projects/{project_id}/documents/{document_id}/generations", response_model=list[GenerationRecordListItemResponse])
async def list_document_generations(
    project_id: str,
    document_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="文档不存在")

    records = await list_generation_records_for_document(db, project_id=uid, document_id=did)
    return [_generation_list_item(record) for record in records]


@router.get("/projects/{project_id}/documents/{document_id}/versions/{version_id}", response_model=DocumentVersionResponse)
async def get_document_version(
    project_id: str,
    document_id: str,
    version_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="文档不存在")

    vid = _to_uuid(version_id)
    result = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == vid, DocumentVersion.document_id == did)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")
    return version


@router.post("/projects/{project_id}/documents/{document_id}/versions/{version_id}/restore", response_model=DocumentResponse)
async def restore_document_version(
    project_id: str,
    document_id: str,
    version_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    vid = _to_uuid(version_id)
    result = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == vid, DocumentVersion.document_id == did)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")

    await save_document_content(db, doc, version.content or "", source="restore")
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/projects/{project_id}/documents/{document_id}/versions/diff", response_model=DocumentVersionDiffResponse)
async def diff_document_versions(
    project_id: str,
    document_id: str,
    req: DocumentVersionDiffRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid, _ = await _verify_article_project(project_id, user, db)
    did = _to_uuid(document_id)
    result = await db.execute(
        select(Document).where(Document.id == did, Document.project_id == uid)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="文档不存在")

    result_a = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == req.version_id_a, DocumentVersion.document_id == did)
    )
    ver_a = result_a.scalar_one_or_none()
    if not ver_a:
        raise HTTPException(status_code=404, detail="版本 A 不存在")

    if req.current_content is not None:
        diff = compute_diff(ver_a.content or "", req.current_content)
        return DocumentVersionDiffResponse(
            version_a=ver_a.version_number,
            version_b=0,
            diff=diff,
        )

    result_b = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == req.version_id_b, DocumentVersion.document_id == did)
    )
    ver_b = result_b.scalar_one_or_none()
    if not ver_b:
        raise HTTPException(status_code=404, detail="版本 B 不存在")

    diff = compute_diff(ver_a.content or "", ver_b.content or "")
    return DocumentVersionDiffResponse(
        version_a=ver_a.version_number,
        version_b=ver_b.version_number,
        diff=diff,
    )


# ═══════════════════════════════════════════════════════════════════
# 资料库 CRUD (Project Knowledge)
# ═══════════════════════════════════════════════════════════════════


@router.get("/projects/{project_id}/knowledge/sources", response_model=list[ProjectSourceListResponse])
async def list_knowledge_sources(
    project_id: str,
    source_type: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出项目的所有资料库条目。支持 source_type 过滤、q 关键词搜索、sort 排序。

    列表接口只返回 content_preview，不返回完整 content，避免资料很大时拖慢页面。
    点开详情再通过 sources/{source_id} 读取全文。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    preview_limit = 1200
    stmt = select(
        ProjectSource.id,
        ProjectSource.project_id,
        ProjectSource.title,
        ProjectSource.source_type,
        func.substr(ProjectSource.content, 1, preview_limit).label("content_preview"),
        func.length(ProjectSource.content).label("content_length"),
        ProjectSource.summary,
        ProjectSource.key_facts,
        ProjectSource.constraints,
        ProjectSource.characters,
        ProjectSource.keywords,
        ProjectSource.tags,
        ProjectSource.always_inject,
        ProjectSource.chunk_count,
        ProjectSource.token_count,
        ProjectSource.created_at,
        ProjectSource.updated_at,
    ).where(ProjectSource.project_id == uid)
    if source_type:
        stmt = stmt.where(ProjectSource.source_type == source_type)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            ProjectSource.title.ilike(pattern) | ProjectSource.content.ilike(pattern)
        )
    sort_map = {
        "created_at": ProjectSource.created_at.asc(),
        "-created_at": ProjectSource.created_at.desc(),
        "title": ProjectSource.title.asc(),
        "-title": ProjectSource.title.desc(),
        "updated_at": ProjectSource.updated_at.asc(),
        "-updated_at": ProjectSource.updated_at.desc(),
    }
    stmt = stmt.order_by(sort_map.get(sort, ProjectSource.created_at.desc()))
    result = await db.execute(stmt)
    items = []
    for row in result.mappings().all():
        content_preview = row["content_preview"] or ""
        content_length = row["content_length"] or 0
        items.append({
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "content_preview": content_preview,
            "content_truncated": content_length > preview_limit,
            "summary": row["summary"],
            "key_facts": row["key_facts"],
            "constraints": row["constraints"],
            "characters": row["characters"],
            "keywords": row["keywords"],
            "tags": row["tags"],
            "always_inject": row["always_inject"],
            "chunk_count": row["chunk_count"],
            "token_count": row["token_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return items


@router.post("/projects/{project_id}/knowledge/sources", response_model=ProjectSourceResponse)
async def create_knowledge_source(
    project_id: str,
    req: ProjectSourceCreate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """新建资料库条目。同人规则默认 always_inject=True。

    写入 ProjectSource 后会立即：
    1. chunk_and_save 生成检索切片；
    2. rebuild_source_facts 重建规则事实索引。

    这样用户上传资料后可以马上检索/问答，不需要手动点重建。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    always_inject = req.always_inject
    if req.source_type == "fanfic_rule" and not req.always_inject:
        always_inject = True

    # novel 类型：genre/canon_level 存入 metadata_，供抽取阶段读取。
    # 例如魔法幻想类资料会走对应的结构化抽取 schema/规则。
    metadata_ = req.metadata_ or {}
    if req.source_type == "novel":
        if req.genre:
            metadata_["genre"] = req.genre
        if req.canon_level:
            metadata_["canon_level"] = req.canon_level
        elif "canon_level" not in metadata_:
            metadata_["canon_level"] = "original"

    source = ProjectSource(
        project_id=uid,
        title=req.title,
        source_type=req.source_type,
        content=req.content,
        always_inject=always_inject,
        tags=req.tags,
        metadata_=metadata_ or None,
        token_count=len(req.content),
    )
    db.add(source)
    await db.commit()

    # 自动切片（短资料也能保底存一个 chunk）。
    # chunk 是后续 search/ask 的最小证据单位。
    from services.knowledge_source import chunk_and_save
    await chunk_and_save(db, uid, str(source.id))

    # 上传后自动重建事实索引，不让用户必须手动点 reindex。
    # 事实索引用于人物-能力等高频问题的本地短路回答。
    from services.knowledge_fact_index import rebuild_source_facts
    await rebuild_source_facts(db, uid, str(source.id))

    # 重新查询确保所有列（含 server_default 的 updated_at）被正确加载
    result = await db.execute(select(ProjectSource).where(ProjectSource.id == source.id))
    return result.scalar_one()


@router.post("/projects/{project_id}/knowledge/upload")
async def upload_knowledge_file(
    project_id: str,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    source_type: str = Form(default="upload"),
    tags: str = Form(default=""),
    always_inject: bool = Form(default=False),
    genre: str = Form(default=""),
    canon_level: str = Form(default="original"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """上传文件到资料库。文件内容读取后保存为可检索文本 + 自动切片。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    # source_type 白名单校验
    allowed_types = {"upload", "fanfic_rule", "timeline", "note", "reference", "novel"}
    if source_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"source_type 必须是 {allowed_types} 之一")

    # novel 类型：genre / canon_level 校验
    if source_type == "novel":
        allowed_genres = {"magic_fantasy", "historical"}
        if genre and genre not in allowed_genres:
            raise HTTPException(status_code=400, detail=f"genre 必须是 {allowed_genres} 之一")
        allowed_canons = {"manual", "fanfic", "original"}
        if canon_level and canon_level not in allowed_canons:
            raise HTTPException(status_code=400, detail=f"canon_level 必须是 {allowed_canons} 之一")

    filename = file.filename or "upload.txt"
    if not title.strip():
        title = filename

    raw = await file.read()
    if len(raw) > settings.KNOWLEDGE_UPLOAD_MAX_BYTES:
        limit_mb = settings.KNOWLEDGE_UPLOAD_MAX_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件大小不能超过 {limit_mb}MB")

    from services.knowledge_file_parser import extract_knowledge_text

    try:
        content, file_metadata = extract_knowledge_text(filename, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{exc}") from exc

    if source_type == "fanfic_rule":
        always_inject = True

    # metadata：file_metadata + content_type；novel 类型额外存 genre/canon_level
    source_metadata = {**file_metadata, "content_type": file.content_type}
    if source_type == "novel":
        source_metadata["genre"] = genre or "magic_fantasy"
        source_metadata["canon_level"] = canon_level or "original"

    source = ProjectSource(
        project_id=uid,
        title=title,
        source_type=source_type,
        content=content,
        always_inject=always_inject,
        tags=tags.split(",") if tags.strip() else None,
        token_count=len(content),
        metadata_=source_metadata,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)

    from services.knowledge_source import chunk_and_save
    await chunk_and_save(db, uid, str(source.id))

    # 上传后自动重建事实索引
    from services.knowledge_fact_index import rebuild_source_facts
    fact_result = await rebuild_source_facts(db, uid, str(source.id))

    return {
        "id": str(source.id),
        "title": source.title,
        "chunk_count": source.chunk_count,
        "fact_count": fact_result["fact_count"],
    }


@router.get("/projects/{project_id}/knowledge/sources/{source_id}", response_model=ProjectSourceResponse)
async def get_knowledge_source(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == uid)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="资料不存在")
    return source


@router.patch("/projects/{project_id}/knowledge/sources/{source_id}", response_model=ProjectSourceResponse)
async def update_knowledge_source(
    project_id: str,
    source_id: str,
    req: ProjectSourceUpdate,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == uid)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="资料不存在")

    update_data = req.model_dump(exclude_unset=True)
    # 如果改成 fanfic_rule，强制 always_inject = True
    if "source_type" in update_data and update_data["source_type"] == "fanfic_rule":
        source.always_inject = True
    for field, value in update_data.items():
        if field == "always_inject" and source.source_type == "fanfic_rule" and value is False:
            continue  # 同人规则不允许关闭 always_inject
        if field == "content" and value is not None:
            source.token_count = len(value)
        setattr(source, field, value)

    await db.commit()

    # 内容变更后自动重建切片与事实索引
    if "content" in update_data and update_data["content"] is not None:
        from services.knowledge_source import chunk_and_save
        await chunk_and_save(db, uid, sid)
        from services.knowledge_fact_index import rebuild_source_facts
        await rebuild_source_facts(db, uid, sid)

    # 重新查询确保所有列被正确加载
    result = await db.execute(select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == uid))
    return result.scalar_one()


@router.delete("/projects/{project_id}/knowledge/sources/{source_id}", status_code=204)
async def delete_knowledge_source(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """删除资料条目，级联删除关联的 chunks 与事实索引。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == uid)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="资料不存在")

    # 级联删除 chunks
    chunks_result = await db.execute(
        select(ProjectSourceChunk).where(
            ProjectSourceChunk.source_id == sid, ProjectSourceChunk.project_id == uid
        )
    )
    for chunk in chunks_result.scalars().all():
        await db.delete(chunk)

    await db.execute(
        delete(ProjectKnowledgeFact).where(
            ProjectKnowledgeFact.source_id == sid,
            ProjectKnowledgeFact.project_id == uid,
        )
    )
    await db.execute(
        delete(CharacterAppearance).where(
            CharacterAppearance.source_id == sid,
            CharacterAppearance.project_id == uid,
        )
    )

    await db.delete(source)
    await db.commit()


@router.get("/projects/{project_id}/knowledge/sources/{source_id}/chunks", response_model=list[ProjectSourceChunkResponse])
async def list_source_chunks(
    project_id: str,
    source_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """分页列出资料条目下的切片，避免大资料一次性渲染卡住页面。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    result = await db.execute(
        select(ProjectSourceChunk)
        .where(ProjectSourceChunk.source_id == sid, ProjectSourceChunk.project_id == uid)
        .order_by(ProjectSourceChunk.chunk_index)
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


# ── QA Session CRUD ──

@router.get("/projects/{project_id}/knowledge/sessions", response_model=list[KnowledgeQaSessionResponse])
async def list_knowledge_sessions(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    result = await db.execute(
        select(KnowledgeQaSession)
        .where(KnowledgeQaSession.project_id == uid)
        .order_by(KnowledgeQaSession.updated_at.desc())
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/knowledge/sessions", response_model=KnowledgeQaSessionResponse)
async def create_knowledge_session(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    session = KnowledgeQaSession(project_id=uid, title="资料问答")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("/projects/{project_id}/knowledge/sessions/{session_id}/messages", response_model=list[KnowledgeQaMessageResponse])
async def list_session_messages(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(session_id)

    result = await db.execute(
        select(KnowledgeQaMessage)
        .where(KnowledgeQaMessage.session_id == sid, KnowledgeQaMessage.project_id == uid)
        .order_by(KnowledgeQaMessage.created_at)
    )
    return result.scalars().all()


@router.patch("/projects/{project_id}/knowledge/sessions/{session_id}", response_model=KnowledgeQaSessionResponse)
async def update_knowledge_session(
    project_id: str,
    session_id: str,
    req: KnowledgeQaSessionUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """更新问答会话标题。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(session_id)

    result = await db.execute(
        select(KnowledgeQaSession).where(
            KnowledgeQaSession.id == sid,
            KnowledgeQaSession.project_id == uid,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if req.title is not None:
        session.title = req.title

    await db.commit()
    await db.refresh(session)
    return session


@router.delete("/projects/{project_id}/knowledge/sessions/{session_id}")
async def delete_knowledge_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """删除问答会话及其所有消息。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(session_id)

    result = await db.execute(
        select(KnowledgeQaSession).where(
            KnowledgeQaSession.id == sid,
            KnowledgeQaSession.project_id == uid,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 删除所有消息
    await db.execute(
        delete(KnowledgeQaMessage).where(KnowledgeQaMessage.session_id == sid)
    )

    # 删除会话
    await db.delete(session)
    await db.commit()

    return Response(status_code=204)


# ── 知识源切片与搜索 ──

@router.post("/projects/{project_id}/knowledge/sources/{source_id}/reindex")
async def reindex_single_source(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """重新切片单个资料条目，并重建其事实索引。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    from services.knowledge_source import chunk_and_save
    from services.knowledge_fact_index import rebuild_source_facts

    chunk_count = await chunk_and_save(db, uid, sid)
    fact_result = await rebuild_source_facts(db, uid, sid)
    return {
        "source_id": source_id,
        "chunk_count": chunk_count,
        "fact_count": fact_result["fact_count"],
    }


@router.post("/projects/{project_id}/knowledge/reindex")
async def reindex_all_sources(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """重新切片项目的所有资料条目，并重建项目事实索引。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.knowledge_source import reindex_project_sources
    from services.knowledge_fact_index import rebuild_project_facts

    result = await reindex_project_sources(db, uid)
    fact_result = await rebuild_project_facts(db, uid)
    return {
        "source_count": result["total_sources"],
        "chunk_count": result["total_chunks"],
        "fact_count": fact_result["fact_count"],
    }


@router.get("/projects/{project_id}/knowledge/facts")
async def list_knowledge_facts(
    project_id: str,
    fact_type: str | None = None,
    subject: str | None = None,
    object: str | None = None,
    source_id: str | None = None,
    confidence: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出规则事实索引。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.knowledge_fact_index import list_facts

    items, total = await list_facts(
        db, uid,
        fact_type=fact_type, subject=subject, object_=object,
        source_id=source_id, confidence=confidence,
        limit=limit, offset=offset,
    )
    return {
        "items": [
            {
                "id": str(f.id),
                "fact_type": f.fact_type,
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "confidence": f.confidence,
                "source_id": str(f.source_id),
                "chunk_id": str(f.chunk_id) if f.chunk_id else None,
                "evidence_text": f.evidence_text,
                "extractor": f.extractor,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in items
        ],
        "total": total,
    }


@router.post("/projects/{project_id}/knowledge/facts/rebuild")
async def rebuild_knowledge_facts(
    project_id: str,
    req: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """重建事实索引（单 source 或全项目）。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.knowledge_fact_index import rebuild_source_facts, rebuild_project_facts

    body = req or {}
    source_id = body.get("source_id")
    fact_types = body.get("fact_types") or ["character_system"]

    if source_id:
        result = await rebuild_source_facts(db, uid, source_id, fact_types=fact_types)
        return {
            "source_count": 1,
            "chunk_count": result["chunk_count"],
            "fact_count": result["fact_count"],
        }
    result = await rebuild_project_facts(db, uid, fact_types=fact_types)
    return result


# ── 结构化知识人工修正 CRUD ──────────────────────────────
# 表名别名 → 真实表名 + ORM 模型
_STRUCTURED_TABLE_MAP = {
    "characters": ("character_profile", CharacterProfile),
    "abilities": ("ability_profile", AbilityProfile),
    "events": ("event_timeline", EventTimeline),
    "world_rules": ("world_rule", WorldRule),
    # 兼容直接用真实表名
    "character_profile": ("character_profile", CharacterProfile),
    "ability_profile": ("ability_profile", AbilityProfile),
    "event_timeline": ("event_timeline", EventTimeline),
    "world_rule": ("world_rule", WorldRule),
}

# 各表允许用户设置的字段白名单（origin/canon_level/source_priority 由后端强制）
_STRUCTURED_WRITABLE_FIELDS: dict[str, set[str]] = {
    "character_profile": {"name", "aliases", "identity_desc", "status_desc", "confidence"},
    "ability_profile": {"character_name", "ability_type", "ability_name", "level_desc",
                        "status", "first_seen_chapter", "confidence"},
    "event_timeline": {"event_title", "event_desc", "characters", "location_desc",
                       "cause_desc", "effect_desc", "importance", "chapter_no", "confidence"},
    "world_rule": {"category", "rule_text", "priority", "chapter_no", "confidence"},
}


@router.get("/projects/{project_id}/knowledge/structured/{table}")
async def list_structured_knowledge(
    project_id: str,
    table: str,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出结构化知识表数据（人物/能力/事件/世界规则）。

    支持别名（characters/abilities/events/world_rules）和真实表名。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from sqlalchemy import select, func
    if table not in _STRUCTURED_TABLE_MAP:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"无效表名: {table}")
    real_table, Model = _STRUCTURED_TABLE_MAP[table]

    base = select(Model).where(Model.project_id == uid)
    count_base = select(func.count(Model.id)).where(Model.project_id == uid)
    total = (await db.execute(count_base)).scalar() or 0
    rows = (await db.execute(
        base.order_by(Model.source_priority.desc(), Model.confidence.desc())
        .offset(offset).limit(limit)
    )).scalars().all()

    items = []
    for r in rows:
        item = {
            "id": str(r.id),
            "source_id": str(r.source_id) if r.source_id else None,
            "canon_level": r.canon_level,
            "origin": r.origin,
            "source_priority": r.source_priority,
            "confidence": r.confidence,
            "evidence": r.evidence or [],
            "chapter_no": getattr(r, "chapter_no", None) or getattr(r, "first_seen_chapter", None),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        # 各表特有字段（用 real_table 而非别名 table）
        if real_table == "character_profile":
            item.update({
                "name": r.name, "aliases": r.aliases or [],
                "identity_desc": r.identity_desc, "status_desc": r.status_desc,
                "first_seen_chapter": getattr(r, "first_seen_chapter", None),
                "last_seen_chapter": getattr(r, "last_seen_chapter", None),
                "appearance_count": getattr(r, "appearance_count", 0),
            })
        elif real_table == "ability_profile":
            item.update({"character_name": r.character_name, "ability_type": r.ability_type, "ability_name": r.ability_name, "level_desc": r.level_desc, "status": r.status})
        elif real_table == "event_timeline":
            item.update({"event_title": r.event_title, "event_desc": r.event_desc, "characters": r.characters or [], "location_desc": r.location_desc, "importance": r.importance})
        elif real_table == "world_rule":
            item.update({"category": r.category, "rule_text": r.rule_text, "priority": r.priority})
        items.append(item)
    return {"items": items, "total": total}


@router.post("/projects/{project_id}/knowledge/structured/{table}")
async def create_structured_record(
    project_id: str,
    table: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """手动新增结构化知识记录。

    强制写入 origin=manual, canon_level=manual, source_priority=100, confidence=1.0。
    manual_note 会追加到 evidence 列表，保留来源追溯。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    if table not in _STRUCTURED_TABLE_MAP:
        raise HTTPException(status_code=400, detail=f"无效表名: {table}")
    real_table, Model = _STRUCTURED_TABLE_MAP[table]
    allowed = _STRUCTURED_WRITABLE_FIELDS[real_table]

    record = Model(
        project_id=uid,
        origin="manual",
        canon_level="manual",
        source_priority=100,
        confidence=float(body.get("confidence", 1.0)),
        evidence=[],
    )
    # manual_note 追加为 evidence，保留来源
    manual_note = body.get("manual_note")
    if manual_note:
        from services.evidence_helpers import make_manual_note
        record.evidence = [make_manual_note(manual_note)]

    for field, value in body.items():
        if field in allowed and value is not None:
            setattr(record, field, value)

    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "id": str(record.id), "table": real_table,
        "origin": record.origin, "canon_level": record.canon_level,
        "source_priority": record.source_priority,
        "evidence": record.evidence or [],
    }


@router.patch("/projects/{project_id}/knowledge/structured/{table}/{record_id}")
async def update_structured_record(
    project_id: str,
    table: str,
    record_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """编辑结构化知识记录。

    保留原 evidence，追加 manual_note 到 evidence。origin 升级为 manual（若原来不是）。
    metadata 中记录 edited_by_user / previous_origin。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    if table not in _STRUCTURED_TABLE_MAP:
        raise HTTPException(status_code=400, detail=f"无效表名: {table}")
    real_table, Model = _STRUCTURED_TABLE_MAP[table]
    allowed = _STRUCTURED_WRITABLE_FIELDS[real_table]
    rid = _to_uuid(record_id)

    # 必须同时校验 project_id，防止跨项目修改
    result = await db.execute(
        select(Model).where(Model.id == rid, Model.project_id == uid)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在或不属于该项目")

    previous_origin = record.origin
    for field, value in body.items():
        if field in allowed and value is not None:
            setattr(record, field, value)

    # 编辑后升级为 manual 优先级
    record.origin = "manual"
    record.canon_level = "manual"
    record.source_priority = 100

    # 追加 manual_note 到 evidence，不丢弃原 evidence
    manual_note = body.get("manual_note")
    if manual_note:
        from services.evidence_helpers import make_manual_note
        existing = list(record.evidence or [])
        existing.append(make_manual_note(
            manual_note,
            source_id=str(record.source_id) if getattr(record, "source_id", None) else None,
        ))
        record.evidence = existing

    await db.commit()
    await db.refresh(record)
    return {
        "id": str(record.id), "table": real_table,
        "origin": record.origin, "canon_level": record.canon_level,
        "source_priority": record.source_priority,
        "previous_origin": previous_origin,
        "evidence": record.evidence or [],
    }


@router.delete("/projects/{project_id}/knowledge/structured/{table}/{record_id}")
async def delete_structured_record(
    project_id: str,
    table: str,
    record_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """删除结构化知识记录（MVP 物理删除，限制 project_id 作用域）。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    if table not in _STRUCTURED_TABLE_MAP:
        raise HTTPException(status_code=400, detail=f"无效表名: {table}")
    real_table, Model = _STRUCTURED_TABLE_MAP[table]
    rid = _to_uuid(record_id)

    # 必须同时校验 project_id
    result = await db.execute(
        select(Model).where(Model.id == rid, Model.project_id == uid)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在或不属于该项目")

    await db.delete(record)
    await db.commit()
    return {"id": record_id, "deleted": True, "table": real_table}


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/split-chapters")
async def split_chapters(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """切分资料源为章节，写入 project_source_chapters。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    from services.chapter_splitter import split_source_chapters

    result = await split_source_chapters(db, uid, sid)
    return {
        "project_id": project_id,
        "source_id": source_id,
        "chapter_count": result.chapter_count,
        "split_type": result.split_type,
    }


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/extract")
async def start_extraction(
    project_id: str,
    source_id: str,
    req: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """启动（或推进）LLM 结构化抽取任务。

    采用 job 表 + 轮询推进模式：本接口创建/查找 job 并推进若干章，
    返回 job_id 与当前状态。前端轮询 /extract/status 查进度，
    再次调用本接口继续推进未完成章节。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    from services.extraction_service import advance_extraction_job

    body = req or {}
    job = await advance_extraction_job(
        db, uid, sid,
        user_id=str(user.id),
        genre=body.get("genre", "magic_fantasy"),
        canon_level=body.get("canon_level", "original"),
        origin=body.get("origin", "llm_extracted"),
        chapter_no_start=body.get("chapter_no_start", 1),
        chapter_no_end=body.get("chapter_no_end"),
        max_chapters_per_run=body.get("max_chapters_per_run", 20),
        force_reextract=body.get("force_reextract", False),
    )
    return {
        "job_id": str(job["id"]),
        "status": job["status"],
        "provider": job.get("provider") or "mock",
        "is_mock": (job.get("provider") or "mock") == "mock",
        "last_run_outcome": job.get("last_run_outcome", "none"),
        "total_chapters": job["total_chapters"],
        "extracted_count": job["extracted_count"],
        "validated_count": job["validated_count"],
        "merged_count": job["merged_count"],
        "failed_count": job["failed_count"],
    }


@router.get("/projects/{project_id}/knowledge/sources/{source_id}/extract/status")
async def get_extraction_status(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询抽取任务状态。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    from services.extraction_service import get_latest_job_status, _resolve_provider_name

    # 当前用户的 LLM provider，用于前端区分 mock / 真实数据
    provider_name = await _resolve_provider_name(str(user.id), db)
    is_mock = provider_name == "mock"

    status = await get_latest_job_status(db, uid, sid)
    if not status:
        return {"job_id": None, "status": "NONE", "total_chapters": 0,
                "extracted_count": 0, "validated_count": 0,
                "merged_count": 0, "failed_count": 0,
                "pending_count": 0,
                "provider": provider_name, "is_mock": is_mock,
                "current_chapter_no": None, "last_error": None,
                "last_run_outcome": "none"}
    # 兼容文档 §15.4 字段名：id → job_id
    status["job_id"] = status.get("id")
    # provider 优先用 job 上记录的（反映抽取时的实际 provider），回退当前配置
    status["provider"] = status.get("provider") or provider_name
    status["is_mock"] = status["provider"] == "mock"
    # pending_count = 总章节 - 已抽取数
    status["pending_count"] = max(0, (status.get("total_chapters", 0) or 0) - (status.get("extracted_count", 0) or 0))
    return status


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/extract/reset")
async def reset_extraction(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """重置抽取：清除 staging/job/4 张结构化表，保留原文和 chunks。

    只删当前 project_id + source_id 的数据。重置后 status 回到 NONE，
    用户可重新切分章节 + 开始抽取。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    from services.extraction_service import reset_extraction as _reset

    return await _reset(db, uid, sid)


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/extract/pause")
async def pause_extraction(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """暂停抽取任务。不打断正在执行的 LLM 请求，但下一次推进不执行。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)
    from services.extraction_service import pause_extraction_job
    result = await pause_extraction_job(db, uid, sid)
    if not result:
        raise HTTPException(status_code=404, detail="没有可暂停的抽取任务")
    return result


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/extract/cancel")
async def cancel_extraction(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """取消抽取任务。不删除已有 staging 和结构化结果。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)
    from services.extraction_service import cancel_extraction_job
    result = await cancel_extraction_job(db, uid, sid)
    if not result:
        raise HTTPException(status_code=404, detail="没有可取消的抽取任务")
    return result


@router.get("/projects/{project_id}/knowledge/sources/{source_id}/extract/failures")
async def list_extraction_failures(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出失败的章节列表。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)
    from services.extraction_service import list_extraction_failures as _list
    return await _list(db, uid, sid)


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/extract/retry-chapter")
async def retry_extraction_chapter(
    project_id: str,
    source_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """单章重试：只重跑指定章节，不影响其他章节。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)
    from services.extraction_service import retry_extraction_chapter as _retry
    chapter_no = int(body.get("chapter_no", 0))
    if chapter_no <= 0:
        raise HTTPException(status_code=400, detail="chapter_no 必须为正整数")
    return await _retry(
        db, uid, sid, chapter_no, str(user.id),
        force_reextract=body.get("force_reextract", True),
    )


@router.get("/projects/{project_id}/knowledge/structured/characters/{character_id}/appearances")
async def list_character_appearances(
    project_id: str,
    character_id: str,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出人物出场记录（按章节排序，支持分页）。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    cid = _to_uuid(character_id)
    from services.extraction_service import list_character_appearances
    return await list_character_appearances(db, uid, cid, limit, offset)


@router.post("/projects/{project_id}/knowledge/structured-qa")
async def structured_qa(
    project_id: str,
    req: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """结构化问答：基于结构化知识表回答人物能力/事件/世界规则三类问题。

    查询优先级：manual > fanfic > ability_profile > project_knowledge_facts > RAG fallback。
    禁止 LLM 凭常识补充，查不到明确说未找到。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.structured_qa import answer_structured_question

    question = req.get("question", "")
    conversation_id = req.get("conversation_id")
    result = await answer_structured_question(db, uid, question, conversation_id=conversation_id)
    return result


# ── 人物别名归一簇 CRUD + 候选探测 ────────────────────────

@router.get("/projects/{project_id}/knowledge/aliases")
async def list_alias_clusters(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """列出项目级人物别名归一簇。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_service import list_alias_clusters
    return await list_alias_clusters(db, uid)


@router.post("/projects/{project_id}/knowledge/aliases")
async def create_alias_cluster(
    project_id: str,
    req: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """新建人物别名归一簇（canonical_name + aliases[]）。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_service import create_alias_cluster, AliasClusterConflictError
    from sqlalchemy.exc import IntegrityError
    try:
        cluster = await create_alias_cluster(
            db, uid,
            canonical_name=req.get("canonical_name", "").strip(),
            aliases=req.get("aliases", []) or [],
            source=req.get("source", "manual"),
            confidence=float(req.get("confidence", 1.0)),
            note=req.get("note"),
        )
        await db.commit()
        return cluster
    except AliasClusterConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="该 canonical_name 已存在 alias 簇")


@router.put("/projects/{project_id}/knowledge/aliases/{cluster_id}")
async def update_alias_cluster(
    project_id: str,
    cluster_id: str,
    req: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """更新别名簇的 aliases/note/confidence。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_service import update_alias_cluster, AliasClusterConflictError
    try:
        cluster = await update_alias_cluster(
            db, uid, cluster_id,
            aliases=req.get("aliases"),
            note=req.get("note"),
            confidence=float(req["confidence"]) if req.get("confidence") is not None else None,
        )
    except AliasClusterConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    if not cluster:
        raise HTTPException(status_code=404, detail="alias 簇不存在")
    await db.commit()
    return cluster


@router.delete("/projects/{project_id}/knowledge/aliases/{cluster_id}", status_code=204)
async def delete_alias_cluster(
    project_id: str,
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """删除别名簇。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_service import delete_alias_cluster
    deleted = await delete_alias_cluster(db, uid, cluster_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="alias 簇不存在")
    await db.commit()


@router.post("/projects/{project_id}/knowledge/aliases/backfill-preview")
async def backfill_alias_preview(
    project_id: str,
    req: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """dry-run：预览回填将影响的行数，不改库。

    body: {"cluster_id": "..."} 可选（不传=该 project 全部簇）。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_backfill import backfill_preview
    cluster_id = (req or {}).get("cluster_id")
    return await backfill_preview(db, uid, cluster_id=cluster_id)


@router.post("/projects/{project_id}/knowledge/aliases/backfill")
async def backfill_alias_apply(
    project_id: str,
    req: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """回填：把已配置 alias 簇的历史脏数据合并到规范名，改 4 张正式表。

    body: {"cluster_id": "..."} 可选（不传=该 project 全部簇）。
    不可逆——建议先调 backfill-preview 确认影响范围。
    不动原文/staging/chunks/facts。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_backfill import apply_backfill
    cluster_id = (req or {}).get("cluster_id")
    stats = await apply_backfill(db, uid, cluster_id=cluster_id)
    await db.commit()
    return {"status": "backfilled", **stats}


@router.get("/projects/{project_id}/knowledge/aliases/candidates")
async def detect_alias_candidates(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """只读候选探测：输出疑似同人候选簇，不自动写库，需人工确认。

    规则：名字互为子串 + aliases 字段交叉。保守、误报少。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    from services.alias_service import detect_alias_candidates
    return await detect_alias_candidates(db, uid)


@router.post("/projects/{project_id}/knowledge/search")
async def search_knowledge(
    project_id: str,
    req: KnowledgeSearchRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """纯检索：在资料库中搜索匹配片段。不调用 LLM。

    这个接口适合调试知识库召回质量：如果 search 找不到证据，
    ask 基本也很难给出可靠回答。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.knowledge_source import search_project_knowledge

    results = await search_project_knowledge(
        db, uid, req.query,
        source_type=req.source_type,
        limit=req.limit,
    )
    return {"results": results}


@router.post("/projects/{project_id}/knowledge/sources/{source_id}/summarize")
async def summarize_knowledge_source(
    project_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """对资料条目进行 AI 摘要（chunk + 源级）。

    原文永远保存。摘要失败时只返回错误，不清空已有数据。
    摘要和关键词是检索增强字段，不是原文替代品。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    sid = _to_uuid(source_id)

    # 先确保切片存在
    from services.knowledge_source import chunk_and_save, summarize_project_source

    await chunk_and_save(db, uid, sid)
    result = await summarize_project_source(db, uid, sid, user.id)
    return result


@router.post("/projects/{project_id}/knowledge/ask")
async def ask_knowledge(
    project_id: str,
    req: KnowledgeAskRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """资料问答：检索资料库 + 结构化表，调用 LLM 生成带引用的回答。

    具体检索、证据分类、prompt 预算和会话保存都在 services.knowledge_source
    的 ask_knowledge_question 中完成。路由层只做权限校验和参数透传。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    from services.knowledge_source import ask_knowledge_question

    result = await ask_knowledge_question(
        db, uid, user.id, req.question,
        conversation_id=req.conversation_id,
        chapter_num=req.chapter_num,
        include_structured=req.include_structured,
        include_web=req.include_web,
        web_provider=req.web_provider,
        web_api_key=req.web_api_key,
        web_base_url=req.web_base_url,
    )
    return result
