"""Select privacy-scoped human edits and render short, deterministic examples."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.generation_record import GenerationRecord
from models.project import Project
from services.edit_metrics_service import ensure_fresh_metrics


_MAX_CANDIDATES = 20  # Match the bounded read-time recompute budget from 1.6.
_DEFAULT_MAX_SNIPPET_CHARS = 300
_DEFAULT_MAX_PROMPT_CHARS = 1200
_HEADER = "【过往人工修改示例（只学习修改方向和文风，不得照抄其中的情节与句子）】"


@dataclass(frozen=True)
class EditPair:
    before: str
    after: str


@dataclass(frozen=True)
class EditExample:
    generation_record_id: str
    edit_ratio: float
    applied_at: datetime | None
    pairs: list[EditPair]
    relevance: float = 0.0


def _without_whitespace(value: str) -> str:
    return "".join((value or "").split())


def _changed_ranges(before: str, after: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    matcher = SequenceMatcher(None, before, after, autojunk=True)
    before_ranges: list[tuple[int, int]] = []
    after_ranges: list[tuple[int, int]] = []
    changed_chars = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before_ranges.append((i1, i2))
        after_ranges.append((j1, j2))
        changed_chars += (i2 - i1) + (j2 - j1)
    return before_ranges, after_ranges, changed_chars


def _snippet(text: str, ranges: list[tuple[int, int]], max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text if max_chars > 0 else ""
    nonempty = [(start, end) for start, end in ranges if end > start]
    if not nonempty:
        return text[:max_chars]
    changed_start = min(start for start, _ in nonempty)
    changed_end = max(end for _, end in nonempty)
    changed_width = min(max_chars, changed_end - changed_start)
    start = max(0, changed_start - max(0, (max_chars - changed_width) // 2))
    end = start + max_chars
    if end > len(text):
        end = len(text)
        start = max(0, end - max_chars)
    return text[start:end]


def _make_pair(before: str, after: str, max_snippet_chars: int) -> EditPair | None:
    before = before or ""
    after = after or ""
    if _without_whitespace(before) == _without_whitespace(after):
        return None
    before_ranges, after_ranges, changed_chars = _changed_ranges(before, after)
    if changed_chars < 10:
        return None
    return EditPair(
        before=_snippet(before, before_ranges, max_snippet_chars),
        after=_snippet(after, after_ranges, max_snippet_chars),
    )


def build_edit_pairs(
    original: str,
    final: str,
    *,
    max_pairs: int = 3,
    max_snippet_chars: int = _DEFAULT_MAX_SNIPPET_CHARS,
) -> list[EditPair]:
    """Align paragraphs and return only substantive changed paragraph pairs."""
    if max_pairs <= 0 or max_snippet_chars <= 0:
        return []
    old_paragraphs = [part for part in (original or "").splitlines() if part.strip()]
    new_paragraphs = [part for part in (final or "").splitlines() if part.strip()]
    matcher = SequenceMatcher(None, old_paragraphs, new_paragraphs, autojunk=True)
    pairs: list[EditPair] = []

    def append_pair(before: str, after: str) -> None:
        if len(pairs) >= max_pairs:
            return
        pair = _make_pair(before, after, max_snippet_chars)
        if pair is not None:
            pairs.append(pair)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_block = old_paragraphs[i1:i2]
        new_block = new_paragraphs[j1:j2]
        if tag == "delete":
            for old in old_block:
                append_pair(old, "")
        elif tag == "insert":
            for new in new_block:
                append_pair("", new)
        else:
            shared = min(len(old_block), len(new_block))
            for index in range(shared):
                append_pair(old_block[index], new_block[index])
            for old in old_block[shared:]:
                append_pair(old, "")
            for new in new_block[shared:]:
                append_pair("", new)
        if len(pairs) >= max_pairs:
            break
    return pairs


def _char_2grams(value: str) -> set[str]:
    normalized = _without_whitespace(value)
    return {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}


def _relevance(query_text: str, pairs: Sequence[EditPair]) -> float:
    query_grams = _char_2grams(query_text)
    if not query_grams:
        return 0.0
    example_grams = _char_2grams("".join(pair.before + pair.after for pair in pairs))
    if not example_grams:
        return 0.0
    return len(query_grams & example_grams) / len(query_grams | example_grams)


def _timestamp_key(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return normalized.timestamp()


async def load_edit_examples(
    db: AsyncSession,
    *,
    owner_id: str,
    project_id: str,
    mode: str | None,
    query_text: str,
    limit: int = 3,
    since: datetime | None = None,
) -> list[EditExample]:
    """Load up to ``limit`` examples from the caller's project and owner only.

    The candidate set is bounded to the 20 newest applied records so stale metric
    caches can be refreshed with 1.6's existing ``max_recompute=20`` budget.
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []
    query = (
        select(GenerationRecord)
        .join(Project, Project.id == GenerationRecord.project_id)
        .where(
            GenerationRecord.project_id == project_id,
            Project.owner_id == str(owner_id),
            GenerationRecord.status == "applied",
        )
    )
    if mode:
        query = query.where(GenerationRecord.mode == mode)
    if since is not None:
        query = query.where(GenerationRecord.updated_at >= since)
    query = query.order_by(GenerationRecord.updated_at.desc(), GenerationRecord.id.asc()).limit(_MAX_CANDIDATES)
    candidates = list((await db.execute(query)).scalars().all())
    if not candidates:
        return []

    await ensure_fresh_metrics(db, candidates, max_recompute=_MAX_CANDIDATES)

    eligible: list[tuple[GenerationRecord, str, str, float]] = []
    min_ratio = float(settings.FEWSHOT_MIN_EDIT_RATIO)
    max_ratio = float(settings.FEWSHOT_MAX_EDIT_RATIO)
    version_ids: set[str] = set()
    for record in candidates:
        metrics = record.human_edit_metrics
        if not isinstance(metrics, dict) or metrics.get("edited") is not True:
            continue
        ratio = metrics.get("edit_ratio")
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            continue
        anchor_id = metrics.get("anchor_version_id")
        human_id = metrics.get("human_version_id")
        if not anchor_id or not human_id or str(record.accepted_version_id) != str(anchor_id):
            continue
        if not (min_ratio <= ratio <= max_ratio):
            continue
        anchor_id, human_id = str(anchor_id), str(human_id)
        version_ids.update((anchor_id, human_id))
        eligible.append((record, anchor_id, human_id, ratio))
    if not eligible:
        return []

    result = await db.execute(
        select(ChapterVersion.id, ChapterVersion.chapter_id, ChapterVersion.content)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .where(
            ChapterVersion.id.in_(version_ids),
            Chapter.project_id == project_id,
        )
    )
    versions = {
        str(row.id): (str(row.chapter_id), row.content or "")
        for row in result
    }

    examples: list[EditExample] = []
    for record, anchor_id, human_id, ratio in eligible:
        anchor = versions.get(anchor_id)
        human = versions.get(human_id)
        if anchor is None or human is None or anchor[0] != human[0]:
            continue
        pairs = build_edit_pairs(anchor[1], human[1])
        if not pairs:
            continue
        examples.append(EditExample(
            generation_record_id=str(record.id),
            edit_ratio=ratio,
            applied_at=record.updated_at,
            pairs=pairs,
            relevance=_relevance(query_text, pairs),
        ))

    examples.sort(key=lambda item: (-item.relevance, -_timestamp_key(item.applied_at), item.generation_record_id))
    return examples[:limit]


def render_edit_examples_block(
    pairs: Sequence[EditPair],
    *,
    max_total_chars: int = _DEFAULT_MAX_PROMPT_CHARS,
) -> str:
    """Render complete examples under a character budget, dropping from the end."""
    if not pairs or max_total_chars < len(_HEADER):
        return ""
    rendered: list[str] = []
    for index, pair in enumerate(pairs, 1):
        rendered.append(
            f"示例{index}\n修改前：{pair.before}\n修改后：{pair.after}"
        )
        candidate = _HEADER + "\n" + "\n\n".join(rendered)
        if len(candidate) > max_total_chars:
            rendered.pop()
            break
    if not rendered:
        return ""
    return _HEADER + "\n" + "\n\n".join(rendered)
