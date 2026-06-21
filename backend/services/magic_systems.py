"""Shared helpers for canonical magic system names."""

from __future__ import annotations

SYSTEM_NORMALIZE_MAP = {
    "寒冰系": "冰系",
    "雷霆系": "雷系",
    "火炎系": "火系",
    "凌水系": "水系",
    "圣光系": "光系",
}

KNOWN_SYSTEM_NAMES = (
    "雷霆系", "寒冰系", "火炎系", "凌水系", "圣光系",
    "暗影系", "召唤系", "空间系", "混沌系", "治愈系", "亡灵系", "心灵系",
    "诅咒系", "植物系", "音系", "毒系", "土系", "火系", "雷系", "冰系",
    "水系", "风系", "光系",
)


def normalize_magic_system_label(name: str) -> str:
    """Normalize obvious system label variants without changing non-system labels."""
    cleaned = name.strip(" ，,。；;：:（）()")
    if cleaned.endswith("魔法"):
        cleaned = cleaned[:-2]
    return SYSTEM_NORMALIZE_MAP.get(cleaned, cleaned)


def canonical_magic_system_name(name: str) -> str | None:
    """Return canonical X系 name from noisy labels such as 雷霆系魔法/雷系星尘."""
    cleaned = (name or "").strip(" ，,。；;：:（）()")
    if cleaned.endswith("魔法"):
        cleaned = cleaned[:-2]
    if cleaned.endswith("星尘"):
        cleaned = cleaned[:-2]
    if cleaned in SYSTEM_NORMALIZE_MAP:
        return SYSTEM_NORMALIZE_MAP[cleaned]
    if cleaned in KNOWN_SYSTEM_NAMES:
        return SYSTEM_NORMALIZE_MAP.get(cleaned, cleaned)
    for raw in KNOWN_SYSTEM_NAMES:
        if raw in cleaned:
            return SYSTEM_NORMALIZE_MAP.get(raw, raw)
    return None
