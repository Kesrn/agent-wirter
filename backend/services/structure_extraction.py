"""Structure extraction service layer.

The functions here are route-agnostic: callers inject ``db``, ``project_id``,
``chapter``/``chapters``, an LLM ``provider``, and optionally
``background_tasks`` for embedding refreshes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.llm_provider import LLMProvider
from models.character import Character
from models.character_event import CharacterEvent
from models.character_relation import CharacterRelation
from models.hidden_thread import HiddenThread
from models.outline import Outline
from models.world_entry import WorldEntry


ROLE_TYPES = {"protagonist", "antagonist", "supporting", "minor"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
STRUCTURE_TARGET_KEYS = ("outlines", "characters", "world_entries", "hidden_threads", "character_relations", "character_events")
APPEARANCE_TYPES = {"appeared", "mentioned", "absent"}
CHARACTER_NAME_STOPWORDS = {
    "一个", "一下", "一眼", "不是", "不能", "不会", "什么", "这个", "那个", "这些", "那些", "自己", "所有",
    "众人", "学生", "老师", "主考官", "考官", "工作人员", "观察室", "训练场", "魔法师", "魔法", "火系", "冰系",
}
COMMON_CHINESE_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费"
    "廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程"
)


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
  "characters": [{{"name": "角色名", "role_type": "protagonist|antagonist|supporting|minor", "scope_type": "core|recurring|chapter|cameo", "profile": "人物信息", "faction": "阵营", "appearance_count": 1, "metadata": {{}}}}],
  "world_entries": [{{"title": "设定名", "category": "地点|组织|规则|物品|general", "scope_type": "global|chapter", "content": "设定内容", "rules": {{}}, "confidence": "low|medium|high"}}],
  "hidden_threads": [{{"name": "暗线名", "description": "线索说明", "chapter_nums": [1]}}],
  "character_relations": [{{"source": "角色A", "target": "角色B", "description": "关系描述"}}],
  "character_events": [{{"character_name": "角色名", "sequence_number": 1, "appearance_type": "appeared|mentioned|absent", "event_summary": "本章做了什么", "actions": ["具体行动"], "state_change": "状态变化", "location": "地点", "emotion": "情绪", "importance": 3}}]
}}

规则：
1. 不确定的信息可以省略，不要编造。
2. 章节编号优先使用正文已有编号；无法判断时按输入顺序从 1 开始。
3. character_relations 使用角色名，不要输出数据库 ID。
4. character_events 只记录本章实际出场或被明确提及的角色；未出场角色不要输出。
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
    events = [_normalize_character_event(item) for item in _as_list(raw.get("character_events") or raw.get("events"))]

    return {
        "outlines": [item for item in outlines if item],
        "characters": _dedupe_by_key([item for item in characters if item], "name"),
        "world_entries": _dedupe_by_key([item for item in world_entries if item], "title"),
        "hidden_threads": _dedupe_by_key([item for item in hidden_threads if item], "name"),
        "character_relations": [item for item in relations if item],
        "character_events": _dedupe_character_events([item for item in events if item]),
    }


def build_structure_preview(extraction: dict[str, Any]) -> dict[str, Any]:
    """Build counts and short rows suitable for an import confirmation UI."""
    normalized = normalize_structure_result(extraction)
    return {
        "counts": {key: len(value) for key, value in normalized.items()},
        "outlines": [_pick(item, ("sequence_number", "title", "summary")) for item in normalized["outlines"]],
        "characters": [_pick(item, ("name", "role_type", "scope_type", "faction", "profile")) for item in normalized["characters"]],
        "world_entries": [_pick(item, ("title", "category", "scope_type", "content", "confidence")) for item in normalized["world_entries"]],
        "hidden_threads": [_pick(item, ("name", "description", "chapter_nums")) for item in normalized["hidden_threads"]],
        "character_relations": [_pick(item, ("source", "target", "description")) for item in normalized["character_relations"]],
        "character_events": [_pick(item, ("character_name", "sequence_number", "appearance_type", "event_summary", "state_change")) for item in normalized["character_events"]],
    }


def filter_structure_targets(extraction: dict[str, Any], targets: Iterable[str] | None = None) -> dict[str, Any]:
    """Keep extraction data only for the requested structure groups."""
    normalized = normalize_structure_result(extraction)
    requested = {str(target).strip() for target in (targets or STRUCTURE_TARGET_KEYS)}
    allowed = requested.intersection(STRUCTURE_TARGET_KEYS) or set(STRUCTURE_TARGET_KEYS)
    return {
        key: normalized[key] if key in allowed else []
        for key in STRUCTURE_TARGET_KEYS
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


def build_fallback_structure_from_chapter(
    chapter: Any,
    *,
    existing_characters: Iterable[Any] | None = None,
    targets: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a conservative local extraction when the model returns no structure."""
    allowed = {str(target).strip() for target in (targets or STRUCTURE_TARGET_KEYS)}
    if not allowed.intersection(STRUCTURE_TARGET_KEYS):
        allowed = set(STRUCTURE_TARGET_KEYS)

    sequence = getattr(chapter, "sequence_number", None)
    title = getattr(chapter, "title", None) or "未命名章节"
    content = getattr(chapter, "content", None) or ""
    if isinstance(chapter, dict):
        sequence = chapter.get("sequence_number", sequence)
        title = chapter.get("title", title) or "未命名章节"
        content = chapter.get("content", content) or ""
    sequence = _positive_int(sequence, 1)
    cleaned = _clean_multiline(content)
    summary = _clip(cleaned, 240)

    known_characters = list(existing_characters or [])
    character_items = _fallback_characters(content, known_characters)
    event_items = _fallback_character_events(content, character_items, sequence, str(title))

    extraction = {key: [] for key in STRUCTURE_TARGET_KEYS}
    if "outlines" in allowed and title:
        extraction["outlines"] = [{
            "sequence_number": sequence,
            "title": str(title)[:200],
            "summary": summary or f"第{sequence}章《{title}》",
            "turning_point": None,
        }]
    if "characters" in allowed:
        extraction["characters"] = character_items
    if "character_events" in allowed:
        extraction["character_events"] = event_items

    return normalize_structure_result(extraction)


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
        "character_events": [],
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

    for item in normalized["character_events"]:
        event = await upsert_character_event(db, pid, item, character_map)
        if event is not None:
            result["character_events"].append(event)

    await refresh_character_event_counts(db, result["character_events"], character_map)

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
        character = await find_similar_existing_character(db, project_id, item["name"])
    if character is None:
        character = Character(project_id=project_id, name=item["name"])
        db.add(character)
    character.role_type = item["role_type"]
    character.scope_type = item["scope_type"]
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
    entry.scope_type = item["scope_type"]
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


