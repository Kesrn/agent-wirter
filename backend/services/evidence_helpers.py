"""Evidence 归一与定位 helper。

evidence 从 list[str] 升级为 list[dict]，区分：
  - exact_quote：完整 substring 命中章节正文（精确引用）
  - summary：LLM 摘要/压缩片段（非逐字，可能带省略号）
  - manual_note：人工手动备注
  - inferred：推断（暂未用，预留）

定位基准是 project_source_chapters（章节正文），不是 chunks。
旧 str evidence 读取时默认当 summary。
"""

from __future__ import annotations

from typing import Any

# evidence dict 必备字段（缺省补全）
EVIDENCE_FIELDS = ("text", "kind", "source_id", "chapter_id", "chapter_no",
                   "chunk_id", "start_offset", "end_offset", "offset_scope", "confidence")


def normalize_evidence_item(
    item: Any,
    *,
    source_id: str | None = None,
    chapter_id: str | None = None,
    chapter_no: int | None = None,
    confidence: float | None = None,
) -> dict:
    """把 str/dict 统一成 evidence dict。str → 默认 summary。

    旧数据是 str，读取层调此函数兼容；新数据是 dict，补全缺失字段。
    """
    if isinstance(item, dict):
        d = dict(item)
        # 补全缺失字段
        d.setdefault("text", "")
        d.setdefault("kind", "summary")
        d.setdefault("source_id", source_id)
        d.setdefault("chapter_id", chapter_id)
        d.setdefault("chapter_no", chapter_no)
        d.setdefault("chunk_id", None)
        d.setdefault("start_offset", None)
        d.setdefault("end_offset", None)
        d.setdefault("offset_scope", None)
        d.setdefault("confidence", confidence)
        return d
    # str → dict（旧数据兼容）
    return {
        "text": str(item) if item is not None else "",
        "kind": "summary",
        "source_id": source_id,
        "chapter_id": chapter_id,
        "chapter_no": chapter_no,
        "chunk_id": None,
        "start_offset": None,
        "end_offset": None,
        "offset_scope": None,
        "confidence": confidence,
    }


def evidence_text(item: Any) -> str:
    """取 evidence 的文本（兼容 str/dict）。"""
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item) if item is not None else ""


def evidence_kind(item: Any) -> str:
    """取 evidence 的 kind（兼容 str，str 默认 summary）。"""
    if isinstance(item, dict):
        return str(item.get("kind", "summary"))
    return "summary"


def evidence_items_equal(a: Any, b: Any) -> bool:
    """按 text 去重（兼容 str/dict）。同一段文本即使 chapter/offset 不同也算重复。"""
    return evidence_text(a) == evidence_text(b)


def make_evidence_ref(
    text: str,
    *,
    chapter: Any = None,
    source_id: str | None = None,
    confidence: float | None = None,
) -> dict:
    """把 LLM 输出的 evidence 字符串转成 evidence dict，并尝试在章节正文定位。

    判定 kind：
      - 完整 text 能在 chapter.content 里 substring 命中 → exact_quote
        + offset_scope=chapter_exact, start/end_offset=命中位置
      - 否则 → summary + offset_scope=None, offset=None
    始终填 chapter_id/chapter_no/source_id。

    chapter 是 ProjectSourceChapter（有 .id/.chapter_no/.content）。
    """
    chapter_id = str(chapter.id) if chapter is not None and getattr(chapter, "id", None) else None
    chapter_no = getattr(chapter, "chapter_no", None) if chapter is not None else None
    chapter_content = getattr(chapter, "content", "") if chapter is not None else ""

    ref = {
        "text": text or "",
        "kind": "summary",
        "source_id": source_id,
        "chapter_id": chapter_id,
        "chapter_no": chapter_no,
        "chunk_id": None,
        "start_offset": None,
        "end_offset": None,
        "offset_scope": None,
        "confidence": confidence,
    }

    # 严格 exact_quote：完整 text 必须 substring 命中章节正文
    if text and chapter_content and text in chapter_content:
        pos = chapter_content.find(text)
        ref["kind"] = "exact_quote"
        ref["start_offset"] = pos
        ref["end_offset"] = pos + len(text)
        ref["offset_scope"] = "chapter_exact"
    # summary 不假装 exact；不存 anchor offset（避免误导，第一版保守）

    return ref


def make_manual_note(text: str, *, source_id: str | None = None) -> dict:
    """构造手动备注 evidence。"""
    return {
        "text": text or "",
        "kind": "manual_note",
        "source_id": source_id,
        "chapter_id": None,
        "chapter_no": None,
        "chunk_id": None,
        "start_offset": None,
        "end_offset": None,
        "offset_scope": None,
        "confidence": 1.0,
    }
