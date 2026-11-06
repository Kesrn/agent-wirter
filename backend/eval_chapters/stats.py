"""Small-sample confidence intervals and paired bootstrap comparisons."""

from __future__ import annotations

import math
import random
from statistics import mean
from typing import Any


BOOTSTRAP_RESAMPLES = 5000
_Z_95 = 1.959963984540054
_DIRECTIONS = {
    "rule_first_pass": 1,
    "rule_final_pass": 1,
    "judge_score": 1,
    "ref_edit_ratio": -1,
    "cost": -1,
    "latency_ms": -1,
}


def wilson_interval(successes: int, total: int, *, z: float = _Z_95) -> tuple[float, float] | None:
    if total <= 0:
        return None
    successes = max(0, min(int(successes), int(total)))
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) / total) + z2 / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def paired_bootstrap(
    differences: list[float],
    *,
    seed: int = 0,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    if resamples < 5000:
        raise ValueError("配对 bootstrap 至少重采样 5000 次")
    if not differences:
        return {
            "mean_difference": None,
            "ci95": None,
            "wins": 0,
            "ties": 0,
            "losses": 0,
            "n": 0,
        }
    rng = random.Random(seed)
    n = len(differences)
    boot_means = sorted(
        mean(differences[rng.randrange(n)] for _ in range(n))
        for _ in range(resamples)
    )
    low_index = max(0, math.floor(0.025 * resamples))
    high_index = min(resamples - 1, math.ceil(0.975 * resamples) - 1)
    return {
        "mean_difference": mean(differences),
        "ci95": [boot_means[low_index], boot_means[high_index]],
        "wins": sum(value > 1e-12 for value in differences),
        "ties": sum(abs(value) <= 1e-12 for value in differences),
        "losses": sum(value < -1e-12 for value in differences),
        "n": n,
    }


def _metric_value(row: dict[str, Any], metric: str) -> float | None:
    value = row.get(metric)
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_summary(results: list[dict[str, Any]], variants: list[str], *, seed: int) -> dict[str, Any]:
    by_variant: dict[str, dict[str, Any]] = {}
    for variant in variants:
        rows = [row for row in results if row.get("variant") == variant and row.get("status") == "completed"]
        metrics: dict[str, Any] = {"sample_count": len(rows)}
        for metric in ("rule_first_pass", "rule_final_pass"):
            successes = sum(bool(row.get(metric)) for row in rows)
            metrics[metric] = {
                "successes": successes,
                "total": len(rows),
                "rate": successes / len(rows) if rows else None,
                "ci95": wilson_interval(successes, len(rows)),
            }
        for metric in ("judge_score", "ref_edit_ratio", "cost", "latency_ms"):
            values = [value for row in rows if (value := _metric_value(row, metric)) is not None]
            metrics[metric] = mean(values) if values else None
        by_variant[variant] = metrics

    baseline = "baseline" if "baseline" in variants else (variants[0] if variants else None)
    baseline_rows = {
        row["sample_id"]: row
        for row in results
        if row.get("variant") == baseline and row.get("status") == "completed"
    }
    paired: dict[str, dict[str, Any]] = {}
    for variant in variants:
        if variant == baseline:
            continue
        variant_rows = {
            row["sample_id"]: row
            for row in results
            if row.get("variant") == variant and row.get("status") == "completed"
        }
        common_ids = sorted(set(baseline_rows) & set(variant_rows))
        paired[variant] = {}
        for metric, direction in _DIRECTIONS.items():
            differences = []
            for sample_id in common_ids:
                baseline_value = _metric_value(baseline_rows[sample_id], metric)
                variant_value = _metric_value(variant_rows[sample_id], metric)
                if baseline_value is not None and variant_value is not None:
                    differences.append(direction * (variant_value - baseline_value))
            compared = paired_bootstrap(differences, seed=seed, resamples=BOOTSTRAP_RESAMPLES)
            interval = compared["ci95"]
            compared["significance"] = (
                "不显著" if interval is None or (interval[0] <= 0 <= interval[1]) else "显著"
            )
            paired[variant][metric] = compared

    return {
        "seed": seed,
        "baseline_variant": baseline,
        "sample_count_by_variant": {name: value["sample_count"] for name, value in by_variant.items()},
        "by_variant": by_variant,
        "paired_vs_baseline": paired,
    }


def _format_interval(interval: list[float] | tuple[float, float] | None, *, percent: bool = False) -> str:
    if interval is None:
        return "—"
    scale = 100 if percent else 1
    suffix = "%" if percent else ""
    return f"[{interval[0] * scale:.1f}{suffix}, {interval[1] * scale:.1f}{suffix}]"


def render_compare_markdown(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    variants = list(summary["by_variant"])
    lines = [
        "# 章节流水线评测对比",
        "",
        "| 版本 | 样本数 | 规则首次通过率（95%区间） | 评分器分数 | 相对参考稿改动比例 | 每章成本 | 每章耗时 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in variants:
        metrics = summary["by_variant"][variant]
        first_pass = metrics["rule_first_pass"]
        rate = first_pass["rate"]
        rate_text = "—" if rate is None else f"{rate * 100:.1f}% {_format_interval(first_pass['ci95'], percent=True)}"
        def fmt(metric: str, suffix: str = "") -> str:
            value = metrics.get(metric)
            return "—" if value is None else f"{value:.4f}{suffix}"
        lines.append(
            f"| {variant} | {metrics['sample_count']} | {rate_text} | {fmt('judge_score')} | "
            f"{fmt('ref_edit_ratio')} | {fmt('cost')} | {fmt('latency_ms', ' ms')} |"
        )

    lines.extend([
        "",
        "所有变体的输出都用同一套规则离线判定，与该变体是否启用规则无关。",
    ])

    lines.extend([
        "",
        "## 相对基线的配对差值",
        "",
        "正值表示有利于该变体；相对参考稿改动比例、成本和耗时已按“降低为有利”反向计分。",
        "",
        "| 版本 | 指标 | 有利方向均值差 | 配对 bootstrap 95%区间 | 胜/平/负 | 结论 |",
        "|---|---|---:|---:|---:|---|",
    ])
    for variant, metric_values in summary["paired_vs_baseline"].items():
        for metric, data in metric_values.items():
            difference = data["mean_difference"]
            difference_text = "—" if difference is None else f"{difference:.4f}"
            interval = _format_interval(data["ci95"])
            lines.append(
                f"| {variant} | {metric} | {difference_text} | {interval} | "
                f"{data['wins']}/{data['ties']}/{data['losses']} | {data['significance']} |"
            )
    maximum_n = max(summary["sample_count_by_variant"].values(), default=0)
    if maximum_n < 30:
        warning = (
            f"样本量警告：本次最多每个变体 {maximum_n} 条样本（少于 30），区间可能较宽；"
            "只有提升明显大于区间才能说“变好了”。"
        )
    else:
        warning = f"样本量警告：本次每个变体最多 {maximum_n} 条样本，结论仍应结合置信区间和数据来源判断。"
    lines.extend(["", warning, "区间包含 0 的差值均标注为“不显著”。", ""])
    return "\n".join(lines)