async def upsert_character_event(
    db: AsyncSession,
    project_id: uuid.UUID,
    item: dict[str, Any],
    character_map: dict[str, Character],
) -> CharacterEvent | None:
    character = character_map.get(item["character_name"])
    if character is None:
        character = await find_similar_existing_character(db, project_id, item["character_name"])
        if character is None:
            character = Character(project_id=project_id, name=item["character_name"], role_type="supporting", scope_type="chapter")
            db.add(character)
            await db.flush()
        character_map[item["character_name"]] = character

    result = await db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == project_id,
            CharacterEvent.character_id == character.id,
            CharacterEvent.chapter_sequence_number == item["sequence_number"],
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        event = CharacterEvent(
            project_id=project_id,
            character_id=character.id,
            chapter_sequence_number=item["sequence_number"],
        )
        db.add(event)

    event.appearance_type = item["appearance_type"]
    event.appeared = item["appearance_type"] == "appeared"
    event.event_summary = item.get("event_summary")
    event.actions = item.get("actions")
    event.state_change = item.get("state_change")
    event.location = item.get("location")
    event.emotion = item.get("emotion")
    event.importance = item["importance"]

    return event


async def find_similar_existing_character(
    db: AsyncSession,
    project_id: uuid.UUID,
    name: str,
) -> Character | None:
    cleaned = _clean_str(name, 100)
    if not cleaned:
        return None
    result = await db.execute(select(Character).where(Character.project_id == project_id))
    candidates = [
        character for character in result.scalars().all()
        if _character_names_look_like_alias(cleaned, str(character.name))
    ]
    return candidates[0] if len(candidates) == 1 else None


def _character_names_look_like_alias(a: str, b: str) -> bool:
    if a == b:
        return True
    if not (2 <= len(a) <= 4 and len(a) == len(b)):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff]+", a + b):
        return False
    if a[0] != b[0]:
        return False
    diff_count = sum(1 for left, right in zip(a, b) if left != right)
    return diff_count == 1


async def refresh_character_event_counts(
    db: AsyncSession,
    events: list[CharacterEvent],
    character_map: dict[str, Character],
) -> None:
    character_ids = {event.character_id for event in events}
    if not character_ids:
        return

    for character in character_map.values():
        if character.id not in character_ids:
            continue
        count_result = await db.execute(
            select(func.count(CharacterEvent.id)).where(
                CharacterEvent.project_id == character.project_id,
                CharacterEvent.character_id == character.id,
                CharacterEvent.appearance_type.in_(["appeared", "mentioned"]),
            )
        )
        character.appearance_count = int(count_result.scalar_one() or 0)


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
        "scope_type": _normalize_character_scope(item.get("scope_type"), role_type),
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
        "scope_type": _normalize_world_scope(item.get("scope_type")),
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


