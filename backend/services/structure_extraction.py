"""Structure extraction service layer.

The functions here are route-agnostic: callers inject ``db``, ``project_id``,
``chapter``/``chapters``, an LLM ``provider``, and optionally
``background_tasks`` for embedding refreshes.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.llm_provider import LLMProvider
from models.character import Character
from models.character_relation import CharacterRelation
from models.hidden_thread import HiddenThread
from models.outline import Outline
from models.world_entry import WorldEntry


ROLE_TYPES = {"protagonist", "antagonist", "supporting", "minor"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}


def build_structure_extraction_prompt(
    *,
    chapter: Any | None = None,
    chapters: Iterable[Any] | None = None,
    project_title: str | None = None,
    extra_context: str | None = None,
    max_chars: int = 24000,
) -> tuple[str, str]:
    """Build system/user prompts for extracting reusable story structure."""
    source_parts: list[str] = []
    if chapter is not None:
        source_parts.append(_chapter_to_prompt_block(chapter))
    if chapters is not None:
        source_parts.extend(_chapter_to_prompt_block(item) for item in chapters)

    source = "\n\n".join(part for part in source_parts if part.strip()).strip()
    if len(source) > max_chars:
        source = source[:max_chars] + "\n\n（后文因长度限制已截断）"

    system_prompt = (
        "你是长篇小说结构提炼助手。请从正文中提炼可写入项目资料库的结构化信息，"
        "只输出严格 JSON，不要 Markdown，不要解释。"
    )
    user_prompt = f"""
项目：{project_title or "未命名项目"}

额外上下文：
{extra_context or "无"}

待提炼正文：
{source or "（空）"}

请输出 JSON 对象，字段如下：
{{
  "outlines": [{{"sequence_number": 1, "title": "章节标题", "summary": "剧情摘要", "turning_point": "转折点"}}],
  "characters": [{{"name": "角色名", "role_type": "protagonist|antagonist|supporting|minor", "profile": "人物信息", "faction": "阵营", "appearance_count": 1, "metadata": {{}}}}],
  "world_entries": [{{"title": "设定名", "category": "地点|组织|规则|物品|general", "content": "设定内容", "rules": {{}}, "confidence": "low|medium|high"}}],
  "hidden_threads": [{{"name": "暗线名", "description": "线索说明", "chapter_nums": [1]}}],
  "character_relations": [{{"source": "角色A", "target": "角色B", "description": "关系描述"}}]
}}

