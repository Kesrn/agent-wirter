#!/usr/bin/env python3
"""检索评测结果对比工具（阶段一：多检索器对比）。

对比两个 run_baseline.py --out 产出的结果 JSON（如 ilike 基线 vs lexical 检索器），
输出：
    - 总体指标对比（recall@k / MRR / precision@k / 误召回率 / p95 延迟 / 样本数）
    - 按 category / difficulty 分组的 recall / MRR / precision 对比（含差值 Δ）

指标从两份文件的 samples[]（每样本 hit / mrr / precision / latency_ms）重新聚合，
不依赖文件里已有的 summary / by_* 字段，保证两组对比口径一致；
聚合函数直接复用 run_baseline.aggregate / group_summaries（run_baseline 顶层只 import
标准库，无副作用导入）。纯 Python 标准库实现，无第三方依赖。

用法：
    # 位置参数（推荐）
    python3 retrieval_eval/compare_results.py \
        results/baseline_regression_76.json results/lexical_regression_76.json

    # 显式参数 + 自定义标签
    python3 retrieval_eval/compare_results.py \
        --base results/baseline_regression_76.json --cmp results/lexical_regression_76.json \
        --label-base ilike --label-cmp lexical

    # 同时导出对比 JSON（供后续阶段/报告使用）
    python3 retrieval_eval/compare_results.py a.json b.json --out comparison.json

注意：两份文件样本数不一致时输出警告但仍继续（可能不是同一数据集，结果仅供参考）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 复用 run_baseline 的聚合逻辑（run_baseline 顶层只 import 标准库，安全）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_baseline import aggregate, group_summaries  # noqa: E402

METRIC_NAMES = (
    "n_samples",
    "recall_at_k",
    "mrr",
    "precision_at_k",
    "false_recall_rate",
    "p95_latency_ms",
)

# 总体表展示顺序：指标名 → 数值格式（d=整数，4f/1f=小数位数）
OVERALL_SPECS = (
    ("n_samples", "d"),
    ("recall_at_k", "4f"),
    ("mrr", "4f"),
    ("precision_at_k", "4f"),
    ("false_recall_rate", "4f"),
    ("p95_latency_ms", "1f"),
)


def load_results(path: str) -> dict:
    """加载 run_baseline.py 产出的结果 JSON，并校验基本结构。"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"结果文件不存在: {p}")
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or "samples" not in raw:
        raise SystemExit(
            f"{p} 不是 run_baseline.py 的结果 JSON（缺少 samples 字段）："
            f"请提供 run_baseline.py --out 生成的文件"
        )
    return raw


def records_of(raw: dict) -> list[dict]:
    return raw.get("samples") or []


def _fmt(v, spec: str) -> str:
    if v is None:
        return "-"
    if spec == "d":
        return f"{int(v):d}"
    return f"{v:.{spec}}"


def _fmt_delta(b, c, spec: str) -> str:
    if b is None or c is None:
        return "-"
    if spec == "d":
        return f"{int(c) - int(b):+d}"
    return f"{c - b:+.4f}" if spec == "4f" else f"{c - b:+.1f}"


def print_overall(summary_b: dict, summary_c: dict, label_base: str, label_cmp: str) -> None:
    """打印总体指标对比表。"""
    print("\n════════ 总体对比 ════════")
    print(f"（{label_base} = 基准，{label_cmp} = 对比；Δ = 对比 - 基准）")
    header = f"{'metric':<20}{label_base:>22}{label_cmp:>22}{'Δ':>14}"
    print(header)
    print("-" * len(header))
    for name, spec in OVERALL_SPECS:
        bv, cv = summary_b.get(name), summary_c.get(name)
        print(
            f"{name:<20}{_fmt(bv, spec):>22}{_fmt(cv, spec):>22}"
            f"{_fmt_delta(bv, cv, spec):>14}"
        )


def print_group_table(
    title: str,
    groups_b: dict,
    groups_c: dict,
    label_base: str,
    label_cmp: str,
) -> None:
    """打印按 category / difficulty 分组的 recall / MRR / precision 对比表。

    只出现在单侧文件里的分组正常展示（另一侧以 '-' 占位），不报错。
    """
    keys = sorted(set(groups_b) | set(groups_c))
    print(f"\n── {title} ──")
    header = (
        f"{'group':<26}{'n_b':>4}{'n_c':>4}"
        f"{'recall_b':>9}{'recall_c':>9}{'Δr':>8}"
        f"{'mrr_b':>8}{'mrr_c':>8}{'Δm':>8}"
        f"{'prec_b':>9}{'prec_c':>9}{'Δp':>8}"
    )
    print(header)
    print("-" * len(header))
    for k in keys:
        b = groups_b.get(k)
        c = groups_c.get(k)
        nb = b["n_samples"] if b else 0
        nc = c["n_samples"] if c else 0
        row = [f"{k:<26}", f"{nb:>4}", f"{nc:>4}"]
        for metric, width, spec in (
            ("recall_at_k", 9, "3f"),
            ("mrr", 8, "3f"),
            ("precision_at_k", 9, "3f"),
        ):
            bv = b[metric] if b else None
            cv = c[metric] if c else None
            row.append(f"{_fmt(bv, spec):>{width}}")
            row.append(f"{_fmt(cv, spec):>{width}}")
            row.append(f"{_fmt_delta(bv, cv, '4f'):>8}")
        print("".join(row))
    print(f"（{label_base} = 基准，{label_cmp} = 对比；Δ = 对比 - 基准）")


