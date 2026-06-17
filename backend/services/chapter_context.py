"""Chapter context service — 章节上下文聚合

取代原有分散的 context 加载逻辑，成为 generate / directions / ask 的统一入口。

设计原则：
- 结构化数据（角色、事件、大纲、设定、暗线）直接查原表，不落 project_sources。
- selected_*_ids 作为附加加载，不替代自动聚合。
- fanfic_rules / retrieved_sources 从 project_sources 加载：always_inject 资料无条件注入，
  其余按本章角色名 / 大纲关键词自动检索匹配。
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
    sources: int = 0  # project_sources / 检索命中的资料

    @property
    def total(self) -> int:
        return self.characters + self.events + self.hidden_threads + self.world_entries + self.sources


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

    project_sources 已接入：always_inject 资料无条件注入，其余按关键词自动检索。
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
    )

    # ── 接入 project_sources（同人规则 + 检索资料） ──
    try:
        from models.project_source import ProjectSource as _PS
        from sqlalchemy import or_ as _or, func as _func

        # 1. always_inject 资料无条件加载
        ai_result = await db.execute(
            select(_PS).where(_PS.project_id == pid, _PS.always_inject == True)
        )
        ai_sources = list(ai_result.scalars().all())

        # 2. 自动检索：按本章角色名 + 大纲标题关键词匹配
        auto_hits: list = []
        search_kws: list[str] = []
        for c in (ctx.characters or [])[:3]:
            if c.name:
                search_kws.append(c.name)
        if ctx.outline and ctx.outline.title:
            search_kws.extend(ctx.outline.title.split()[:3])
        if ctx.chapter and ctx.chapter.title:
            search_kws.extend(ctx.chapter.title.split()[:3])

        if search_kws:
            conds = []
            for kw in search_kws[:5]:
                conds.append(_PS.content.ilike(f"%{kw}%"))
                conds.append(_PS.title.ilike(f"%{kw}%"))
            hit_result = await db.execute(
                select(_PS).where(
                    _PS.project_id == pid,
                    _PS.always_inject == False,
                    _or(*conds),
                ).limit(5)
            )
            auto_hits = list(hit_result.scalars().all())

        # 去重合并
        seen: set[str] = set()
        merged: list = []
        for s in ai_sources + auto_hits:
            sid = str(s.id)
            if sid not in seen:
                seen.add(sid)
                merged.append(s)

        for s in merged:
            snippet = (s.summary or s.content or "")[:200]
            ctx.retrieved_sources.append(
                RetrievedSourceInfo(
                    id=str(s.id),
                    title=s.title,
                    snippet=snippet,
                    source_type=s.source_type,
                    always_inject=s.always_inject,
                )
            )
        stats.sources = len(merged)
    except Exception as e:
        # 表可能不存在（首次迁移前），静默跳过
        logger.warning("_load_knowledge_sources skipped: %s", e)

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
) -> None:
    """按用户显式选择的 ID 精确加载条目，追加到已有上下文。"""
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


# ── Prompt 格式化 ──────────────────────────────────────


def format_chapter_context_for_prompt(context: ChapterContext) -> str:
    """将 ChapterContext 格式化为 LLM prompt 可用的文本。

    输出 sections（按优先级排列）：
    ## 当前章节
    ## 本章大纲
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

    # 当前章节
    if context.chapter:
        parts.append(
            f"## 当前章节\n"
            f"第{context.chapter.sequence_number}章 {context.chapter.title}\n"
            f"{context.chapter.content_snippet}"
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
            "sources": context.stats.sources,
        },
        "chapter_goal": {
            "outline": context.outline.summary if context.outline else "",
            "light_line": context.outline.turning_point if context.outline else "",
        },
    }