规则：
1. 不确定的信息可以省略，不要编造。
2. 章节编号优先使用正文已有编号；无法判断时按输入顺序从 1 开始。
3. character_relations 使用角色名，不要输出数据库 ID。
""".strip()
    return system_prompt, user_prompt


async def extract_structure_with_provider(
    provider: LLMProvider,
    *,
    chapter: Any | None = None,
    chapters: Iterable[Any] | None = None,
    project_title: str | None = None,
    extra_context: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Run the provider and return a normalized extraction result."""
    system_prompt, user_prompt = build_structure_extraction_prompt(
        chapter=chapter,
        chapters=chapters,
        project_title=project_title,
        extra_context=extra_context,
    )
    raw_text = await provider.generate(system_prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
    parsed = parse_structure_json(raw_text)
    return normalize_structure_result(parsed)


def parse_structure_json(text: str) -> dict[str, Any]:
    """Parse messy LLM JSON output into a dict.

    Handles fenced code blocks, leading commentary, trailing commentary, and
    common full-width punctuation variants.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("，", ",").replace("：", ":")
    decoder = json.JSONDecoder()
    for idx, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[idx:])
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                return {"outlines": value}
        except json.JSONDecodeError:
            continue
    return {}


def normalize_structure_result(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize extraction JSON to the service schema and drop unusable items."""
    raw = raw or {}
    outlines = [_normalize_outline(item, idx + 1) for idx, item in enumerate(_as_list(raw.get("outlines")))]
    characters = [_normalize_character(item) for item in _as_list(raw.get("characters"))]
    world_entries = [_normalize_world_entry(item) for item in _as_list(raw.get("world_entries") or raw.get("world"))]
    hidden_threads = [_normalize_hidden_thread(item) for item in _as_list(raw.get("hidden_threads"))]
    relations = [_normalize_relation(item) for item in _as_list(raw.get("character_relations") or raw.get("relations"))]

    return {
        "outlines": [item for item in outlines if item],
        "characters": _dedupe_by_key([item for item in characters if item], "name"),
        "world_entries": _dedupe_by_key([item for item in world_entries if item], "title"),
        "hidden_threads": _dedupe_by_key([item for item in hidden_threads if item], "name"),
        "character_relations": [item for item in relations if item],
    }


def build_structure_preview(extraction: dict[str, Any]) -> dict[str, Any]:
    """Build counts and short rows suitable for an import confirmation UI."""
    normalized = normalize_structure_result(extraction)
    return {
        "counts": {key: len(value) for key, value in normalized.items()},
        "outlines": [_pick(item, ("sequence_number", "title", "summary")) for item in normalized["outlines"]],
        "characters": [_pick(item, ("name", "role_type", "faction", "profile")) for item in normalized["characters"]],
        "world_entries": [_pick(item, ("title", "category", "content", "confidence")) for item in normalized["world_entries"]],
        "hidden_threads": [_pick(item, ("name", "description", "chapter_nums")) for item in normalized["hidden_threads"]],
        "character_relations": [_pick(item, ("source", "target", "description")) for item in normalized["character_relations"]],
    }


async def preview_structure_extraction(
    provider: LLMProvider,
    *,
    chapter: Any | None = None,
    chapters: Iterable[Any] | None = None,
    project_title: str | None = None,
    extra_context: str | None = None,
) -> dict[str, Any]:
    """Run extraction and return both normalized data and a compact preview."""
    extraction = await extract_structure_with_provider(
        provider,
        chapter=chapter,
        chapters=chapters,
        project_title=project_title,
        extra_context=extra_context,
    )
    return {"extraction": extraction, "preview": build_structure_preview(extraction)}


async def apply_structure_extraction(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    extraction: dict[str, Any],
    *,
    background_tasks: Any | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Upsert normalized extraction data into project structure tables."""
    pid = _to_uuid(project_id)
    normalized = normalize_structure_result(extraction)

    result: dict[str, list[Any]] = {
        "outlines": [],
        "characters": [],
        "world_entries": [],
        "hidden_threads": [],
        "character_relations": [],
    }

    for item in normalized["outlines"]:
        result["outlines"].append(await upsert_outline(db, pid, item))

    for item in normalized["characters"]:
        character = await upsert_character(db, pid, item)
        result["characters"].append(character)

    for item in normalized["world_entries"]:
        entry = await upsert_world_entry(db, pid, item)
        result["world_entries"].append(entry)

    for item in normalized["hidden_threads"]:
        result["hidden_threads"].append(await upsert_hidden_thread(db, pid, item))

    await db.flush()

    character_map = {str(item.name).strip(): item for item in result["characters"]}
    if normalized["character_relations"]:
        existing_chars = await db.execute(select(Character).where(Character.project_id == pid))
        for character in existing_chars.scalars().all():
            character_map.setdefault(str(character.name).strip(), character)

    for item in normalized["character_relations"]:
        relation = await upsert_character_relation(db, pid, item, character_map)
        if relation is not None:
            result["character_relations"].append(relation)

    if commit:
        await db.commit()
        for items in result.values():
            for item in items:
                await db.refresh(item)

    for character in result["characters"]:
        _schedule_embedding(background_tasks, Character, character.id, f"{character.name} {character.profile or ''}")
    for entry in result["world_entries"]:
        _schedule_embedding(background_tasks, WorldEntry, entry.id, f"{entry.title} {entry.content}")

    return {
        "counts": {key: len(value) for key, value in result.items()},
        "items": result,
    }


async def upsert_outline(db: AsyncSession, project_id: uuid.UUID, item: dict[str, Any]) -> Outline:
    result = await db.execute(
        select(Outline).where(Outline.project_id == project_id, Outline.sequence_number == item["sequence_number"])
    )
    outline = result.scalar_one_or_none()
    if outline is None:
        outline = Outline(project_id=project_id, sequence_number=item["sequence_number"], title=item["title"])
        db.add(outline)
    outline.title = item["title"]
    outline.summary = item.get("summary")
    outline.turning_point = item.get("turning_point")
    return outline


async def upsert_character(db: AsyncSession, project_id: uuid.UUID, item: dict[str, Any]) -> Character:
    result = await db.execute(select(Character).where(Character.project_id == project_id, Character.name == item["name"]))
    character = result.scalar_one_or_none()
    if character is None:
        character = Character(project_id=project_id, name=item["name"])
        db.add(character)
    character.role_type = item["role_type"]
    character.profile = item.get("profile")
    character.faction = item.get("faction")
    character.appearance_count = max(character.appearance_count or 0, item.get("appearance_count") or 0)
    character.metadata_ = item.get("metadata")
    return character


async def upsert_world_entry(db: AsyncSession, project_id: uuid.UUID, item: dict[str, Any]) -> WorldEntry:
    result = await db.execute(select(WorldEntry).where(WorldEntry.project_id == project_id, WorldEntry.title == item["title"]))
    entry = result.scalar_one_or_none()
    if entry is None:
        entry = WorldEntry(project_id=project_id, title=item["title"])
        db.add(entry)
    entry.category = item["category"]
    entry.content = item["content"]
    entry.rules = item.get("rules")
    entry.confidence = item["confidence"]
    return entry


async def upsert_hidden_thread(db: AsyncSession, project_id: uuid.UUID, item: dict[str, Any]) -> HiddenThread:
    result = await db.execute(select(HiddenThread).where(HiddenThread.project_id == project_id, HiddenThread.name == item["name"]))
    thread = result.scalar_one_or_none()
    if thread is None:
        thread = HiddenThread(project_id=project_id, name=item["name"])
        db.add(thread)
    thread.description = item.get("description")
    thread.chapter_nums = item.get("chapter_nums")
    return thread


async def upsert_character_relation(
    db: AsyncSession,
    project_id: uuid.UUID,
    item: dict[str, Any],
    character_map: dict[str, Character],
) -> CharacterRelation | None:
    source = character_map.get(item["source"])
    target = character_map.get(item["target"])
    if source is None or target is None or source.id == target.id:
        return None

    result = await db.execute(
        select(CharacterRelation).where(
            CharacterRelation.project_id == project_id,
            CharacterRelation.source_character_id == source.id,
            CharacterRelation.target_character_id == target.id,
        )
    )
    relation = result.scalar_one_or_none()
    if relation is None:
        relation = CharacterRelation(
            project_id=project_id,
            source_character_id=source.id,
            target_character_id=target.id,
            description=item["description"],
        )
        db.add(relation)
    else:
        relation.description = item["description"]
    return relation


def _chapter_to_prompt_block(chapter: Any) -> str:
    sequence = getattr(chapter, "sequence_number", None)
    title = getattr(chapter, "title", None)
    content = getattr(chapter, "content", None)
    if isinstance(chapter, dict):
        sequence = chapter.get("sequence_number", sequence)
        title = chapter.get("title", title)
        content = chapter.get("content", content)
    heading = f"### 第{sequence or '?'}章 {title or '未命名'}"
    return f"{heading}\n{content or ''}".strip()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_str(value: Any, max_len: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len] if max_len else text


def _normalize_outline(item: Any, fallback_sequence: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = _clean_str(item.get("title") or item.get("chapter_title"), 200)
    if not title:
        return None
    return {
        "sequence_number": _positive_int(item.get("sequence_number") or item.get("chapter_num"), fallback_sequence),
        "title": title,
        "summary": _clean_str(item.get("summary"), 10000) or None,
        "turning_point": _clean_str(item.get("turning_point"), 5000) or None,
    }


def _normalize_character(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = _clean_str(item.get("name"), 100)
    if not name:
        return None
    role_type = _clean_str(item.get("role_type") or "supporting")
    return {
        "name": name,
        "role_type": role_type if role_type in ROLE_TYPES else "supporting",
        "profile": _clean_str(item.get("profile") or item.get("description"), 5000) or None,
        "faction": _clean_str(item.get("faction"), 100) or None,
        "appearance_count": _positive_int(item.get("appearance_count"), 1),
        "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else None,
    }


def _normalize_world_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = _clean_str(item.get("title") or item.get("name"), 200)
    content = _clean_str(item.get("content") or item.get("description"), 10000)
    if not title or not content:
        return None
    confidence = _clean_str(item.get("confidence") or "medium")
    return {
        "title": title,
        "category": _clean_str(item.get("category") or "general", 50) or "general",
        "content": content,
        "rules": item.get("rules") if isinstance(item.get("rules"), dict) else None,
        "confidence": confidence if confidence in CONFIDENCE_LEVELS else "medium",
    }


def _normalize_hidden_thread(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = _clean_str(item.get("name") or item.get("title"), 200)
    if not name:
        return None
    nums = item.get("chapter_nums") or item.get("chapters") or []
    return {
        "name": name,
        "description": _clean_str(item.get("description"), 10000) or None,
        "chapter_nums": sorted({_positive_int(num, 0) for num in _as_list(nums) if _positive_int(num, 0) > 0}) or None,
    }


def _normalize_relation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    source = _clean_str(item.get("source") or item.get("source_name"), 100)
    target = _clean_str(item.get("target") or item.get("target_name"), 100)
    description = _clean_str(item.get("description"), 500)
    if not source or not target or not description:
        return None
    return {"source": source, "target": target, "description": description}


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _dedupe_by_key(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        marker = str(item.get(key) or "").strip()
        if marker and marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys}


def _to_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _schedule_embedding(background_tasks: Any | None, model_class: Any, entry_id: uuid.UUID, text: str) -> None:
    if background_tasks is None:
        return
    from rag.embedding_service import _update_embedding_bg

    background_tasks.add_task(_update_embedding_bg, model_class, entry_id, text)


__all__ = [
    "apply_structure_extraction",
    "build_structure_extraction_prompt",
    "build_structure_preview",
    "extract_structure_with_provider",
    "normalize_structure_result",
    "parse_structure_json",
    "preview_structure_extraction",
    "upsert_character",
    "upsert_character_relation",
    "upsert_hidden_thread",
    "upsert_outline",
    "upsert_world_entry",
]
