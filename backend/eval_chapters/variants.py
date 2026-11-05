"""Named, ablatable chapter-pipeline configurations."""

from __future__ import annotations


VARIANTS: dict[str, dict[str, bool]] = {
    "baseline": {"rules_enabled": False, "fewshot_enabled": False},
    "rules": {"rules_enabled": True, "fewshot_enabled": False},
    "rules_fewshot": {"rules_enabled": True, "fewshot_enabled": True},
}


def get_variants(names: list[str]) -> dict[str, dict[str, bool]]:
    if not names:
        raise ValueError("至少指定一个变体")
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        raise ValueError(f"未知变体：{', '.join(unknown)}")
    if len(set(names)) != len(names):
        raise ValueError("变体名称不能重复")
    return {name: dict(VARIANTS[name]) for name in names}