def diff_groups(groups_b: dict, groups_c: dict) -> dict:
    """构造分组对比 JSON（含 base/compare/delta 三份数值）。"""
    out: dict = {}
    for k in sorted(set(groups_b) | set(groups_c)):
        b, c = groups_b.get(k), groups_c.get(k)
        entry: dict = {
            "n_base": b["n_samples"] if b else 0,
            "n_compare": c["n_samples"] if c else 0,
        }
        for m in METRIC_NAMES:
            bv = b[m] if b else None
            cv = c[m] if c else None
            delta = None
            if bv is not None and cv is not None:
                delta = round(cv - bv, 4) if m != "n_samples" else cv - bv
            entry[m] = {"base": bv, "compare": cv, "delta": delta}
        out[k] = entry
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="对比两个检索评测结果 JSON（run_baseline.py --out 产物）："
                    "总体指标 + 按 category/difficulty 分组的 recall/MRR/precision"
    )
    parser.add_argument("base", nargs="?", help="基准结果 JSON 路径（位置参数，与 --base 二选一）")
    parser.add_argument("cmp", nargs="?", help="对比结果 JSON 路径（位置参数，与 --cmp 二选一）")
    parser.add_argument("--base", dest="base_opt", help="基准结果 JSON 路径")
    parser.add_argument("--cmp", dest="cmp_opt", help="对比结果 JSON 路径")
    parser.add_argument("--label-base", default=None, help="基准标签（默认取文件名的 stem）")
    parser.add_argument("--label-cmp", default=None, help="对比标签（默认取文件名的 stem）")
    parser.add_argument("--out", default=None, help="可选的对比 JSON 输出路径")
    args = parser.parse_args()

    base_path = args.base or args.base_opt
    cmp_path = args.cmp or args.cmp_opt
    if not base_path or not cmp_path:
        parser.error("需要两个结果 JSON：位置参数 base cmp，或 --base/--cmp")

    raw_b = load_results(base_path)
    raw_c = load_results(cmp_path)

    label_base = args.label_base or Path(base_path).stem
    label_cmp = args.label_cmp or Path(cmp_path).stem

    rec_b = records_of(raw_b)
    rec_c = records_of(raw_c)
    summary_b = aggregate(rec_b)
    summary_c = aggregate(rec_c)

    if summary_b["n_samples"] != summary_c["n_samples"]:
        print(
            f"[warn] 样本数不一致: {label_base}={summary_b['n_samples']} vs "
            f"{label_cmp}={summary_c['n_samples']}（可能不是同一数据集，对比结果仅供参考）"
        )

    groups_b = group_summaries(rec_b)
    groups_c = group_summaries(rec_c)

    print_overall(summary_b, summary_c, label_base, label_cmp)
    print_group_table(
        "按 category 分组对比",
        groups_b["by_category"],
        groups_c["by_category"],
        label_base,
        label_cmp,
    )
    print_group_table(
        "按 difficulty 分组对比",
        groups_b["by_difficulty"],
        groups_c["by_difficulty"],
        label_base,
        label_cmp,
    )

    if args.out:
        payload = {
            "base": {"path": str(Path(base_path).resolve()), "label": label_base, "summary": summary_b},
            "compare": {"path": str(Path(cmp_path).resolve()), "label": label_cmp, "summary": summary_c},
            "overall": {
                m: {
                    "base": summary_b.get(m),
                    "compare": summary_c.get(m),
                    "delta": (
                        summary_c.get(m) - summary_b.get(m)
                        if summary_b.get(m) is not None and summary_c.get(m) is not None
                        else None
                    ),
                }
                for m in METRIC_NAMES
            },
            "by_category": diff_groups(groups_b["by_category"], groups_c["by_category"]),
            "by_difficulty": diff_groups(groups_b["by_difficulty"], groups_c["by_difficulty"]),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[ok] 对比 JSON 已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
