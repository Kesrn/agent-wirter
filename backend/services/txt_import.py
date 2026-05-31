"""TXT import helpers.

This module intentionally contains no route registration.  Routes can use it to
decode uploaded bytes, split long text into chapter payloads, and build a compact
import summary for UI confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")

CHAPTER_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"(?:第\s*[0-9０-９一二三四五六七八九十百千万两〇零]+\s*[章节卷回部集篇].{0,80})"
    r"|(?:卷\s*[0-9０-９一二三四五六七八九十百千万两〇零]+.{0,80})"
    r"|(?:Chapter\s+\d+.{0,80})"
    r"|(?:CHAPTER\s+\d+.{0,80})"
    r"|(?:\d{1,4}\s*[.、．]\s*\S.{0,80})"
    r"|(?:序章|楔子|引子|正文|终章|尾声|番外(?:\s*\S.{0,60})?|后记|完本感言)"
    r")\s*$"
)


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    had_bom: bool = False


def detect_txt_encoding(data: bytes) -> tuple[str, bool]:
    """Best-effort encoding detection for Chinese TXT uploads."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", True

    for encoding in SUPPORTED_ENCODINGS[1:]:
        try:
            data.decode(encoding)
            return encoding, False
        except UnicodeDecodeError:
            continue

    return "utf-8", False


def decode_txt_bytes(data: bytes, *, return_info: bool = False) -> str | DecodedText:
    """Decode uploaded TXT bytes.

    UTF-8 and UTF-8 BOM are preferred.  GB18030 is attempted before GBK because
    it is a superset and handles more mainland Chinese TXT files.  If all strict
    decoders fail, UTF-8 with replacement is used so routes can return a preview
    instead of crashing on a few bad bytes.
    """
    if not data:
        decoded = DecodedText(text="", encoding="utf-8", had_bom=False)
        return decoded if return_info else decoded.text

    if data.startswith(b"\xef\xbb\xbf"):
        decoded = DecodedText(text=data.decode("utf-8-sig"), encoding="utf-8-sig", had_bom=True)
        return decoded if return_info else decoded.text

    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            decoded = DecodedText(text=data.decode(encoding), encoding=encoding, had_bom=False)
            return decoded if return_info else decoded.text
        except UnicodeDecodeError:
            pass

    decoded = DecodedText(text=data.decode("utf-8", errors="replace"), encoding="utf-8-replace", had_bom=False)
    return decoded if return_info else decoded.text


def normalize_txt_text(text: str) -> str:
    """Normalize line endings and remove common invisible control characters."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "").replace("\x00", "")
    return text.strip()


def _word_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _line_count(text: str) -> int:
    return len([line for line in (text or "").splitlines() if line.strip()])


def _fallback_title(filename: str | None = None) -> str:
    if not filename:
        return "正文"
    stem = Path(filename).stem.strip()
    return stem[:200] or "正文"


def is_chapter_title(line: str) -> bool:
    """Return whether a single line looks like a common chapter heading."""
    candidate = (line or "").strip()
    if not candidate or len(candidate) > 120:
        return False
    return bool(CHAPTER_TITLE_RE.match(candidate))


def clean_chapter_title(title: str) -> str:
    """Remove duplicated chapter numbering from a TXT heading.

    UI already renders the chapter sequence number.  Importing ``第1章 世界大变``
    as the raw title would display as ``第1章 · 第1章 世界大变``.
    """
    candidate = (title or "").strip()
    if not candidate:
        return ""

    patterns = (
        r"^第\s*[0-9０-９一二三四五六七八九十百千万两〇零]+\s*[章节卷回部集篇]\s*[：:、.．\-—]?\s*(.+)$",
        r"^Chapter\s+\d+\s*[：:、.．\-—]?\s*(.+)$",
        r"^CHAPTER\s+\d+\s*[：:、.．\-—]?\s*(.+)$",
        r"^\d{1,4}\s*[.、．]\s*(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, candidate)
        if match:
            cleaned = match.group(1).strip()
            return cleaned[:200] if cleaned else candidate[:200]
    return candidate[:200]


def should_import_preface(preface: str) -> bool:
    """Keep only real prose before the first chapter, not short TXT metadata."""
    content = normalize_txt_text(preface)
    if not content:
        return False
    return _word_count(content) >= 500 or _line_count(content) >= 12


def split_txt_into_chapters(text: str, *, filename: str | None = None) -> list[dict[str, Any]]:
    """Split TXT content into chapter dictionaries.

    Returns items shaped for direct Chapter creation:
    ``{"sequence_number", "title", "content", "word_count"}``.
    If no chapter heading is found, the whole file becomes one chapter.
    """
    normalized = normalize_txt_text(text)
    if not normalized:
        return []

    lines = normalized.split("\n")
    headings: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if is_chapter_title(line):
            headings.append((idx, line.strip()))

    if not headings:
        return [
            {
                "sequence_number": 1,
                "title": _fallback_title(filename),
                "content": normalized,
                "word_count": _word_count(normalized),
            }
        ]

    chapters: list[dict[str, Any]] = []
    preface = "\n".join(lines[: headings[0][0]]).strip()
    if should_import_preface(preface):
        chapters.append(
            {
                "sequence_number": 1,
                "title": "序章",
                "content": preface,
                "word_count": _word_count(preface),
            }
        )

    for heading_index, (line_no, title) in enumerate(headings):
        next_line_no = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(lines)
        content = "\n".join(lines[line_no + 1 : next_line_no]).strip()
        if not content and len(headings) > 1:
            continue
        chapters.append(
            {
                "sequence_number": len(chapters) + 1,
                "title": clean_chapter_title(title) or title[:200],
                "content": content,
                "word_count": _word_count(content),
            }
        )

    if not chapters:
        return [
            {
                "sequence_number": 1,
                "title": _fallback_title(filename),
                "content": normalized,
                "word_count": _word_count(normalized),
            }
        ]

    return chapters


def build_import_meta(
    *,
    filename: str | None = None,
    data: bytes | None = None,
    text: str | None = None,
    chapters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a UI-friendly import summary.

    Routes can pass either raw ``data`` or already decoded ``text``.  When
    chapters are omitted, they are derived from the decoded text.
    """
    decoded: DecodedText | None = None
    if text is None and data is not None:
        decoded = decode_txt_bytes(data, return_info=True)  # type: ignore[assignment]
        text = decoded.text

    text = normalize_txt_text(text or "")
    if chapters is None:
        chapters = split_txt_into_chapters(text, filename=filename)

    return {
        "filename": filename,
        "encoding": decoded.encoding if decoded else None,
        "had_bom": decoded.had_bom if decoded else None,
        "bytes": len(data) if data is not None else None,
        "chars": len(text),
        "word_count": _word_count(text),
        "chapter_count": len(chapters),
        "chapters": [
            {
                "sequence_number": item["sequence_number"],
                "title": item["title"],
                "word_count": item["word_count"],
                "preview": (item.get("content") or "")[:160],
            }
            for item in chapters
        ],
    }


__all__ = [
    "CHAPTER_TITLE_RE",
    "DecodedText",
    "build_import_meta",
    "decode_txt_bytes",
    "detect_txt_encoding",
    "clean_chapter_title",
    "is_chapter_title",
    "normalize_txt_text",
    "should_import_preface",
    "split_txt_into_chapters",
]
