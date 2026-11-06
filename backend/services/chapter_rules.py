"""Pure deterministic checks for chapter task-card compliance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from services.chapter_quality import QualityIssue, count_non_space_chars


_BANNED_PHRASES_PATH = Path(__file__).resolve().parents[1] / "config" / "banned_phrases.json"


def _load_banned_phrases() -> tuple[str, ...]:
    try:
        data = json.loads(_BANNED_PHRASES_PATH.read_text(encoding="utf-8"))
        return tuple(str(value).strip() for value in data.get("phrases", []) if str(value).strip())
    except (OSError, ValueError, TypeError):
        return ()


_DEFAULT_BANNED_PHRASES = _load_banned_phrases()


@dataclass(frozen=True)
class RulesConfig:
    """All thresholds used by deterministic task-card rules."""

    must_include_error_below: float = 0.3
    must_include_warning_below: float = 0.6
    forbidden_max_chars: int = 12
    name_variant_min_length: int = 3
    name_variant_min_occurrences: int = 2
    max_per_1000_chars: float = 2.0
    banned_phrases: tuple[str, ...] = _DEFAULT_BANNED_PHRASES


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        values: Iterable[object] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _unique_2grams(value: str) -> set[str]:
    compact = _compact(value)
    return {compact[index:index + 2] for index in range(max(len(compact) - 1, 0))}


def _task_card_forbidden(task_card: dict) -> list[str]:
    entries = _string_list(task_card.get("forbidden"))
    rules = task_card.get("information_rules")
    if isinstance(rules, dict):
        entries.extend(_string_list(rules.get("forbidden")))
    scenes = task_card.get("scenes")
    if isinstance(scenes, list):
        for scene in scenes:
            if isinstance(scene, dict):
                entries.extend(_string_list(scene.get("must_not_include")))
    return list(dict.fromkeys(entries))


def _character_names(characters: list[dict]) -> list[tuple[str, tuple[str, ...]]]:
    result: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for character in characters or []:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name") or "").strip()
        aliases = tuple(dict.fromkeys(_string_list(character.get("aliases"))))
        if name and name not in seen:
            result.append((name, aliases))
            seen.add(name)
    return result


def _issue(code: str, severity: str, message: str, evidence: str | None = None) -> QualityIssue:
    return QualityIssue(code=code, severity=severity, message=message, evidence=evidence)


def validate_task_card_compliance(
    text: str,
    task_card: dict | None,
    characters: list[dict],
    *,
    config: RulesConfig | None = None,
) -> list[QualityIssue]:
    """Return rule violations for an already-loaded task card and character list.

    This function has no database, LLM, or network access. It only examines its
    arguments and the configurable phrase list loaded by ``RulesConfig``.
    """
    cfg = config or RulesConfig()
    body = text or ""
    compact_body = _compact(body)
    issues: list[QualityIssue] = []
    names = _character_names(characters or [])

    card = task_card if isinstance(task_card, dict) else None
    if card is not None:
        nested = card.get("ChapterTaskCard") or card.get("chapter_task_card")
        if isinstance(nested, dict):
            card = nested
    if card is not None:
        scenes = card.get("scenes")
        if isinstance(scenes, list):
            for scene_index, scene in enumerate(scenes, 1):
                if not isinstance(scene, dict):
                    continue
                for entry in _string_list(scene.get("must_include")):
                    compact_entry = _compact(entry)
                    if not compact_entry:
                        continue
                    if len(compact_entry) < 2:
                        coverage = 1.0 if compact_entry in compact_body else 0.0
                    else:
                        grams = _unique_2grams(compact_entry)
                        coverage = (sum(gram in compact_body for gram in grams) / len(grams)) if grams else 1.0
                    if coverage < cfg.must_include_error_below:
                        severity = "ERROR"
                    elif coverage < cfg.must_include_warning_below:
                        severity = "WARNING"
                    else:
                        continue
                    issues.append(_issue(
                        "RULE_MUST_INCLUDE_MISSING",
                        severity,
                        f"第{scene_index}场景未充分覆盖必含事件：{entry}（覆盖率 {coverage:.2f}）",
                        f"条目：{entry}；2-gram 覆盖率：{coverage:.2f}",
                    ))

        forbidden_entries = _task_card_forbidden(card)
        for entry in forbidden_entries:
            compact_entry = _compact(entry)
            if len(compact_entry) <= cfg.forbidden_max_chars and compact_entry and compact_entry in compact_body:
                issues.append(_issue(
                    "RULE_FORBIDDEN_PRESENT",
                    "ERROR",
                    f"正文包含任务卡禁止内容：{entry}",
                    f"命中：{entry}",
                ))

        forbidden_names = {
            name
            for name, aliases in names
            for name in (name, *aliases)
            if name in forbidden_entries
        }
        for forbidden_name in sorted(forbidden_names):
            if forbidden_name and _compact(forbidden_name) in compact_body:
                issues.append(_issue(
                    "RULE_CHARACTER_FORBIDDEN",
                    "ERROR",
                    f"正文出现任务卡禁止出场的人物：{forbidden_name}",
                    f"命中人物名或别名：{forbidden_name}",
                ))

        if isinstance(scenes, list):
            character_lookup: dict[str, tuple[str, ...]] = {}
            for name, aliases in names:
                candidates = tuple(dict.fromkeys((name, *aliases)))
                for candidate in candidates:
                    character_lookup[candidate] = candidates
            for scene_index, scene in enumerate(scenes, 1):
                if not isinstance(scene, dict):
                    continue
                for expected in _string_list(scene.get("characters")):
                    aliases = character_lookup.get(expected, ())
                    candidates = (expected, *aliases)
                    if not any(_compact(candidate) in compact_body for candidate in candidates if candidate):
                        issues.append(_issue(
                            "RULE_CHARACTER_ABSENT",
                            "WARNING",
                            f"第{scene_index}场景要求出场的人物未在正文中找到：{expected}",
                            f"人物：{expected}；检查名称及可用别名",
                        ))

    # A typo candidate must repeat at least twice and must not be another known name.
    all_known_names = {
        _compact(value)
        for name, aliases in names
        for value in (name, *aliases)
        if len(_compact(value)) >= cfg.name_variant_min_length
    }
    compact_names = [name for name in all_known_names if len(name) >= cfg.name_variant_min_length]
    variant_hits: dict[tuple[str, str], int] = {}
    for canonical in compact_names:
        size = len(canonical)
        for start in range(max(0, len(compact_body) - size + 1)):
            candidate = compact_body[start:start + size]
            if candidate == canonical or candidate in all_known_names:
                continue
            if sum(left != right for left, right in zip(canonical, candidate)) == 1:
                key = (canonical, candidate)
                variant_hits[key] = variant_hits.get(key, 0) + 1
    emitted: set[str] = set()
    for (canonical, candidate), count in sorted(variant_hits.items()):
        if count >= cfg.name_variant_min_occurrences and canonical not in emitted:
            issues.append(_issue(
                "RULE_NAME_VARIANT",
                "WARNING",
                f"发现疑似人名错写：角色「{canonical}」可能被写为「{candidate}」",
                f"候选「{candidate}」出现 {count} 次，与「{canonical}」恰有一字不同",
            ))
            emitted.add(canonical)

    phrase_counts = [(phrase, body.count(phrase)) for phrase in cfg.banned_phrases if phrase and body.count(phrase)]
    total_phrase_hits = sum(count for _, count in phrase_counts)
    non_space_chars = count_non_space_chars(body)
    rate = total_phrase_hits * 1000 / non_space_chars if non_space_chars else 0.0
    if rate > cfg.max_per_1000_chars:
        evidence = "；".join(f"{phrase}：{count}次" for phrase, count in phrase_counts)
        issues.append(_issue(
            "RULE_BANNED_PHRASE",
            "WARNING",
            f"AI 腔词表命中频率偏高：每千字 {rate:.2f} 次（上限 {cfg.max_per_1000_chars:g}）",
            f"{evidence}；合计 {total_phrase_hits} 次 / {non_space_chars} 字",
        ))

    return issues
