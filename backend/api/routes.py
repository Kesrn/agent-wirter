"""API 路由"""

import json
import logging
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response
from urllib.parse import quote
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.session import get_db
from models.project import Project
from models.expert import Expert
from models.chapter import Chapter
from models.world_entry import WorldEntry
from models.character import Character
from models.character_relation import CharacterRelation
from models.outline import Outline
from models.hidden_thread import HiddenThread
from schemas.api import (
    ProjectCreate, ProjectResponse,
    ExpertCreate, ExpertUpdate, ExpertResponse,
    ChapterCreate, ChapterResponse, ChapterUpdate,
    WorldEntryCreate, WorldEntryUpdate, WorldEntryResponse,
    CharacterCreate, CharacterUpdate, CharacterResponse,
    CharacterRelationCreate, CharacterRelationUpdate, CharacterRelationResponse,
    OutlineCreate, OutlineUpdate, OutlineResponse,
    HiddenThreadCreate, HiddenThreadUpdate, HiddenThreadResponse,
    GenerateRequest, ExpertTestRequest,
    AuthUser,
)
from agents.safety import validate_expert_safety
from agents.expert_templates import BUILTIN_EXPERTS
from agents.llm_provider import get_llm_provider
from agents.workflow import get_creative_app, CreativeState
from api.auth import get_current_user
from api.llm_deps import get_user_llm_config
from api.rate_limiter import agent_limiter
from rag.embedding_service import generate_embedding, _update_embedding_bg

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

WRITER_ROLES = ("writer",)  # 只有 writer 角色的输出会写入章节正文


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
    if req.outline is not None:
        chapter.outline = req.outline
    if req.content is not None:
        chapter.content = req.content
        chapter.word_count = len(req.content or "")
    if req.status is not None:
        chapter.status = req.status

    await db.commit()
    await db.refresh(chapter)
    return chapter


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