def _normalize_character_event(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    character_name = _clean_str(item.get("character_name") or item.get("name"), 100)
    if not character_name:
        return None
    appearance_type = _clean_str(item.get("appearance_type") or "")
    if not appearance_type:
        appearance_type = "appeared" if item.get("appeared", True) else "absent"
    actions = [
        _clean_str(action, 500)
        for action in _as_list(item.get("actions"))
        if _clean_str(action, 500)
    ]
    return {
        "character_name": character_name,
        "sequence_number": _positive_int(item.get("sequence_number") or item.get("chapter_num"), 1),
        "appearance_type": appearance_type if appearance_type in APPEARANCE_TYPES else "appeared",
        "event_summary": _clean_str(item.get("event_summary") or item.get("summary"), 10000) or None,
        "actions": actions or None,
        "state_change": _clean_str(item.get("state_change"), 10000) or None,
        "location": _clean_str(item.get("location"), 200) or None,
        "emotion": _clean_str(item.get("emotion"), 100) or None,
        "importance": min(5, max(1, _positive_int(item.get("importance"), 3))),
    }


def _fallback_characters(content: str, existing_characters: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for character in existing_characters:
        name = _clean_str(getattr(character, "name", ""), 100)
        if not name or name in seen or name not in content:
            continue
        seen.add(name)
        items.append({
            "name": name,
            "role_type": getattr(character, "role_type", None) or "supporting",
            "scope_type": getattr(character, "scope_type", None) or "recurring",
            "profile": getattr(character, "profile", None) or f"在本章中出现或被提及：{_clip(_sentences_for_name(content, name), 180)}",
            "faction": getattr(character, "faction", None),
            "appearance_count": 1,
            "metadata": getattr(character, "metadata_", None),
        })

    for name, count in _candidate_character_names(content):
        if name in seen:
            continue
        seen.add(name)
        role_type = "protagonist" if not items else "supporting"
        items.append({
            "name": name,
            "role_type": role_type,
            "scope_type": "chapter",
            "profile": f"本章候选角色，文本中出现约 {count} 次。",
            "faction": None,
            "appearance_count": 1,
            "metadata": {"source": "fallback_extraction"},
        })
        if len(items) >= 8:
            break
    return items


def _fallback_character_events(
    content: str,
    characters: list[dict[str, Any]],
    sequence: int,
    title: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for character in characters[:8]:
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        evidence = _sentences_for_name(content, name)
        if not evidence and name not in content:
            continue
        events.append({
            "character_name": name,
            "sequence_number": sequence,
            "appearance_type": "appeared" if evidence else "mentioned",
            "event_summary": _clip(evidence, 260) or f"在第{sequence}章《{title}》中被提及。",
            "actions": _fallback_actions(evidence),
            "state_change": None,
            "location": None,
            "emotion": None,
            "importance": 3,
        })
    return events


def _candidate_character_names(content: str) -> list[tuple[str, int]]:
    patterns = [
        r"([\u4e00-\u9fff]{2,4})(?:说道|说着|问道|喊道|怒道|笑道|冷笑|皱眉|开口|沉声|低声|站|走|看|听|抬头|点头)",
        r"(?:叫做|名叫|名字叫|喊了声)([\u4e00-\u9fff]{2,4})",
        r"([\u4e00-\u9fff]{2,4})[，,](?:你|我|他|她)",
    ]
    counter: Counter[str] = Counter()
    for pattern in patterns:
        for match in re.findall(pattern, content):
            name = _clean_str(match, 100)
            if _is_likely_character_name(name):
                counter[name] += 1
    return counter.most_common(12)


def _is_likely_character_name(name: str) -> bool:
    if len(name) < 2 or len(name) > 3:
        return False
    if name in CHARACTER_NAME_STOPWORDS:
        return False
    if any(word in name for word in CHARACTER_NAME_STOPWORDS):
        return False
    if name[0] not in COMMON_CHINESE_SURNAMES:
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]+", name))


def _normalize_character_scope(value: Any, role_type: str | None = None) -> str:
    scope = _clean_str(value)
    if scope in {"core", "recurring", "chapter", "cameo"}:
        return scope
    return "core" if role_type == "protagonist" else "recurring"


def _normalize_world_scope(value: Any) -> str:
    scope = _clean_str(value)
    return scope if scope in {"global", "chapter"} else "global"


def _sentences_for_name(content: str, name: str, limit: int = 3) -> str:
    chunks = re.split(r"(?<=[。！？!?])\s*|\n+", content)
    sentences = [chunk.strip() for chunk in chunks if name in chunk and chunk.strip()]
    return " ".join(sentences[:limit])


def _fallback_actions(evidence: str) -> list[str] | None:
    if not evidence:
        return None
    actions: list[str] = []
    for marker in ("质问", "反驳", "确认", "宣布", "挑战", "拒绝", "观察", "走出", "站在", "开口", "沉默"):
        if marker in evidence:
            actions.append(marker)
    return actions[:4] or None


def _clean_multiline(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clip(value: str, max_len: int) -> str:
    text = _clean_multiline(value)
    return text[:max_len]


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


def _dedupe_character_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        marker = (str(item.get("character_name") or "").strip(), int(item.get("sequence_number") or 0))
        if marker[0] and marker[1] > 0 and marker not in seen:
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
    "build_fallback_structure_from_chapter",
    "build_structure_extraction_prompt",
    "build_structure_preview",
    "extract_structure_with_provider",
    "filter_structure_targets",
    "normalize_structure_result",
    "parse_structure_json",
    "preview_structure_extraction",
    "upsert_character",
    "upsert_character_event",
    "upsert_character_relation",
    "upsert_hidden_thread",
    "upsert_outline",
    "upsert_world_entry",
]
