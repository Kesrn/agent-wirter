#!/usr/bin/env python3
"""评测样本格式定义（阶段零：评测集构建）。

定义检索评测数据集的统一 JSON 契约，供数据集生成器（generate_regression.py）、
人工查询集（datasets/natural_queries.json）与基线运行器（run_baseline.py）共用。

样本契约（顶层为 JSON 数组，或 {"meta": {...}, "samples": [...]}）：

    {
      "query_id": "reg-001",
      "category": "character_first_appearance|ability_query|timeline_event|world_rule|source_chunk|no_result|...",
      "query": "用户可能输入的问句",
      "expected": {
        "evidence_type": "character_profile|ability_profile|event_timeline|world_rule|source_chunk",
        "chapter_no": 12,                # 可选，期望证据所在章节
        "source_ids": ["uuid或id"],       # 来源级 id（project_sources.id）或切片级 id（project_source_chunks.id）
        "entities": ["实体名"]
      },
      "difficulty": "normal|alias|typo|homophone|same_name|no_result|conflict|cross_chapter|no_inject",
      "notes": "标注说明"
    }

扩展字段（契约之外，供溯源用，基线运行器忽略）：
    expected.project_id   —— 样本所属项目 id
    expected.record_ids   —— 期望证据在结构化表（ability_profile 等）中的行 id，便于回查

只依赖 Python 标准库（dataclasses / json / typing）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DATASET_VERSION = "1.0"

# ──────────────────────────────────────────────────────────
# 合法取值枚举（与 run_baseline.py / README.md 契约一致）
# ──────────────────────────────────────────────────────────

# query.category：查询意图分类
CATEGORIES = (
    "character_first_appearance",  # 角色首次出场 / 觉醒时间
    "ability_query",               # 角色能力查询
    "timeline_event",              # 时间线事件查询
    "world_rule",                  # 世界规则查询
    "character_profile",           # 角色档案查询
    "source_chunk",                # 正文片段查询
    "no_result",                   # 明确查无结果的查询
    "meta",                        # 元查询（如"哪些内容不该注入"）
)

# expected.evidence_type：期望证据的类型
EVIDENCE_TYPES = (
    "character_profile",
    "ability_profile",
    "event_timeline",
    "world_rule",
    "source_chunk",
)

# sample.difficulty：难度/干扰类型
DIFFICULTIES = (
    "normal",       # 普通正常查询
    "alias",        # 别名/缩写
    "typo",         # 错别字
    "homophone",    # 同音字
    "same_name",    # 同名不同人（需要消歧）
    "no_result",    # 查无结果（检索不应强行召回）
    "conflict",     # 新旧/多来源设定冲突
    "cross_chapter",  # 跨章节聚合推理
    "no_inject",    # 元查询：哪些内容不应注入正文
    "smoke",        # 基线运行器内置冒烟样本专用（不进正式数据集）
)


# ──────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────

@dataclass
class EvalExpected:
    """期望证据：检索结果应命中的内容描述。"""

    evidence_type: str = "source_chunk"
    chapter_no: Optional[int] = None
    source_ids: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    # ── 扩展字段（溯源用，run_baseline.py 不读取）──
    project_id: Optional[str] = None
    record_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 去掉为空的扩展字段，保持契约输出干净
        if not d.get("project_id"):
            d.pop("project_id", None)
        if not d.get("record_ids"):
            d.pop("record_ids", None)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalExpected":
        return cls(
            evidence_type=str(raw.get("evidence_type") or "source_chunk"),
            chapter_no=raw.get("chapter_no"),
            source_ids=[str(x) for x in (raw.get("source_ids") or []) if x],
            entities=[str(x) for x in (raw.get("entities") or []) if x],
            project_id=raw.get("project_id"),
            record_ids=[str(x) for x in (raw.get("record_ids") or []) if x],
        )


@dataclass
class EvalSample:
    """一条检索评测样本。"""

    query_id: str
    category: str
    query: str
    expected: EvalExpected
    difficulty: str = "normal"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "category": self.category,
            "query": self.query,
            "expected": self.expected.to_dict(),
            "difficulty": self.difficulty,
            "notes": self.notes,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalSample":
        return cls(
            query_id=str(raw["query_id"]),
            category=str(raw.get("category") or "unknown"),
            query=str(raw["query"]),
            expected=EvalExpected.from_dict(raw.get("expected") or {}),
            difficulty=str(raw.get("difficulty") or "normal"),
            notes=str(raw.get("notes") or ""),
        )


# ──────────────────────────────────────────────────────────
# 校验
# ──────────────────────────────────────────────────────────

def validate_sample(sample: EvalSample, *, strict: bool = True) -> list[str]:
    """校验一条样本，返回问题列表（空列表 = 合法）。

    strict=True 时 category / difficulty / evidence_type 必须在枚举内；
    strict=False 仅检查必填字段（与 run_baseline.py 的过滤逻辑对齐）。
    """
    problems: list[str] = []
    if not sample.query_id:
        problems.append("query_id 缺失")
    if not sample.query:
        problems.append("query 缺失")
    if not (sample.expected.source_ids or sample.expected.entities):
        problems.append("expected.source_ids 与 expected.entities 至少一个非空")
    if strict:
        if sample.category not in CATEGORIES:
            problems.append(f"category 非法: {sample.category!r}（合法值: {'|'.join(CATEGORIES)}）")
        if sample.difficulty not in DIFFICULTIES:
            problems.append(f"difficulty 非法: {sample.difficulty!r}（合法值: {'|'.join(DIFFICULTIES)}）")
        if sample.expected.evidence_type not in EVIDENCE_TYPES:
            problems.append(f"expected.evidence_type 非法: {sample.expected.evidence_type!r}")
    if sample.expected.chapter_no is not None and not isinstance(sample.expected.chapter_no, int):
        problems.append(f"expected.chapter_no 必须是整数，得到 {sample.expected.chapter_no!r}")
    return problems


# ──────────────────────────────────────────────────────────
# 读写工具
# ──────────────────────────────────────────────────────────

def load_dataset(path: str | Path) -> tuple[list[EvalSample], dict[str, Any], int]:
    """从 JSON 文件加载评测数据集。

    兼容顶层为数组，或 {"meta": ..., "samples"|"items"|"data": [...]} 两种格式
    （与 run_baseline.py 的 load_dataset 一致）。
    返回 (samples, meta, skipped)；meta 为空 dict 时表示文件没有 meta 块。
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        items, meta = raw, {}
    elif isinstance(raw, dict):
        items = raw.get("samples") or raw.get("items") or raw.get("data") or []
        meta = {k: v for k, v in raw.items() if k not in ("samples", "items", "data")}
    else:
        raise ValueError(f"数据集 {path} 顶层必须是 JSON 数组或包含 samples/items/data 的对象")

    samples: list[EvalSample] = []
    skipped = 0
    for it in items:
        sample = EvalSample.from_dict(it)
        problems = validate_sample(sample, strict=False)
        if problems:
            skipped += 1
            continue
        samples.append(sample)
    return samples, meta, skipped