@router.post("/projects/{project_id}/chapters/generate")
async def generate_chapter(
    request: Request,
    project_id: str,
    req: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """SSE 流式生成章节内容 — LangGraph Workflow 驱动

    工作流节点：ContextLoader -> Writer -> Critic -> ConsistencyChecker -> Editor -> HumanReview
    使用 interrupt_before=["human_review"] 实现 HITL。

    SSE 事件格式与前端 parseSSEStream 兼容：
    - event: progress          — 进度通知
    - event: writer_output     — Writer 节点完成
    - event: critic_output     — Critic 节点完成
    - event: consistency_check — 一致性检查完成
    - event: editor_output     — Editor 节点完成
    - event: done              — 全部完成
    - event: error             — 出错

    落库策略：
    - writer 角色输出聚合后写入 Chapter.content（仅成功完成后写入）
    - critic/editor 等非 writer 角色输出仅通过 SSE 发给前端，不写入章节正文
    - 异常时不写入部分内容
    """
    # Rate limiting
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

    # 章节定位：严格项目内校验
    target_chapter_id = None  # 用于落库的章节 UUID
    if req.chapter_id:
        cid = _to_uuid(req.chapter_id)
        ch_result = await db.execute(
            select(Chapter).where(Chapter.id == cid, Chapter.project_id == uid)
        )
        if not ch_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="章节不存在")
        target_chapter_id = cid
    elif req.chapter_num is not None:
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
            yield f"event: progress\ndata: {json.dumps({'message': '开始生成', 'mode': req.mode, 'chapter_num': req.chapter_num}, ensure_ascii=False)}\n\n"

            # 获取当前章节内容（enhance/continue/summarize 都需要）
            chapter_content = ""
            if target_chapter_id:
                ch_result = await db.execute(
                    select(Chapter).where(Chapter.id == target_chapter_id)
                )
                chapter_obj = ch_result.scalar_one_or_none()
                if chapter_obj:
                    chapter_content = chapter_obj.content or ""

            llm_config_dict = await get_user_llm_config(user.id, db)
            provider = get_llm_provider(llm_config_dict)

            # ==================== enhance 模式 ====================
            if req.mode == "enhance":
                if not req.enhance_direction:
                    # 第一步：分析章节，输出 3 个润色方向
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'editor', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    result = await provider.generate(
                        "你是一位专业文学编辑。分析以下章节内容，给出3个不同方向的润色建议。每个方向用一句话概括，用JSON数组格式输出。",
                        chapter_content or "(空章节)",
                    )
                    directions = _parse_directions(result)[:3]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'editor', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    yield f"event: enhance_directions\ndata: {json.dumps({'directions': directions}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'message': '请选择润色方向'}, ensure_ascii=False)}\n\n"
                    return
                else:
                    # 第二步：按选定方向润色
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'editor', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    user_prompt = f"## 润色方向\n{req.enhance_direction}\n\n## 用户补充\n{req.user_note or '无'}\n\n## 待润色文本\n{chapter_content}\n\n请润色："
                    writer_content = ""
                    async for chunk in provider.generate_stream(
                        "你是一位专业文学编辑。按指定方向对文本进行润色，保持原有风格和情节不变。",
                        user_prompt,
                    ):
                        writer_content += chunk
                        yield f"event: writer_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'editor', 'step': 'success'}, ensure_ascii=False)}\n\n"

                    # 落库：润色结果替换 Chapter.content
                    if writer_content and target_chapter_id:
                        try:
                            ch_result = await db.execute(
                                select(Chapter).where(Chapter.id == target_chapter_id)
                            )
                            chapter = ch_result.scalar_one_or_none()
                            if chapter:
                                chapter.content = writer_content
                                chapter.word_count = len(writer_content)
                                chapter.status = "draft"
                                await db.commit()
                        except Exception:
                            logger.exception("落库失败")
                            yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                            return

                    yield f"event: done\ndata: {json.dumps({'message': '润色完成'}, ensure_ascii=False)}\n\n"
                    return

            # ==================== continue 模式（无 expert_id） ====================
            elif req.mode == "continue" and not req.expert_id:
                if not req.turn_direction:
                    # 第一步：分析当前写作，输出 5 个转折方向建议
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    result = await provider.generate(
                        "你是一位创意写作顾问。分析当前章节的写作进展，给出5个下一步情节发展方向的建议。每个建议用一句话概括，用JSON数组格式输出。",
                        chapter_content or "(空章节)",
                    )
                    suggestions = _parse_directions(result)[:5]
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"
                    yield f"event: turn_suggestions\ndata: {json.dumps({'suggestions': suggestions}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'message': '请选择转折方向'}, ensure_ascii=False)}\n\n"
                    return
                else:
                    # 第二步：按选定方向续写
                    yield f"event: agent_start\ndata: {json.dumps({'agent': 'writer', 'step': 'running'}, ensure_ascii=False)}\n\n"
                    user_prompt = f"## 续写方向\n{req.turn_direction}\n\n## 用户补充\n{req.user_note or '无'}\n\n## 当前章节（续写接在后面）\n{chapter_content}\n\n请续写："
                    writer_content = ""
                    async for chunk in provider.generate_stream(
                        "你是一位才华横溢的创意写作大师。根据指定方向续写章节，注意与原文的标点衔接。",
                        user_prompt,
                    ):
                        writer_content += chunk
                        yield f"event: writer_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                    yield f"event: agent_done\ndata: {json.dumps({'agent': 'writer', 'step': 'success'}, ensure_ascii=False)}\n\n"

                    # 落库：续写内容追加到 Chapter.content 末尾
                    if writer_content and target_chapter_id:
                        try:
                            ch_result = await db.execute(
                                select(Chapter).where(Chapter.id == target_chapter_id)
                            )
                            chapter = ch_result.scalar_one_or_none()
                            if chapter:
                                existing = chapter.content or ""
                                chapter.content = existing + writer_content
                                chapter.word_count = len(chapter.content)
                                chapter.status = "draft"
                                await db.commit()
                        except Exception:
                            logger.exception("落库失败")
                            yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                            return

                    yield f"event: done\ndata: {json.dumps({'message': '续写完成'}, ensure_ascii=False)}\n\n"
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
                async for chunk in provider.generate_stream(expert.system_prompt, "继续创作"):
                    if is_writer:
                        writer_content += chunk
                    yield f"event: writer_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': expert.name, 'step': 'success'}, ensure_ascii=False)}\n\n"

                # 落库
                if writer_content and target_chapter_id:
                    try:
                        ch_result = await db.execute(
                            select(Chapter).where(Chapter.id == target_chapter_id)
                        )
                        chapter = ch_result.scalar_one_or_none()
                        if chapter:
                            chapter.content = writer_content
                            chapter.word_count = len(writer_content)
                            chapter.status = "draft"
                            await db.commit()
                    except Exception:
                        logger.exception("落库失败")
                        yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                        return

                yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"
                return

            # ==================== summarize 模式 ====================
            elif req.mode == "summarize":
                yield f"event: agent_start\ndata: {json.dumps({'agent': 'reader', 'step': 'running'}, ensure_ascii=False)}\n\n"
                async for chunk in provider.generate_stream(
                    "你是一位普通读者。从阅读体验角度评价以下章节，给出真实感受：哪些段落吸引人、哪里节奏拖沓、角色是否立体、情节是否合理。用中文输出。",
                    chapter_content or "(空章节)",
                ):
                    yield f"event: writer_output\ndata: {json.dumps({'token': chunk}, ensure_ascii=False)}\n\n"
                yield f"event: agent_done\ndata: {json.dumps({'agent': 'reader', 'step': 'success'}, ensure_ascii=False)}\n\n"
                # 不落库（只是反馈，不改原文）
                yield f"event: done\ndata: {json.dumps({'message': '读者反馈完成'}, ensure_ascii=False)}\n\n"
                return

            # ==================== full_pipeline 模式 ====================
            # 查询项目所有启用的专家（含内置和自定义）
            exp_result = await db.execute(
                select(Expert).where(Expert.project_id == uid, Expert.is_enabled == True)
            )
            enabled_experts = exp_result.scalars().all()

            app = get_creative_app(enabled_experts=enabled_experts)
            thread_id = f"{uid}:{target_chapter_id or 'no-chapter'}:{uuid.uuid4().hex[:8]}"

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
                "chapter_id": str(target_chapter_id) if target_chapter_id else "",
                "mode": req.mode,
                "context": "",
                "draft": "",
                "original_text": "",
                "critiques": [],
                "consistency_report": "",
                "edited_draft": "",
                "revision_count": 0,
                "next_agent": "context_loader",
                "writer_prompt": writer_prompt,
                "critic_prompt": critic_prompt,
                "editor_prompt": "",
                "consistency_prompt": consistency_prompt,
                "llm_config": llm_config_dict,
                "selected_outline_ids": req.selected_outline_ids or [],
                "selected_character_ids": req.selected_character_ids or [],
                "selected_world_entry_ids": req.selected_world_entry_ids or [],
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
                kind = event.get("event")

                if kind == "on_chain_start":
                    node_name = event.get("name", "")
                    if node_name in NODE_EVENT_MAP:
                        yield f"event: progress\ndata: {json.dumps({'message': f'{node_name} 节点执行中'}, ensure_ascii=False)}\n\n"
                    if node_name:
                        yield f"event: agent_start\ndata: {json.dumps({'agent': node_name, 'step': 'running'}, ensure_ascii=False)}\n\n"
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

            success = True

            # 落库
            if writer_content and target_chapter_id:
                try:
                    ch_result = await db.execute(
                        select(Chapter).where(Chapter.id == target_chapter_id)
                    )
                    chapter = ch_result.scalar_one_or_none()
                    if chapter:
                        chapter.content = writer_content
                        chapter.word_count = len(writer_content)
                        chapter.status = "draft"
                        await db.commit()
                except Exception:
                    logger.exception("落库失败")
                    success = False
                    yield f"event: error\ndata: {json.dumps({'message': '保存失败'}, ensure_ascii=False)}\n\n"
                    return

            if success:
                yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("生成失败")
            yield f"event: error\ndata: {json.dumps({'message': f'生成失败: {str(e)}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ==================== 工作流恢复（HITL） ====================

