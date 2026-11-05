"""JSONL format and validation for offline chapter evaluation samples."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalSample:
    id: str
    split: str
    source: str
    target_words: int
    context: dict[str, Any]
    task_card: dict[str, Any]
    reference: str | None = None


def _validate_row(value: Any, line_number: int) -> EvalSample:
    if not isinstance(value, dict):
        raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
    sample_id = value.get("id")
    split = value.get("split")
    source = value.get("source")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ValueError(f"第 {line_number} 行缺少非空 id")
    if split not in {"dev", "holdout"}:
        raise ValueError(f"第 {line_number} 行 split 只能是 dev 或 holdout")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"第 {line_number} 行缺少非空 source")
    if not isinstance(value.get("context"), dict):
        raise ValueError(f"第 {line_number} 行 context 必须是对象")
    if not isinstance(value.get("task_card"), dict):
        raise ValueError(f"第 {line_number} 行 task_card 必须是对象")
    target_words = value.get("target_words")
    if not isinstance(target_words, int) or isinstance(target_words, bool) or target_words < 0:
        raise ValueError(f"第 {line_number} 行 target_words 必须是非负整数")
    reference = value.get("reference")
    if reference is not None and not isinstance(reference, str):
        raise ValueError(f"第 {line_number} 行 reference 必须是字符串或 null")
    return EvalSample(
        id=sample_id.strip(),
        split=split,
        source=source.strip(),
        target_words=target_words,
        context=value["context"],
        task_card=value["task_card"],
        reference=reference,
    )


def load_dataset(path: str | Path, *, split: str, limit: int | None = None) -> list[EvalSample]:
    if split not in {"dev", "holdout"}:
        raise ValueError("split 只能是 dev 或 holdout")
    samples: list[EvalSample] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            sample = _validate_row(json.loads(line), line_number)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行 JSON 无效：{exc.msg}") from exc
        if sample.id in seen:
            raise ValueError(f"样本 id 重复：{sample.id}")
        seen.add(sample.id)
        if sample.split == split:
            samples.append(sample)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        samples = samples[:limit]
    return samples


def sample_input_text(sample: EvalSample) -> str:
    return json.dumps(
        {
            "source": sample.source,
            "context": sample.context,
            "task_card": sample.task_card,
            "target_words": sample.target_words,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