def write_dataset(
    path: str | Path,
    samples: list[EvalSample],
    meta: Optional[dict[str, Any]] = None,
    *,
    strict: bool = True,
) -> int:
    """将样本写为 JSON（UTF-8，ensure_ascii=False，indent=2）。

    写前做严格校验，校验失败的样本计入返回的 skipped 数。
    返回写入的样本条数。
    """
    meta = dict(meta or {})
    meta.setdefault("dataset_version", DATASET_VERSION)
    meta.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    valid: list[dict[str, Any]] = []
    skipped = 0
    for sample in samples:
        problems = validate_sample(sample, strict=strict)
        if problems:
            skipped += 1
            continue
        valid.append(sample.to_dict())
    meta["sample_count"] = len(valid)
    if skipped:
        meta["skipped_invalid"] = skipped

    payload: Any = {"meta": meta, "samples": valid}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(valid)


# ──────────────────────────────────────────────────────────
# CLI：python3 dataset_schema.py <dataset.json> [--lenient]
# ──────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    # argv 即 sys.argv[1:]：第一个参数是数据集路径
    if len(argv) < 1:
        print(f"用法: python3 {Path(__file__).name} <dataset.json> [--lenient]", file=sys.stderr)
        return 2
    path = argv[0]
    strict = "--lenient" not in argv
    samples, meta, skipped = load_dataset(path)
    total_problems = 0
    for s in samples:
        for p in validate_sample(s, strict=strict):
            total_problems += 1
            print(f"[invalid] {s.query_id}: {p}")
    print(f"{path}: 共 {len(samples) + skipped} 条，有效 {len(samples)} 条，加载时跳过 {skipped} 条，"
          f"严格校验问题 {total_problems} 条，meta={meta}")
    return 0 if (skipped == 0 and total_problems == 0) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
