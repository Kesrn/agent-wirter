"""API 路由"""

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
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.project_knowledge_fact import ProjectKnowledgeFact
from models.knowledge_qa_session import KnowledgeQaSession
from models.knowledge_qa_message import KnowledgeQaMessage
from models.writing_memory_staging import WritingMemoryStaging
from models.structured_knowledge import (
    CharacterProfile, AbilityProfile, EventTimeline, WorldRule, CharacterAppearance,
)
from schemas.api import (
    ProjectCreate, ProjectUpdate, ProjectResponse,
    TxtImportResponse,
    ExpertCreate, ExpertUpdate, ExpertResponse,
    ChapterCreate, ChapterResponse, ChapterUpdate,
    ChapterReviewNoteCreate, ChapterReviewNoteUpdate, ChapterReviewNoteResponse,
    ChapterStructureExtractRequest, ChapterStructureExtractResponse,
    ChapterVersionResponse, ChapterVersionListItemResponse, ChapterVersionDiffRequest, ChapterVersionDiffResponse,
    WorldEntryCreate, WorldEntryUpdate, WorldEntryResponse,
    CharacterCreate, CharacterUpdate, CharacterMergeRequest, CharacterResponse,
    CharacterEventUpsert, CharacterEventResponse,
    CharacterRelationCreate, CharacterRelationUpdate, CharacterRelationResponse,
    OutlineCreate, OutlineUpdate, OutlineResponse,
    HiddenThreadCreate, HiddenThreadUpdate, HiddenThreadResponse,
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
    AiRunResponse, AiRunListItemResponse, AiRunStepResponse,
    HumanDecisionRequest, HumanDecisionResponse,
    WritingMemoryStagingResponse,
    AuthUser,
)
from agents.safety import validate_expert_safety
from agents.expert_templates import BUILTIN_EXPERTS
from agents.llm_provider import LLMConfigError, get_llm_provider
from agents.workflow import get_creative_app, CreativeState
from harness.run_manager import (
    create_run, mark_running, mark_waiting_human, mark_completed, mark_failed, mark_cancelled,
)
from harness.step_logger import start_step, finish_step, fail_step
from harness.human_interrupt_service import (
    create_interrupt, resolve_interrupt, get_interrupt_by_run, get_interrupt_by_thread, check_interrupt_resolved
)
from skills.runner import ExpertSkillResult, build_expert_skill_pack, build_expert_system_prompt
from api.auth import get_current_user
from api.llm_deps import get_user_llm_config
from api.rate_limiter import agent_limiter
from rag.embedding_service import generate_embedding, _update_embedding_bg
from services.diff_service import compute_diff
from services.chapter_save import save_chapter_content
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
from services.memory_staging_service import run_fact_extraction
from observability.langfuse import activate_langfuse_context, current_langfuse_trace_id, finish_langfuse_context
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

WRITER_ROLES = ("writer",)  # 只有 writer 角色的输出会写入章节正文
ENHANCE_MAX_OUTPUT_WORDS = min(5000, max(1000, int(settings.MAX_TOKENS_LIMIT * 0.65)))
ENHANCE_MIN_OUTPUT_WORDS = 20
ALLOWED_IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _format_bytes_limit(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:g}MB"
    if size >= 1024:
        return f"{size / 1024:g}KB"
    return f"{size}B"


def _parse_directions(text: str) -> list[str]:
    """从容错解析 LLM 输出为字符串列表。尝试 JSON 数组，失败则按换行分割。"""
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
    """Serialize a workflow skill pack summary without breaking old clients."""
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
    """Return only newly emitted skill packs from a LangGraph node output."""
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
    """Build a direct-route skill pack and align its SSE key with UI steps."""
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
    """Build a compact article/copywriting brief for prompts."""
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
    return (
        f"你是一位专业中文内容策划和文案编辑，当前任务是{task}。"
        "你服务的是文章/文案项目，不是小说创作。"
        "禁止使用小说章节、剧情续写、角色登场、世界观设定、伏笔推进等叙事小说口吻。"
        "输出要围绕主题、受众、平台、结构、表达目标和行动引导。"
        "除非用户明确要求，不能输出解释性前缀、修改说明或项目符号清单；正文任务只输出可直接使用的正文。"
    )


