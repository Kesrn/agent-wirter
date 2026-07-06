"""Chapter context service — 章节上下文聚合

取代原有分散的 context 加载逻辑，成为 generate / directions / ask 的统一入口。

设计原则：
- 结构化数据（角色、事件、大纲、设定、暗线）直接查原表，不落 project_sources。
- selected_*_ids 作为附加加载，不替代自动聚合。
- fanfic_rules / retrieved_sources 从 project_sources 加载，但默认不注入；
  只有 include_knowledge_sources=True 时才加载资料库内容。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    import uuid

logger = logging.getLogger(__name__)

# ── 返回结构 ──────────────────────────────────────────


@dataclass
class ChapterInfo:
    id: str
    sequence_number: int
    title: str
    content_snippet: str  # 前 500 字符


@dataclass
class OutlineInfo:
    id: str
    sequence_number: int
    title: str
    summary: str
    turning_point: str
    story_arc_id: str | None = None
    arc_position: str | None = None


@dataclass
class CharacterInfo:
    id: str
    name: str
    role_type: str
    profile: str
    faction: str


@dataclass
class CharacterEventInfo:
    id: str
    character_id: str
    character_name: str  # 反查填充
    chapter_sequence_number: int
    event_summary: str
    appearance_type: str
    state_change: str
    importance: int


@dataclass
class HiddenThreadInfo:
    id: str
    name: str
    description: str
    chapter_nums: list[int]


@dataclass
class WorldEntryInfo:
    id: str
    title: str
    category: str
    scope_type: str
    content: str


@dataclass
class FanficRuleInfo:
    id: str
    title: str
    content: str


@dataclass
class RetrievedSourceInfo:
    id: str
    title: str
    snippet: str
    source_type: str = ""
    always_inject: bool = False


@dataclass
class ContextStats:
    characters: int = 0
    events: int = 0
    hidden_threads: int = 0
    world_entries: int = 0
    fanfic_rules: int = 0
    sources: int = 0  # project_sources / 检索命中的资料
    confirmed_memories: int = 0

    @property
    def total(self) -> int:
        return self.characters + self.events + self.hidden_threads + self.world_entries + self.fanfic_rules + self.sources + self.confirmed_memories


@dataclass
class ConfirmedMemoryInfo:
    """已确认的写作记忆（staging CONFIRMED）"""
    id: str
    memory_type: str
    title: str
    description: str = ""
    evidence: str | None = None
    chapter_sequence_number: int | None = None


@dataclass
class PreviousChapterEndingInfo:
    """上一章结尾锚点（K-1：opening_anchor 自动提取）"""
    id: str
    sequence_number: int
    title: str
    ending_text: str


@dataclass
class StoryArcInfo:
    """长线结构信息（K-2：分卷/分幕/弧）"""
    id: str
    arc_type: str               # VOLUME / ACT / ARC
    name: str
    summary: str = ""
    goal: str = ""
    main_conflict: str = ""
    start_chapter: int | None = None
    end_chapter: int | None = None
    arc_position: str | None = None   # 本章在 arc 中的位置（来自 Outline.arc_position）


@dataclass
class ChapterContext:
    """build_chapter_context 的返回值"""

    chapter: ChapterInfo | None = None
    outline: OutlineInfo | None = None
    selected_outlines: list[OutlineInfo] = field(default_factory=list)  # 用户额外选中的参考大纲（不覆盖本章 outline）
    characters: list[CharacterInfo] = field(default_factory=list)
    character_events: list[CharacterEventInfo] = field(default_factory=list)
    hidden_threads: list[HiddenThreadInfo] = field(default_factory=list)
    world_entries: list[WorldEntryInfo] = field(default_factory=list)
    fanfic_rules: list[FanficRuleInfo] = field(default_factory=list)
    retrieved_sources: list[RetrievedSourceInfo] = field(default_factory=list)
    previous_chapters: list[ChapterInfo] = field(default_factory=list)
    confirmed_memories: list[ConfirmedMemoryInfo] = field(default_factory=list)
    previous_chapter_ending: PreviousChapterEndingInfo | None = None  # K-1: opening_anchor
    story_arcs: list[StoryArcInfo] = field(default_factory=list)  # K-2: 当前长线结构
    stats: ContextStats = field(default_factory=ContextStats)

    def to_dict(self) -> dict:
        """转为前端 API 可序列化的 dict"""
        return {
            "chapter": {
                "id": self.chapter.id,
                "sequence_number": self.chapter.sequence_number,
                "title": self.chapter.title,
                "content_snippet": self.chapter.content_snippet,
            }
            if self.chapter
            else None,
            "outline": {
                "id": self.outline.id,
                "title": self.outline.title,
                "summary": self.outline.summary,
                "turning_point": self.outline.turning_point,
            }
            if self.outline
            else None,
            "selected_outlines": [
                {
                    "id": o.id,
                    "title": o.title,
                    "summary": o.summary,
                    "turning_point": o.turning_point,
                    "sequence_number": o.sequence_number,
                }
                for o in self.selected_outlines
            ],
            "characters": [
                {
                    "id": c.id,
                    "name": c.name,
                    "role_type": c.role_type,
                    "profile": c.profile,
                    "faction": c.faction,
                }
                for c in self.characters
            ],
            "character_events": [
                {
                    "id": e.id,
                    "character_id": e.character_id,
                    "character_name": e.character_name,
                    "event_summary": e.event_summary,
                    "appearance_type": e.appearance_type,
                    "state_change": e.state_change,
                    "importance": e.importance,
                }
                for e in self.character_events
            ],
            "hidden_threads": [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "chapter_nums": t.chapter_nums,
                }
                for t in self.hidden_threads
            ],
            "world_entries": [
                {
                    "id": w.id,
                    "title": w.title,
                    "category": w.category,
                    "scope_type": w.scope_type,
                    "content": w.content,
                }
                for w in self.world_entries
            ],
            "fanfic_rules": [
                {"id": r.id, "title": r.title, "content": r.content} for r in self.fanfic_rules
            ],
            "retrieved_sources": [
                {
                    "id": s.id,
                    "title": s.title,
                    "snippet": s.snippet,
                    "source_type": s.source_type,
                }
                for s in self.retrieved_sources
            ],
            "previous_chapters": [
                {
                    "id": c.id,
                    "sequence_number": c.sequence_number,
                    "title": c.title,
                }
                for c in self.previous_chapters
            ],
            "stats": {
                "characters": self.stats.characters,
                "events": self.stats.events,
                "hidden_threads": self.stats.hidden_threads,
                "world_entries": self.stats.world_entries,
                "fanfic_rules": self.stats.fanfic_rules,
                "sources": self.stats.sources,
            },
        }


# ── 主服务函数 ────────────────────────────────────────


async def build_chapter_context(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    chapter_sequence_number: int,
    *,
    intent: str = "generate",
    user_query: str | None = None,
    selected_outline_ids: list[str] | None = None,
    selected_character_ids: list[str] | None = None,
    selected_world_entry_ids: list[str] | None = None,
    selected_hidden_thread_ids: list[str] | None = None,
    include_knowledge_sources: bool = False,
) -> ChapterContext:
    """为指定章节聚合上下文。

    自动加载：
    - 当前章节信息
    - 本章大纲（Outline.sequence_number == chapter_sequence_number）
    - 本章角色事件（CharacterEvent.chapter_sequence_number == chapter_sequence_number）
    - 事件关联角色（反查）
    - 暗线（HiddenThread.chapter_nums 包含当前章节）
    - 全局设定（WorldEntry.scope_type == "global"）
    - 章节设定（WorldEntry.scope_type == "chapter"）
    - 最近 3 章前文
    - 用户通过 selected_*_ids 显式选择的条目（追加不替代）

    资料库 project_sources 默认不注入；调用方显式传 include_knowledge_sources=True 时，
    fanfic_rule / always_inject / 自动检索命中的资料才会进入 prompt。
    """
    import uuid as _uuid

    pid = str(project_id) if isinstance(project_id, _uuid.UUID) else project_id
    seq = chapter_sequence_number

    ctx = ChapterContext()
    stats = ctx.stats

    # ── 当前章节 ──
    from models.chapter import Chapter

    chapter = (
        await db.execute(
            select(Chapter).where(
                Chapter.project_id == pid,
                Chapter.sequence_number == seq,
            )
        )
    ).scalar_one_or_none()

    if chapter:
        content_snippet = (chapter.content or "")[:500]
        ctx.chapter = ChapterInfo(
            id=str(chapter.id),
            sequence_number=chapter.sequence_number,
            title=chapter.title,
            content_snippet=content_snippet,
        )

    # ── 本章大纲 ──
    await _load_outline(db, pid, seq, ctx)

    # ── 长线结构（K-2: 需要本章 outline 的 story_arc_id）──
    await _load_story_arcs(db, pid, seq, ctx)

    # ── 本章角色事件 + 角色 ──
    await _load_character_events(db, pid, seq, ctx, stats)

    # ── 暗线 ──
    await _load_hidden_threads(db, pid, seq, ctx, stats)

    # ── 相关设定 ──
    await _load_world_entries(db, pid, ctx, stats)

    # ── 前文 ──
    await _load_previous_chapters(db, pid, seq, ctx)

    # ── 用户显式选择的条目（追加） ──
    await _load_selected(
        db,
        pid,
        selected_outline_ids,
        selected_character_ids,
        selected_world_entry_ids,
        selected_hidden_thread_ids,
        ctx,
        stats,
        current_seq=seq,
    )

    # ── 接入 project_sources（同人规则 + 检索资料）：默认关闭，避免资料库污染本章 prompt ──
    if include_knowledge_sources:
        await _load_project_sources(db, pid, ctx, stats, user_query=user_query)

    # ── 已确认记忆（H3b）──
    await _load_confirmed_memories(db, pid, seq, ctx, stats)

    # ── 上章结尾锚点（K-1）──
    await _load_previous_chapter_ending(db, pid, seq, ctx)

    logger.info(
        "chapter_context built: project=%s chapter=%d stats=%s intent=%s",
        pid,
        seq,
        stats,
        intent,
    )
    return ctx


# ── 内部加载函数 ──────────────────────────────────────


async def _load_outline(
    db: AsyncSession, project_id: str, seq: int, ctx: ChapterContext
) -> None:
    from models.outline import Outline

    outline = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project_id,
                Outline.sequence_number == seq,
            )
        )
    ).scalar_one_or_none()

    if outline:
        ctx.outline = OutlineInfo(
            id=str(outline.id),
            sequence_number=outline.sequence_number,
            title=outline.title,
            summary=outline.summary or "",
            turning_point=outline.turning_point or "",
            story_arc_id=str(outline.story_arc_id) if outline.story_arc_id else None,
            arc_position=outline.arc_position,
        )


async def _load_story_arcs(
    db: AsyncSession, project_id: str, seq: int, ctx: ChapterContext
) -> None:
    """加载与当前章节相关的长线结构（K-2）。

    查找规则：
    1. 如果本章 outline 有 story_arc_id，直接查该 arc
    2. 同时查 start_chapter <= seq <= end_chapter 的 arc（范围覆盖）
    3. 合并去重，按 order_index 排序
    4. 如果本章 outline 有 arc_position，标记到对应 arc
    """
    from models.story_arc import StoryArc

    arc_ids: set[str] = set()

    # 1. 通过 outline.story_arc_id 查直接关联
    outline_arc_position = None
    outline_arc_id = None
    if ctx.outline and ctx.outline.story_arc_id:
        outline_arc_id = ctx.outline.story_arc_id
        arc_ids.add(outline_arc_id)
        outline_arc_position = ctx.outline.arc_position

    # 2. 通过 start/end_chapter 范围查
    range_result = await db.execute(
        select(StoryArc).where(
            StoryArc.project_id == project_id,
            StoryArc.start_chapter.isnot(None),
            StoryArc.end_chapter.isnot(None),
            StoryArc.start_chapter <= seq,
            StoryArc.end_chapter >= seq,
        )
    )
    range_arcs = range_result.scalars().all()
    for a in range_arcs:
        arc_ids.add(str(a.id))

    if not arc_ids:
        return

    # 3. 查询所有相关 arc，按 order_index 排序
    import uuid as _uuid
    all_result = await db.execute(
        select(StoryArc)
        .where(
            StoryArc.project_id == project_id,
            StoryArc.id.in_([_uuid.UUID(aid) for aid in arc_ids]),
        )
        .order_by(StoryArc.order_index.asc(), StoryArc.created_at.asc())
    )
    arcs = all_result.scalars().all()

    for a in arcs:
        ctx.story_arcs.append(
            StoryArcInfo(
                id=str(a.id),
                arc_type=a.arc_type,
                name=a.name,
                summary=a.summary or "",
                goal=a.goal or "",
                main_conflict=a.main_conflict or "",
                start_chapter=a.start_chapter,
                end_chapter=a.end_chapter,
                arc_position=outline_arc_position if outline_arc_id == str(a.id) else None,
            )
        )


async def _load_character_events(
    db: AsyncSession, project_id: str, seq: int, ctx: ChapterContext, stats: ContextStats
) -> None:
    from models.character_event import CharacterEvent
    from models.character import Character

    events = (
        await db.execute(
            select(CharacterEvent).where(
                CharacterEvent.project_id == project_id,
                CharacterEvent.chapter_sequence_number == seq,
                CharacterEvent.appeared == True,  # noqa: E712
            )
        )
    ).scalars().all()

    if not events:
        return

    # 批量反查角色名和 profile
    character_ids = {str(e.character_id) for e in events}
    char_map: dict[str, tuple[str, str, str, str]] = {}
    if character_ids:
        import uuid as _uuid

        char_ids_uuid = [_uuid.UUID(cid) for cid in character_ids]
        chars = (
            await db.execute(
                select(Character).where(Character.id.in_(char_ids_uuid))
            )
        ).scalars().all()
        for c in chars:
            char_map[str(c.id)] = (
                c.name,
                c.role_type or "supporting",
                c.profile or "",
                c.faction or "",
            )

    for e in events:
        name, role_type, profile, faction = char_map.get(
            str(e.character_id), ("(未知角色)", "supporting", "", "")
        )
        ctx.character_events.append(
            CharacterEventInfo(
                id=str(e.id),
                character_id=str(e.character_id),
                character_name=name,
                chapter_sequence_number=e.chapter_sequence_number,
                event_summary=e.event_summary or "",
                appearance_type=e.appearance_type or "appeared",
                state_change=e.state_change or "",
                importance=e.importance,
            )
        )
    stats.events = len(ctx.character_events)

    # 填充角色列表（从事件反查的角色）
    seen_char_ids: set[str] = set()
    for evt in ctx.character_events:
        if evt.character_id not in seen_char_ids and evt.character_id in char_map:
            seen_char_ids.add(evt.character_id)
            name, role_type, profile, faction = char_map[evt.character_id]
            ctx.characters.append(
                CharacterInfo(
                    id=evt.character_id,
                    name=name,
                    role_type=role_type,
                    profile=profile,
                    faction=faction,
                )
            )
    stats.characters = len(ctx.characters)


async def _load_hidden_threads(
    db: AsyncSession, project_id: str, seq: int, ctx: ChapterContext, stats: ContextStats
) -> None:
    from models.hidden_thread import HiddenThread

    all_threads = (
        await db.execute(
            select(HiddenThread).where(HiddenThread.project_id == project_id)
        )
    ).scalars().all()

    for t in all_threads:
        chapter_nums: list[int] = t.chapter_nums or []
        if seq in chapter_nums:
            ctx.hidden_threads.append(
                HiddenThreadInfo(
                    id=str(t.id),
                    name=t.name,
                    description=t.description or "",
                    chapter_nums=chapter_nums,
                )
            )
    stats.hidden_threads = len(ctx.hidden_threads)


async def _load_world_entries(
    db: AsyncSession, project_id: str, ctx: ChapterContext, stats: ContextStats
) -> None:
    from models.world_entry import WorldEntry

    # 全局设定 + 章节设定（scope_type == "chapter" 兜底：无 chapter_sequence_number 字段时全量加载）
    entries = (
        await db.execute(
            select(WorldEntry).where(
                WorldEntry.project_id == project_id,
                WorldEntry.scope_type.in_(["global", "chapter"]),
            )
        )
    ).scalars().all()

    for we in entries:
        ctx.world_entries.append(
            WorldEntryInfo(
                id=str(we.id),
                title=we.title,
                category=we.category,
                scope_type=we.scope_type,
                content=we.content or "",
            )
        )
    stats.world_entries = len(ctx.world_entries)


async def _load_confirmed_memories(
    db: AsyncSession, project_id: str, current_seq: int, ctx: ChapterContext, stats: ContextStats
) -> None:
    """加载已确认的写作记忆（staging CONFIRMED，chapter_sequence_number <= 当前章节或 NULL）。"""
    from models.writing_memory_staging import WritingMemoryStaging
    from sqlalchemy import or_

    memories = (
        await db.execute(
            select(WritingMemoryStaging).where(
                WritingMemoryStaging.project_id == project_id,
                WritingMemoryStaging.status == "CONFIRMED",
                or_(
                    WritingMemoryStaging.chapter_sequence_number.is_(None),
                    WritingMemoryStaging.chapter_sequence_number <= current_seq,
                ),
            ).order_by(
                WritingMemoryStaging.chapter_sequence_number.desc().nullslast(),
            ).limit(20)
        )
    ).scalars().all()

    for m in memories:
        desc = ""
        payload = m.payload or {}
        if isinstance(payload, dict):
            desc = payload.get("description") or payload.get("content") or payload.get("event_summary") or ""
        ctx.confirmed_memories.append(
            ConfirmedMemoryInfo(
                id=str(m.id),
                memory_type=m.memory_type,
                title=m.title,
                description=desc,
                evidence=m.evidence,
                chapter_sequence_number=m.chapter_sequence_number,
            )
        )
    stats.confirmed_memories = len(ctx.confirmed_memories)


async def _load_previous_chapter_ending(
    db: AsyncSession,
    project_id: str,
    current_seq: int,
    ctx: ChapterContext,
    *,
    max_chars: int = 500,
) -> None:
    """加载上一章结尾作为开篇锚点（K-1: opening_anchor 自动提取）。

    查询规则：
    1. 优先查 sequence_number == current_seq - 1
    2. 如果缺章，回退到 sequence_number < current_seq 的最近一章
    3. 正文为空则不注入
    4. 取 content.strip()[-500:]，不是开头 500 字
    """
    from models.chapter import Chapter

    # 优先查紧邻上一章
    result = await db.execute(
        select(Chapter).where(
            Chapter.project_id == project_id,
            Chapter.sequence_number == current_seq - 1,
            Chapter.content.isnot(None),
            Chapter.content != "",
        )
    )
    chapter = result.scalar_one_or_none()

    # 回退：查最近的前一章
    if chapter is None:
        result = await db.execute(
            select(Chapter).where(
                Chapter.project_id == project_id,
                Chapter.sequence_number < current_seq,
                Chapter.content.isnot(None),
                Chapter.content != "",
            )
            .order_by(Chapter.sequence_number.desc())
            .limit(1)
        )
        chapter = result.scalar_one_or_none()

    if chapter is None or not chapter.content:
        return

    content = chapter.content.strip()
    ending_text = content[-max_chars:] if len(content) > max_chars else content

    ctx.previous_chapter_ending = PreviousChapterEndingInfo(
        id=str(chapter.id),
        sequence_number=chapter.sequence_number,
        title=chapter.title,
        ending_text=ending_text,
    )


async def _load_previous_chapters(
    db: AsyncSession, project_id: str, current_seq: int, ctx: ChapterContext
) -> None:
    from models.chapter import Chapter

    chapters = (
        await db.execute(
            select(Chapter)
            .where(
                Chapter.project_id == project_id,
                Chapter.sequence_number < current_seq,
            )
            .order_by(Chapter.sequence_number.desc())
            .limit(3)
        )
    ).scalars().all()

    for ch in reversed(chapters):
        content_snippet = (ch.content or "")[:300]
        ctx.previous_chapters.append(
            ChapterInfo(
                id=str(ch.id),
                sequence_number=ch.sequence_number,
                title=ch.title,
                content_snippet=content_snippet,
            )
        )


async def _load_selected(
    db: AsyncSession,
    project_id: str,
    selected_outline_ids: list[str] | None,
    selected_character_ids: list[str] | None,
    selected_world_entry_ids: list[str] | None,
    selected_hidden_thread_ids: list[str] | None,
    ctx: ChapterContext,
    stats: ContextStats,
    *,
    current_seq: int | None = None,
) -> None:
    """按用户显式选择的 ID 精确加载条目，追加到已有上下文。

    大纲兜底：生成/续写本章时（current_seq 非 None），只允许本章大纲进入
    selected_outlines，跨章节大纲一律忽略，避免污染本章生成上下文。
    """
    import uuid as _uuid

    # ── 大纲 ──
    if selected_outline_ids:
        from models.outline import Outline

        ids = [_uuid.UUID(x) for x in selected_outline_ids]
        outlines = (
            await db.execute(
                select(Outline).where(
                    Outline.id.in_(ids),
                    Outline.project_id == project_id,
                )
            )
        ).scalars().all()
        existing_outline_ids = {ctx.outline.id} if ctx.outline else set()
        for o in outlines:
            # 兜底：跨章节大纲不注入本章上下文（current_seq 非 None 时）
            if current_seq and o.sequence_number != current_seq:
                continue
            if str(o.id) not in existing_outline_ids:
                ctx.selected_outlines.append(
                    OutlineInfo(
                        id=str(o.id),
                        sequence_number=o.sequence_number,
                        title=o.title,
                        summary=o.summary or "",
                        turning_point=o.turning_point or "",
                    )
                )
                existing_outline_ids.add(str(o.id))

    # ── 角色 ──
    if selected_character_ids:
        from models.character import Character

        ids = [_uuid.UUID(x) for x in selected_character_ids]
        chars = (
            await db.execute(
                select(Character).where(
                    Character.id.in_(ids),
                    Character.project_id == project_id,
                )
            )
        ).scalars().all()
        existing_char_ids = {c.id for c in ctx.characters}
        for c in chars:
            if str(c.id) not in existing_char_ids:
                ctx.characters.append(
                    CharacterInfo(
                        id=str(c.id),
                        name=c.name,
                        role_type=c.role_type,
                        profile=c.profile or "",
                        faction=c.faction or "",
                    )
                )
                existing_char_ids.add(str(c.id))
        stats.characters = len(ctx.characters)

    # ── 世界观 ──
    if selected_world_entry_ids:
        from models.world_entry import WorldEntry

        ids = [_uuid.UUID(x) for x in selected_world_entry_ids]
        entries = (
            await db.execute(
                select(WorldEntry).where(
                    WorldEntry.id.in_(ids),
                    WorldEntry.project_id == project_id,
                )
            )
        ).scalars().all()
        existing_we_ids = {w.id for w in ctx.world_entries}
        for we in entries:
            if str(we.id) not in existing_we_ids:
                ctx.world_entries.append(
                    WorldEntryInfo(
                        id=str(we.id),
                        title=we.title,
                        category=we.category,
                        scope_type=we.scope_type,
                        content=we.content or "",
                    )
                )
                existing_we_ids.add(str(we.id))
        stats.world_entries = len(ctx.world_entries)

    # ── 暗线 ──
    if selected_hidden_thread_ids:
        from models.hidden_thread import HiddenThread

        ids = [_uuid.UUID(x) for x in selected_hidden_thread_ids]
        threads = (
            await db.execute(
                select(HiddenThread).where(
                    HiddenThread.id.in_(ids),
                    HiddenThread.project_id == project_id,
                )
            )
        ).scalars().all()
        existing_ht_ids = {t.id for t in ctx.hidden_threads}
        for t in threads:
            if str(t.id) not in existing_ht_ids:
                ctx.hidden_threads.append(
                    HiddenThreadInfo(
                        id=str(t.id),
                        name=t.name,
                        description=t.description or "",
                        chapter_nums=t.chapter_nums or [],
                    )
                )
                existing_ht_ids.add(str(t.id))
                stats.hidden_threads = len(ctx.hidden_threads)


async def _load_project_sources(
    db: AsyncSession,
    project_id: str,
    ctx: ChapterContext,
    stats: ContextStats,
    *,
    user_query: str | None = None,
) -> None:
    """加载同人规则与自动检索到的资料源。"""
    from models.project_source import ProjectSource
    from sqlalchemy import or_ as _or

    def _escape_like(term: str) -> str:
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _source_snippet(source: ProjectSource) -> str:
        return (source.summary or source.content or "")[:200]

    def _append_fanfic_rule(source: ProjectSource) -> None:
        ctx.fanfic_rules.append(
            FanficRuleInfo(
                id=str(source.id),
                title=source.title,
                content=source.content or source.summary or "",
            )
        )

    def _append_retrieved_source(source: ProjectSource) -> None:
        ctx.retrieved_sources.append(
            RetrievedSourceInfo(
                id=str(source.id),
                title=source.title,
                snippet=_source_snippet(source),
                source_type=source.source_type,
                always_inject=source.always_inject,
            )
        )

    try:
        # 1. fanfic_rule 资料无条件进入“同人规则”
        fanfic_result = await db.execute(
            select(ProjectSource).where(
                ProjectSource.project_id == project_id,
                ProjectSource.source_type == "fanfic_rule",
            )
        )
        fanfic_sources = list(fanfic_result.scalars().all())
        fanfic_seen: set[str] = set()
        for source in fanfic_sources:
            sid = str(source.id)
            if sid in fanfic_seen:
                continue
            fanfic_seen.add(sid)
            _append_fanfic_rule(source)
        stats.fanfic_rules = len(ctx.fanfic_rules)

        # 2. always_inject 的非 fanfic_rule 资料无条件进入检索资料
        always_result = await db.execute(
            select(ProjectSource).where(
                ProjectSource.project_id == project_id,
                ProjectSource.always_inject == True,  # noqa: E712
                ProjectSource.source_type != "fanfic_rule",
            )
        )
        always_sources = list(always_result.scalars().all())

        # 3. 自动检索：按本章角色名 / 章节 / 目标关键词匹配
        search_kws: list[str] = []
        for c in (ctx.characters or [])[:3]:
            if c.name:
                search_kws.append(c.name)
        if ctx.outline:
            search_kws.extend((ctx.outline.title or "").split()[:3])
            search_kws.extend((ctx.outline.summary or "").split()[:4])
        if ctx.chapter:
            search_kws.extend((ctx.chapter.title or "").split()[:3])
            search_kws.extend((ctx.chapter.content_snippet or "").split()[:4])
        if user_query:
            search_kws.extend(user_query.split()[:6])

        auto_hits: list[ProjectSource] = []
        if search_kws:
            conds = []
            for kw in search_kws[:8]:
                escaped = _escape_like(kw)
                conds.extend(
                    [
                        ProjectSource.title.ilike(f"%{escaped}%", escape="\\"),
                        ProjectSource.summary.ilike(f"%{escaped}%", escape="\\"),
                        ProjectSource.content.ilike(f"%{escaped}%", escape="\\"),
                    ]
                )
            if conds:
                hit_result = await db.execute(
                    select(ProjectSource).where(
                        ProjectSource.project_id == project_id,
                        ProjectSource.source_type != "fanfic_rule",
                        ProjectSource.always_inject == False,  # noqa: E712
                        _or(*conds),
                    ).limit(5)
                )
                auto_hits = list(hit_result.scalars().all())

        # 去重合并
        seen: set[str] = set()
        merged: list[ProjectSource] = []
        for source in always_sources + auto_hits:
            sid = str(source.id)
            if sid not in seen:
                seen.add(sid)
                merged.append(source)

        for source in merged:
            _append_retrieved_source(source)
        stats.sources = len(merged)
    except Exception as e:
        # 表可能不存在（首次迁移前），静默跳过
        logger.warning("_load_project_sources skipped: %s", e)


# ── Prompt 格式化 ──────────────────────────────────────


def format_chapter_context_for_prompt(context: ChapterContext) -> str:
    """将 ChapterContext 格式化为 LLM prompt 可用的文本。

    输出 sections（按优先级排列）：
    ## 当前长线结构
    ## 当前章节
    ## 本章大纲
    ## 上章结尾锚点
    ## 明线推进
    ## 本章角色
    ## 本章角色事件
    ## 暗线
    ## 相关设定
    ## 同人规则
    ## 前文摘要
    ## 检索资料
    """
    parts: list[str] = []

    # 当前长线结构（K-2: 分卷/分幕/弧，放在最前给 architect 全局节奏感）
    if context.story_arcs:
        arc_lines = []
        for arc in context.story_arcs:
            line = f"- [{arc.arc_type}] {arc.name}"
            if arc.goal:
                line += f"\n  目标：{arc.goal}"
            if arc.main_conflict:
                line += f"\n  主冲突：{arc.main_conflict}"
            if arc.arc_position:
                line += f"\n  当前阶段：{arc.arc_position}"
            arc_lines.append(line)
        parts.append("## 当前长线结构\n" + "\n".join(arc_lines))

    # 当前章节
    if context.chapter:
        chapter_text = (
            f"## 当前章节\n"
            f"第{context.chapter.sequence_number}章 {context.chapter.title}"
        )
        if context.chapter.content_snippet.strip():
            chapter_text += (
                "\n已有正文片段（仅作本章草稿参考，不作为续写起点）：\n"
                f"{context.chapter.content_snippet}"
            )
        else:
            chapter_text += "\n正文：空，请根据本章资料生成完整章节。"
        parts.append(
            chapter_text
        )

    # 本章大纲
    if context.outline:
        ol = context.outline
        ol_text = f"## 本章大纲\n第{ol.sequence_number}章 {ol.title}"
        if ol.summary:
            ol_text += f"\n概要：{ol.summary}"
        if ol.turning_point:
            ol_text += f"\n转折点：{ol.turning_point}"
        parts.append(ol_text)

    # 上章结尾锚点（K-1: opening_anchor）— 紧跟本章大纲，让 architect 先抓开篇承接
    if context.previous_chapter_ending:
        ending = context.previous_chapter_ending
        parts.append(
            f"## 上章结尾锚点\n"
            f"第{ending.sequence_number}章《{ending.title}》的结尾：\n"
            f"{ending.ending_text}"
        )

    # 明线推进（复用 Outline.turning_point）
    if context.outline and context.outline.turning_point:
        parts.append(f"## 明线推进\n{context.outline.turning_point}")

    # 参考大纲（用户额外选中的其他章节大纲，不覆盖本章大纲）
    if context.selected_outlines:
        ref_lines = []
        for ol in context.selected_outlines:
            ref_lines.append(f"### 第{ol.sequence_number}章 {ol.title}\n概要：{ol.summary or '(无)'}\n转折点：{ol.turning_point or '(无)'}")
        parts.append("## 参考大纲\n" + "\n\n".join(ref_lines))

    # 本章角色
    if context.characters:
        char_lines = []
        for c in context.characters:
            line = f"- {c.name}（{c.role_type}）"
            if c.faction:
                line += f" 阵营：{c.faction}"
            if c.profile:
                line += f"\n  {c.profile}"
            char_lines.append(line)
        parts.append(f"## 本章角色\n" + "\n".join(char_lines))

    # 本章角色事件
    if context.character_events:
        evt_lines = []
        for e in sorted(context.character_events, key=lambda x: x.importance, reverse=True):
            line = f"- {e.character_name}：{e.event_summary}"
            if e.state_change:
                line += f"（变化：{e.state_change}）"
            if e.appearance_type and e.appearance_type != "appeared":
                line += f" [{e.appearance_type}]"
            evt_lines.append(line)
        parts.append(f"## 本章角色事件\n" + "\n".join(evt_lines))

    # 暗线
    if context.hidden_threads:
        ht_lines = []
        for t in context.hidden_threads:
            ht_lines.append(f"- {t.name}：{t.description}")
        parts.append(f"## 暗线\n" + "\n".join(ht_lines))

    # 相关设定
    if context.world_entries:
        we_lines = []
        for w in context.world_entries:
            scope_label = "全局" if w.scope_type == "global" else "章节"
            we_lines.append(f"- [{w.category}][{scope_label}] {w.title}：{w.content}")
        parts.append(f"## 相关设定\n" + "\n".join(we_lines))

    # 同人规则
    if context.fanfic_rules:
        rule_lines = []
        for r in context.fanfic_rules:
            rule_lines.append(f"- {r.title}：{r.content}")
        parts.append(f"## 同人规则\n" + "\n".join(rule_lines))

    # 前文摘要
    if context.previous_chapters:
        prev_lines = []
        for pc in context.previous_chapters:
            prev_lines.append(
                f"### 第{pc.sequence_number}章 {pc.title}\n{pc.content_snippet}"
            )
        parts.append(f"## 前文摘要\n" + "\n\n".join(prev_lines))

    # 检索资料（project_sources RAG 命中）
    if context.retrieved_sources:
        src_lines = []
        for s in context.retrieved_sources:
            src_lines.append(f"- [{s.source_type}] {s.title}：{s.snippet}")
        parts.append(f"## 检索资料\n" + "\n".join(src_lines))

    # 已确认记忆（H3b：staging CONFIRMED，含 PLOT_FACT）
    if context.confirmed_memories:
        mem_lines = []
        for m in context.confirmed_memories:
            source_ch = f"来源：第{m.chapter_sequence_number}章" if m.chapter_sequence_number else "来源：未知"
            line = f"- [{m.memory_type}] {m.title}"
            if m.description:
                line += f"：{m.description}"
            line += f"（{source_ch}）"
            mem_lines.append(line)
        parts.append(f"## 已确认记忆\n" + "\n".join(mem_lines))

    return "\n\n".join(parts) if parts else "(暂无章节上下文)"


# ── 精简统计（供前端 API） ──


def context_to_stats(context: ChapterContext) -> dict:
    """返回精简 stats 结构，供 GET /chapters/{num}/context 使用。"""
    return {
        "stats": {
            "characters": context.stats.characters,
            "events": context.stats.events,
            "hidden_threads": context.stats.hidden_threads,
            "world_entries": context.stats.world_entries,
            "fanfic_rules": context.stats.fanfic_rules,
            "sources": context.stats.sources,
        },
        "chapter_goal": {
            "outline": context.outline.summary if context.outline else "",
            "light_line": context.outline.turning_point if context.outline else "",
        },
    }
