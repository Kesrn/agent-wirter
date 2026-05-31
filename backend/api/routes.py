"""API 路由"""

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime

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
from models.document import Document
from models.document_version import DocumentVersion
from models.generation_record import GenerationRecord
from models.world_entry import WorldEntry
from models.character import Character
from models.character_relation import CharacterRelation
from models.outline import Outline
from models.hidden_thread import HiddenThread
from schemas.api import (
    ProjectCreate, ProjectResponse,
    TxtImportResponse,
    ExpertCreate, ExpertUpdate, ExpertResponse,
    ChapterCreate, ChapterResponse, ChapterUpdate,
    ChapterStructureExtractRequest, ChapterStructureExtractResponse,
    ChapterVersionResponse, ChapterVersionListItemResponse, ChapterVersionDiffRequest, ChapterVersionDiffResponse,
    WorldEntryCreate, WorldEntryUpdate, WorldEntryResponse,
    CharacterCreate, CharacterUpdate, CharacterResponse,
    CharacterRelationCreate, CharacterRelationUpdate, CharacterRelationResponse,
    OutlineCreate, OutlineUpdate, OutlineResponse,
    HiddenThreadCreate, HiddenThreadUpdate, HiddenThreadResponse,
    GenerateRequest, ExpertTestRequest,
    DocumentCreate, DocumentUpdate, DocumentResponse,
    DocumentVersionListItemResponse, DocumentVersionResponse,
    DocumentVersionDiffRequest, DocumentVersionDiffResponse,
    GenerationRecordListItemResponse, GenerationRecordResponse,
    GenerationRecordUpdate, GenerationRecordDiffRequest, GenerationRecordDiffResponse,
    AuthUser,
)
from agents.safety import validate_expert_safety
from agents.expert_templates import BUILTIN_EXPERTS
from agents.llm_provider import get_llm_provider
from agents.workflow import get_creative_app, CreativeState
from skills.registry import get_skill_for_node
from api.auth import get_current_user
from api.llm_deps import get_user_llm_config
from api.rate_limiter import agent_limiter
from rag.embedding_service import generate_embedding, _update_embedding_bg
from services.diff_service import compute_diff
from services.chapter_save import save_chapter_content
from services.document_save import save_document_content
from services.txt_import import decode_txt_bytes, split_txt_into_chapters, build_import_meta
from services.structure_extraction import (
    apply_structure_extraction,
    build_structure_preview,
    extract_structure_with_provider,
    normalize_structure_result,
)
from services.generation_record_service import (
    create_generation_record,
    get_generation_record,
    list_generation_records_for_chapter,
    list_generation_records_for_document,
    update_generation_record_status,
)
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
        created_at=record.created_at,
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

    await db.execute(delete(GenerationRecord).where(GenerationRecord.project_id == project_id))
    await db.execute(delete(ChapterVersion).where(ChapterVersion.chapter_id.in_(chapter_ids)))
    await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)))
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

    extraction = normalize_structure_result(req.extraction) if req.extraction is not None else None
    if extraction is None:
        extra_context = None
        if req.include_existing_context:
            extra_context = await _build_structure_context(uid, db)
        provider = get_llm_provider(await get_user_llm_config(user.id, db))
        extraction = await extract_structure_with_provider(
            provider,
            chapter=chapter,
            project_title=project.title,
            extra_context=extra_context,
        )

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
        try:
            generation_record_saved = False

            async def _save_generation_history(
                content: str,
                *,
                expert_id: str | uuid.UUID | None = None,
                review_results: dict | None = None,
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
                    f"data: {json.dumps({'id': record_id, 'status': 'candidate'}, ensure_ascii=False)}\n\n"
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

            # 加载创作上下文（大纲/角色/世界观/暗线/前文），供 enhance/continue/summarize 使用
            from agents.workflow import context_loader_node
            ctx_state = CreativeState(
                project_id=str(uid),
                chapter_id="" if is_article_project else (str(target_chapter_id) if target_chapter_id else ""),
                mode=req.mode,
                context="",
                draft="",
                original_text="",
                critiques=[],
                consistency_report="",
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
                        result = await provider.generate(
                            (
                                "你是一位专业内容编辑。请分析当前文章/文案，给出3个“改写优化”方向。"
                                "方向必须聚焦结构、标题吸引力、表达清晰度、受众说服力、平台适配、行动引导；"
                                "禁止给出小说续写、剧情转折、角色行动、世界观设定。"
                                "每个方向用一句话概括，只输出JSON字符串数组。"
                            ),
                            f"## 内容 brief\n{_article_brief(req)}\n\n## 当前稿件\n{chapter_content or '(空稿件)'}",
                        )
                    else:
                        result = await provider.generate(
                            (
                                "你是一位专业文学编辑。请分析用户提供的当前章节，给出3个“润色/改写”方向。"
                                "方向必须聚焦文风、语气、节奏、氛围、描写密度、人物心理、对话质感等编辑维度；"
                                "禁止给出续写、转折、新剧情、新角色登场、后续事件安排。"
                                "每个方向用一句话概括，只输出JSON字符串数组。"
                            ),
                            f"## 当前章节\n{chapter_content or '(空章节)'}",
                        )
                    directions = _parse_directions(result)[:3]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'editor', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    yield f"event: enhance_directions\ndata: {json.dumps({'directions': directions}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'message': '请选择润色方向'}, ensure_ascii=False)}\n\n"
                    return
                else:
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

                    if is_article_project:
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
                        system_prompt = _article_system_prompt("改写优化当前文章/文案")
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

                    record_id = await _save_generation_history(writer_content)
                    yield _generation_record_event(record_id)
                    yield f"event: done\ndata: {json.dumps({'message': '润色完成'}, ensure_ascii=False)}\n\n"
                    return

            # ==================== continue 模式（无 expert_id） ====================
            elif req.mode == "continue" and not req.expert_id:
                if not req.turn_direction:
                    # 第一步：分析当前写作，输出 5 个方向建议
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    if is_article_project:
                        result = await provider.generate(
                            (
                                "你是一位专业内容策划。请基于当前文章/文案和 brief，给出5个可执行的内容方向或标题角度。"
                                "建议必须聚焦选题、结构、卖点、受众痛点、平台表达和行动引导；"
                                "禁止小说剧情、角色、续写、转折等叙事建议。每个建议用一句话概括，只输出JSON字符串数组。"
                            ),
                            f"## 内容 brief\n{_article_brief(req)}\n\n## 当前稿件\n{chapter_content or '(空稿件)'}",
                        )
                    else:
                        result = await provider.generate(
                            "你是一位创意写作顾问。分析当前章节的写作进展，给出5个下一步情节发展方向的建议。每个建议用一句话概括，用JSON数组格式输出。",
                            chapter_content or "(空章节)",
                        )
                    suggestions = _parse_directions(result)[:5]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    suggestion_event = "content_suggestions" if is_article_project else "turn_suggestions"
                    yield f"event: {suggestion_event}\ndata: {json.dumps({'suggestions': suggestions}, ensure_ascii=False)}\n\n"
                    done_message = "请选择内容方向" if is_article_project else "请选择转折方向"
                    yield f"event: done\ndata: {json.dumps({'message': done_message}, ensure_ascii=False)}\n\n"
                    return
                else:
                    # 第二步：按选定方向生成/续写
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    if is_article_project:
                        user_prompt = (
                            f"## 内容 brief\n{_article_brief(req)}\n\n"
                            f"## 可用上下文\n{creative_context or '无'}\n\n"
                            f"## 选定内容方向\n{req.turn_direction}\n\n"
                            f"## 用户补充\n{req.user_note or '无'}\n\n"
                            f"## 当前稿件\n{chapter_content or '(空稿件)'}\n\n"
                            "请根据以上信息生成完整文章/文案正文。不要把内容简单接在当前稿件后面，而是围绕方向输出一版完整可用稿。"
                        )
                        system_prompt = _article_system_prompt("生成文章/文案内容")
                        done_message = "内容生成完成"
                    else:
                        user_prompt = f"## 上下文\n{creative_context}\n\n## 续写方向\n{req.turn_direction}\n\n## 用户补充\n{req.user_note or '无'}\n\n## 当前章节（续写接在后面）\n{chapter_content}\n\n请严格按照上下文中的设定续写："
                        system_prompt = "你是一位才华横溢的创意写作大师。根据指定方向续写章节，注意与原文的标点衔接。"
                        done_message = "续写完成"
                    writer_content = ""
                    async for chunk in provider.generate_stream(
                        system_prompt,
                        user_prompt,
                    ):
                        if await _check_cancelled():
                            return
                        writer_content += chunk
                        output_event = "content_output" if is_article_project else "writer_output"
                        yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"

                    record_id = await _save_generation_history(writer_content)
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
                expert_user_prompt = (
                    _article_generate_prompt(req, creative_context, chapter_content)
                    if is_article_project
                    else "继续创作"
                )
                async for chunk in provider.generate_stream(expert.system_prompt, expert_user_prompt):
                    if await _check_cancelled():
                        return
                    if is_writer:
                        writer_content += chunk
                    output_event = "content_output" if is_article_project else "writer_output"
                    yield f"event: {output_event}\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': expert.name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                record_id = await _save_generation_history(writer_content, expert_id=eid if is_writer else None)
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"
                return

            # ==================== summarize 模式 ====================
            elif req.mode == "summarize":
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'reader', 'step': 'running'}, ensure_ascii=False)}\n\n"
                if is_article_project:
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
                    done_message = "受众反馈完成"
                else:
                    summarize_system_prompt = "你是一位普通读者。从阅读体验角度评价以下章节，给出真实感受：哪些段落吸引人、哪里节奏拖沓、角色是否立体、情节是否合理，以及是否与已知设定一致。用中文输出。"
                    summarize_prompt = f"## 上下文\n{creative_context}\n\n## 当前章节内容\n{chapter_content or '(空章节)'}\n\n请从读者视角分析这段内容，严格按照上下文中的设定进行评价："
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
                writer_content = ""
                async for chunk in provider.generate_stream(
                    _article_system_prompt("生成完整文章/文案"),
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
                "mode": req.mode,
                "context": "",
                "draft": "",
                "original_text": "",
                "critiques": [],
                "consistency_report": "",
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
            success = False
            current_stream_node = None  # 追踪当前正在流式输出的节点

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
                        # 发送 skill_pack 事件：告知前端当前专家使用了哪个 skill
                        skill_info = get_skill_for_node(node_name)
                        if skill_info:
                            yield f"event: skill_pack\ndata: {json.dumps({'expert': node_name, 'skill': skill_info.name, 'skill_dir': skill_info.dir_name}, ensure_ascii=False)}\n\n"
                        if node_name in STREAM_NODES:
                            current_stream_node = node_name

                elif kind == "on_chain_end":
                    node_name = event.get("name", "")
                    output = event.get("data", {}).get("output", {})
                    if node_name:
                        yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"
                        if node_name in STREAM_NODES:
                            current_stream_node = None

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
                        report = output.get("consistency_report", "") if isinstance(output, dict) else ""
                        yield f"event: consistency_check\ndata: {json.dumps({'report': report}, ensure_ascii=False)}\n\n"

                    elif node_name == "human_review":
                        record_id = await _save_generation_history(writer_content)
                        yield _generation_record_event(record_id)
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
                record_id = await _save_generation_history(writer_content)
                yield _generation_record_event(record_id)
                yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                return

            record_id = await _save_generation_history(writer_content)
            yield _generation_record_event(record_id)
            yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("生成失败")
            yield f"event: error\ndata: {json.dumps({'message': f'生成失败: {str(e)}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
        try:
            # 获取当前状态
            state = await app.aget_state(config)
            if not state or not state.values:
                yield f"event: error\ndata: {json.dumps({'message': '工作流状态不存在或已过期'}, ensure_ascii=False)}\n\n"
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
                    f"data: {json.dumps({'id': record_id, 'status': 'candidate'}, ensure_ascii=False)}\n\n"
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
                        f"## 一致性检查\n{current_values.get('consistency_report', '') or '无'}"
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
                            skill_info = get_skill_for_node(node_name)
                            if skill_info:
                                yield f"event: skill_pack\ndata: {json.dumps({'expert': node_name, 'skill': skill_info.name, 'skill_dir': skill_info.dir_name}, ensure_ascii=False)}\n\n"
                    elif kind == "on_chain_end":
                        node_name = event.get("name", "")
                        output = event.get("data", {}).get("output", {})
                        if node_name:
                            yield f"event: agent_done\ndata: {json.dumps({'agent': node_name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                        if node_name == "writer":
                            draft = output.get("draft", "") if isinstance(output, dict) else ""
                            revised_content = draft
                            yield f"event: writer_output\ndata: {json.dumps({'content': draft}, ensure_ascii=False)}\n\n"
                        elif node_name == "critic":
                            critiques = output.get("critiques", []) if isinstance(output, dict) else []
                            yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"
                        elif node_name == "consistency_checker":
                            report = output.get("consistency_report", "") if isinstance(output, dict) else ""
                            yield f"event: consistency_check\ndata: {json.dumps({'report': report}, ensure_ascii=False)}\n\n"
                        elif node_name == "editor":
                            edited = output.get("edited_draft", "") if isinstance(output, dict) else ""
                            if edited:
                                revised_content = edited
                            yield f"event: editor_output\ndata: {json.dumps({'content': edited}, ensure_ascii=False)}\n\n"
                        elif node_name == "human_review":
                            record_id = await _save_resume_generation_history(revised_content, {**current_values, **update_state})
                            yield _generation_record_event(record_id)
                            yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                            return

                workflow_state = await app.aget_state(config)
                next_nodes = workflow_state.next if workflow_state else []
                if "human_review" in next_nodes:
                    record_id = await _save_resume_generation_history(revised_content, {**current_values, **update_state})
                    yield _generation_record_event(record_id)
                    yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                    return

                record_id = await _save_resume_generation_history(revised_content, {**current_values, **update_state})
                yield _generation_record_event(record_id)
                yield f"event: done\ndata: {json.dumps({'message': '修订完成'}, ensure_ascii=False)}\n\n"
                return

            # approve: 恢复执行到结束
            await app.aupdate_state(config, {}, as_node="human_review")
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
                            await save_chapter_content(db, chapter, raw_content, source="ai_approve", set_status="draft")
                            await db.commit()
                except Exception:
                    logger.exception("落库失败")
                    yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                    return

            yield f"event: done\ndata: {json.dumps({'message': '已批准，内容已保存'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("恢复工作流失败")
            yield f"event: error\ndata: {json.dumps({'message': f'恢复失败: {str(e)}'}, ensure_ascii=False)}\n\n"

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
            created_at=row[5],
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