def _article_generate_prompt(req: GenerateRequest, creative_context: str, current_content: str) -> str:
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
    return GenerationRecordListItemResponse(
        id=record.id,
        project_id=record.project_id,
        chapter_id=record.chapter_id,
        document_id=record.document_id,
        mode=record.mode,
        expert_id=record.expert_id,
        direction=record.direction,
        word_count=record.word_count,
        status=record.status,
        langfuse_trace_id=record.langfuse_trace_id,
        created_at=record.created_at,
    )


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
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 UUID 格式")


async def _verify_project_owner(project_id: uuid.UUID, user_id: str, db: AsyncSession) -> Project:
    """校验项目存在且属于当前用户，否则 404。"""
    result = await db.execute(select(Project).where(Project.id == project_id, Project.owner_id == user_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _delete_project_tree(project_id: uuid.UUID, db: AsyncSession) -> None:
    """Delete a project and all project-scoped rows."""
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

    for tpl in BUILTIN_EXPERTS:
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

        for tpl in BUILTIN_EXPERTS:
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
    if "content" in req.model_fields_set:
        await save_chapter_content(db, chapter, req.content or "", source="manual")
    if req.status is not None:
        chapter.status = req.status

    await db.commit()
    await db.refresh(chapter)
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

    outline = Outline(
        project_id=uid,
        sequence_number=req.sequence_number,
        title=req.title,
        summary=req.summary,
        turning_point=req.turning_point,
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
    # Rate limiting
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    project_result = await db.execute(select(Project).where(Project.id == uid))
    project = project_result.scalar_one()
    is_article_project = project.mode == "article"
    if "/documents/" in request.url.path and not is_article_project:
        raise HTTPException(status_code=400, detail="Document generation only available for article projects")

    # 章节/稿件定位：小说走 Chapter，文章走 Document。
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
            generation_record_saved = False

            async def _save_generation_history(
                content: str,
                *,
                expert_id: str | uuid.UUID | None = None,
                review_results: dict | None = None,
                skill_packs: list[dict] | None = None,
                run_id: str | None = None,
            ) -> str | None:
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
                if not record_id:
                    return ""
                return (
                    "event: generation_record\n"
                    f"data: {json.dumps({'id': record_id, 'status': 'candidate', 'langfuse_trace_id': current_langfuse_trace_id()}, ensure_ascii=False)}\n\n"
                )

            yield f"event: progress\ndata: {json.dumps({'message': '开始生成', 'mode': req.mode, 'chapter_num': req.chapter_num}, ensure_ascii=False)}\n\n"

            # 检查客户端是否已断开连接（取消时前端会 abort SSE 连接）
            async def _check_cancelled():
                if await request.is_disconnected():
                    logger.info("客户端已断开连接，取消生成")
                    return True
                return False

            # 获取当前章节/稿件内容（enhance/continue/summarize 都需要）
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

            # 确定章节序号（供 ChapterContextService 按章加载上下文）
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
                else:
                    user_prompt = f"## 上下文\n{creative_context}\n\n## 续写方向\n{req.turn_direction}\n\n## 用户补充\n{req.user_note or '无'}\n\n## 当前章节（续写接在后面）\n{chapter_content}\n\n请严格按照上下文中的设定续写："
                    system_prompt = "你是一位才华横溢的创意写作大师。根据指定方向续写章节，注意与原文的标点衔接。"
                    plan = plan_direct_skill_pack(
                        project_mode=project.mode,
                        generate_mode=req.mode,
                        action="continue_generate",
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
                    system_prompt = build_expert_system_prompt("writer", system_prompt, pack)
                    direct_skill_packs = [summary]
                    done_message = "续写完成"
                writer_content = ""
                async for chunk in provider.generate_stream(system_prompt, user_prompt):
                    if await _check_cancelled():
                        return
                    writer_content += chunk
                    output_event = "content_output" if is_article_project else "writer_output"
                    yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"

                record_id = await _save_generation_history(writer_content, skill_packs=direct_skill_packs)
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': done_message}, ensure_ascii=False)}\n\n"
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

            app = get_creative_app(enabled_experts=enabled_experts)
            thread_id = f"{uid}:{target_chapter_id or target_document_id or 'no-chapter'}:{uuid.uuid4().hex[:8]}"

            # ── Harness: 创建 AI Run ──
            run_type = {
                "full_pipeline": "CHAPTER_DRAFT",
                "continue": "CHAPTER_CONTINUE",
                "enhance": "CHAPTER_REWRITE",
                "summarize": "SUMMARY_GENERATION",
            }.get(req.mode, "CHAPTER_DRAFT")
            ai_run = await create_run(
                db,
                project_id=uid,
                chapter_id=target_chapter_id,
                document_id=target_document_id,
                run_type=run_type,
                mode=req.mode,
                user_goal=req.user_note,
                model_config_snapshot=llm_config_dict,
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

            initial_state: CreativeState = {
                "project_id": str(uid),
                "chapter_id": str(target_chapter_id or target_document_id) if (target_chapter_id or target_document_id) else "",
                "chapter_num": chapter_num,
                "mode": req.mode,
                "context": "",
                "draft": "",
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
                "target_words": req.target_words or 0,
                "selected_direction": req.selected_direction or "",
                "user_note": req.user_note or "",
                "skill_packs": [],
                # ── Harness 注入：节点内 LLM call log 用（不传 db，避免 msgpack 序列化失败）──
                "harness_run_id": run_id_str,
                "harness_step_id": None,
            }

            config = {"configurable": {"thread_id": thread_id}}

            # 节点到 SSE 事件的映射
            NODE_EVENT_MAP = {
                "writer": "writer_output",
                "critic": "critic_output",
                "consistency_checker": "consistency_check",
            }

            # 流式输出的节点（逐 token 发送）
            STREAM_NODES = {"writer"}

            writer_content = ""
            workflow_skill_packs: list[dict] = []
            seen_skill_pack_keys: set[tuple[str, str]] = set()
            current_stream_node = None  # 追踪当前正在流式输出的节点

            # ── Harness: step 追踪 ──
            STEP_NODE_MAP = {
                "context_loader": ("build_context", 1),
                "writer": ("generate_draft", 2),
                "critic": ("critique", 3),
                "consistency_checker": ("consistency_check", 3),
                "human_review": ("human_review", 4),
            }
            active_steps: dict[str, object] = {}  # node_name -> AiRunStep
            revision_round = 0

            # 逐节点流式执行
            async for event in app.astream_events(initial_state, config=config, version="v2"):
                # 客户端取消时立即停止，不继续跑后续节点
                if await _check_cancelled():
                    try:
                        await mark_cancelled(db, ai_run)
                        await db.commit()
                    except Exception:
                        logger.warning("harness mark_cancelled 失败", exc_info=True)
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
                                if node_name == "writer":
                                    output_snapshot = {"content_hash": str(hash(output.get("draft", "")))[:128]}
                                elif node_name == "context_loader":
                                    output_snapshot = {"context_len": len(output.get("context", ""))}
                                elif node_name == "critic":
                                    output_snapshot = {"critique_count": len(output.get("critiques", []))}
                                elif node_name == "consistency_checker":
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

                    elif node_name == "writer":
                        draft = output.get("draft", "") if isinstance(output, dict) else ""
                        writer_content = draft
                        # 如果没有流式 token 事件（generate 而非 generate_stream），发送完整内容
                        if draft:
                            yield f"event: writer_output\ndata: {json.dumps({'content': draft}, ensure_ascii=False)}\n\n"

                    elif node_name == "critic":
                        critiques = output.get("critiques", []) if isinstance(output, dict) else []
                        yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"

                    elif node_name == "consistency_checker":
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
                            if current_stream_node == "writer":
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

        except Exception as e:
            logger.exception("生成失败")
            try:
                await mark_failed(db, ai_run, error_message=str(e))
                await db.commit()
            except Exception:
                logger.warning("harness mark_failed 失败", exc_info=True)
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
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """确认写作记忆 staging（H1 只改状态，H3 才写正式表）。"""
    staging = await _transition_staging_status(
        db, _to_uuid(project_id), staging_id, "CONFIRMED", user
    )
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
    from models.harness_enums import InterruptDecision

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


# ==================== 工作流恢复（HITL） ====================

@router.post("/projects/{project_id}/documents/resume")
@router.post("/projects/{project_id}/chapters/resume")
async def resume_chapter_generation(
    request: Request,
    project_id: str,
    thread_id: str = Query(..., description="HITL 暂停时返回的 thread_id"),
    action: str = Query(default="approve", pattern=r"^(approve|reject|review|revise)$"),
    feedback: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """恢复 HITL 暂停的工作流

    用户对 Editor 输出做出决策后，恢复工作流执行：
    - approve: 结束流程，落库最终内容
    - reject: 终止流程，不落库
    - review: 审核当前候选稿，返回可选修改方向
    - revise: 将用户选择的修改方向注入状态，重新从 Writer 开始
    """
    # Rate limiting
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    project_result = await db.execute(select(Project).where(Project.id == uid))
    project = project_result.scalar_one()
    if "/documents/" in request.url.path and project.mode != "article":
        raise HTTPException(status_code=400, detail="Document generation only available for article projects")

    # 查询项目启用的专家，用于 HITL 恢复时构建正确的图
    exp_result = await db.execute(
        select(Expert).where(Expert.project_id == uid, Expert.is_enabled == True)
    )
    enabled_experts = exp_result.scalars().all()
    app = get_creative_app(enabled_experts=enabled_experts)
    config = {"configurable": {"thread_id": thread_id}}

    async def event_stream():
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
        try:
            # 获取当前状态
            state = await app.aget_state(config)
            if not state or not state.values:
                yield f"event: error\ndata: {json.dumps({'message': '工作流状态不存在或已过期'}, ensure_ascii=False)}\n\n"
                return

            # ── Harness: 按 thread_id 反查 run_id（resume 路径关联 run）──
            _resume_run = (
                await db.execute(select(AiRun).where(AiRun.thread_id == thread_id))
            ).scalar_one_or_none()
            _resume_run_id = str(_resume_run.id) if _resume_run else None

            # ── Harness E: 幂等性检查 - 防止重复 approve ──
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

            if action == "reject":
                yield f"event: done\ndata: {json.dumps({'message': '已拒绝，流程终止'}, ensure_ascii=False)}\n\n"
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
                if feedback:
                    update_state["critiques"] = [f"[用户选择的修改方向] {feedback}"]

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

                        if node_name == "writer":
                            draft = output.get("draft", "") if isinstance(output, dict) else ""
                            revised_content = draft
                            yield f"event: writer_output\ndata: {json.dumps({'content': draft}, ensure_ascii=False)}\n\n"
                        elif node_name == "critic":
                            critiques = output.get("critiques", []) if isinstance(output, dict) else []
                            yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"
                        elif node_name == "consistency_checker":
                            guardrail = output.get("consistency_report", {}) if isinstance(output, dict) else {}
                            if not isinstance(guardrail, dict):
                                guardrail = {}
                            report_text = _guardrail_to_text(guardrail)
                            yield f"event: consistency_check\ndata: {json.dumps({'report': report_text, 'guardrail_result': guardrail}, ensure_ascii=False)}\n\n"
                        elif node_name == "editor":
                            edited = output.get("edited_draft", "") if isinstance(output, dict) else ""
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

            # ── Harness E: approve 时解析 interrupt ──
            if _resume_run:
                from models.harness_enums import InterruptDecision
                interrupt = await get_interrupt_by_thread(db, thread_id)
                if interrupt and not interrupt.resolved:
                    await resolve_interrupt(db, interrupt, decision=InterruptDecision.APPROVE, feedback=feedback)
                    await db.commit()

            current_values = state.values
            # 优先使用 edited_draft（经编辑润色），若无则使用 draft（原始创作）
            raw_content = current_values.get("edited_draft", "") or current_values.get("draft", "")
            target_content_id = current_values.get("chapter_id", "")

            # 落库：小说写 Chapter，文章写 Document。
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
                            await db.commit()
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
                            await db.commit()
                            # ── Phase F: 联动 GenerationRecord.accepted_version_id ──
                            if _resume_run_id:
                                _gr = (
                                    await db.execute(
                                        select(GenerationRecord).where(
                                            GenerationRecord.run_id == _to_uuid(_resume_run_id)
                                        )
                                    )
                                ).scalar_one_or_none()
                                if _gr:
                                    # 查刚创建的版本（最新）
                                    _ver = (
                                        await db.execute(
                                            select(ChapterVersion)
                                            .where(ChapterVersion.chapter_id == chapter.id)
                                            .order_by(ChapterVersion.version_number.desc())
                                            .limit(1)
                                        )
                                    ).scalar_one_or_none()
                                    if _ver:
                                        await update_generation_record_status(
                                            db, _gr, "applied",
                                            accepted_version_id=str(_ver.id),
                                        )
                                        await db.commit()
                            # ── Phase H2: 后台抽取写作记忆 ──
                            # SQLite :memory: 测试环境跳过（独立 session 看不到内存表）；
                            # 生产用 Postgres 不受影响
                            from db.session import get_engine as _get_engine_for_guard
                            _is_sqlite = _get_engine_for_guard().dialect.name == "sqlite"
                            if _resume_run_id and not _is_sqlite:
                                _accepted_vid = (
                                    str(_gr.accepted_version_id) if _gr and _gr.accepted_version_id
                                    else (str(_ver.id) if _ver else None)
                                )
                                if _accepted_vid:
                                    asyncio.create_task(run_fact_extraction(
                                        project_id=str(uid),
                                        chapter_id=str(chapter.id),
                                        chapter_version_id=_accepted_vid,
                                        chapter_sequence_number=chapter.sequence_number,
                                        run_id=_resume_run_id,
                                        content=raw_content,
                                        context=current_values.get("context", ""),
                                        llm_config=current_values.get("llm_config"),
                                    ))
                except Exception:
                    logger.exception("落库失败")
                    yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                    return

            yield f"event: done\ndata: {json.dumps({'message': '已批准，内容已保存'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("恢复工作流失败")
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
    """列出项目的所有资料库条目。支持 source_type 过滤、q 关键词搜索、sort 排序。"""
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
    """新建资料库条目。同人规则默认 always_inject=True。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    always_inject = req.always_inject
    if req.source_type == "fanfic_rule" and not req.always_inject:
        always_inject = True

    # novel 类型：genre/canon_level 存入 metadata_，供抽取阶段读取
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

    # 自动切片（短资料也能保底存一个 chunk）
    from services.knowledge_source import chunk_and_save
    await chunk_and_save(db, uid, str(source.id))

    # 上传后自动重建事实索引，不让用户必须手动点 reindex
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
    """纯检索：在资料库中搜索匹配片段。不调用 LLM。"""
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
    """资料问答：检索资料库 + 结构化表，调用 LLM 生成带引用的回答。"""
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