@router.post("/projects/{project_id}/chapters/resume")
async def resume_chapter_generation(
    project_id: str,
    thread_id: str = Query(..., description="HITL 暂停时返回的 thread_id"),
    action: str = Query(default="approve", pattern=r"^(approve|reject|revise)$"),
    feedback: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """恢复 HITL 暂停的工作流

    用户对 Editor 输出做出决策后，恢复工作流执行：
    - approve: 结束流程，落库最终内容
    - reject: 终止流程，不落库
    - revise: 将反馈注入状态，重新从 Writer 开始
    """
    # Rate limiting
    agent_limiter.check(f"generate:{user.id}")

    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)

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

            if action == "reject":
                yield f"event: done\ndata: {json.dumps({'message': '已拒绝，流程终止'}, ensure_ascii=False)}\n\n"
                return

            if action == "revise":
                # 更新状态，增加修订计数并注入反馈
                current_values = state.values
                revision_count = current_values.get("revision_count", 0) + 1
                if revision_count > 3:
                    yield f"event: done\ndata: {json.dumps({'message': '修订次数已达上限，流程终止'}, ensure_ascii=False)}\n\n"
                    return

                update_state = {
                    "revision_count": revision_count,
                    "next_agent": "writer",
                }
                if feedback:
                    update_state["critiques"] = current_values.get("critiques", []) + [f"[用户反馈] {feedback}"]

                await app.aupdate_state(config, update_state, as_node="human_review")

                # 继续流式执行
                async for event in app.astream_events(None, config=config, version="v2"):
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

                        if node_name == "writer":
                            draft = output.get("draft", "") if isinstance(output, dict) else ""
                            yield f"event: writer_output\ndata: {json.dumps({'content': draft}, ensure_ascii=False)}\n\n"
                        elif node_name == "critic":
                            critiques = output.get("critiques", []) if isinstance(output, dict) else []
                            yield f"event: critic_output\ndata: {json.dumps({'critiques': critiques}, ensure_ascii=False)}\n\n"
                        elif node_name == "consistency_checker":
                            report = output.get("consistency_report", "") if isinstance(output, dict) else ""
                            yield f"event: consistency_check\ndata: {json.dumps({'report': report}, ensure_ascii=False)}\n\n"
                        elif node_name == "editor":
                            edited = output.get("edited_draft", "") if isinstance(output, dict) else ""
                            yield f"event: editor_output\ndata: {json.dumps({'content': edited}, ensure_ascii=False)}\n\n"
                        elif node_name == "human_review":
                            yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
                            return

                yield f"event: done\ndata: {json.dumps({'message': '修订完成'}, ensure_ascii=False)}\n\n"
                return

            # approve: 恢复执行到结束
            await app.aupdate_state(config, {"next_agent": "__end__"}, as_node="human_review")
            current_values = state.values
            # 优先使用 edited_draft（经编辑润色），若无则使用 draft（原始创作）
            final_content = current_values.get("edited_draft", "") or current_values.get("draft", "")
            target_chapter_id = current_values.get("chapter_id", "")

            # 落库
            if final_content and target_chapter_id:
                try:
                    ch_result = await db.execute(
                        select(Chapter).where(Chapter.id == _to_uuid(target_chapter_id))
                    )
                    chapter = ch_result.scalar_one_or_none()
                    if chapter:
                        chapter.content = final_content
                        chapter.word_count = len(final_content)
                        chapter.status = "draft"
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
