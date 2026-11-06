#!/usr/bin/env python3
"""Run deterministic chapter rules over a JSONL evaluation set (offline)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Support ``python scripts/run_validators.py`` from any working directory.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.chapter_rules import validate_task_card_compliance


RULE_CODES = (
    "RULE_MUST_INCLUDE_MISSING",
    "RULE_FORBIDDEN_PRESENT",
    "RULE_CHARACTER_FORBIDDEN",
    "RULE_CHARACTER_ABSENT",
    "RULE_NAME_VARIANT",
    "RULE_BANNED_PHRASE",
)


def evaluate_samples(samples: list[dict[str, Any]], *, rules_enabled: bool = True) -> dict[str, Any]:
    report_samples: list[dict[str, Any]] = []
    hit_counts = dict.fromkeys(RULE_CODES, 0)
    zero_error_count = 0
    for sample in samples:
        issues = (
            validate_task_card_compliance(
                str(sample.get("text") or ""),
                sample.get("task_card"),
                sample.get("characters") if isinstance(sample.get("characters"), list) else [],
            )
            if rules_enabled else []
        )
        issue_dicts = [
            {
                "code": issue.code,
                "severity": issue.severity,
                "message": issue.message,
                "evidence": issue.evidence,
            }
            for issue in issues
        ]
        for code in RULE_CODES:
            if any(issue.code == code for issue in issues):
                hit_counts[code] += 1
        has_error = any(issue.severity == "ERROR" for issue in issues)
        zero_error_count += not has_error
        report_samples.append({
            "id": sample.get("id"),
            "passed": not has_error,
            "issues": issue_dicts,
        })

    count = len(samples)
    checks = {
        code: {
            "passed": count - hit_counts[code],
            "total": count,
            "pass_rate": round((count - hit_counts[code]) / count, 4) if count else None,
        }
        for code in RULE_CODES
    }
    return {
        "rules_enabled": rules_enabled,
        "sample_count": count,
        "zero_error_pass_rate": round(zero_error_count / count, 4) if count else None,
        "checks": checks,
        "samples": report_samples,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    samples = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
        samples.append(value)
    return samples


def _write_outputs(summary: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 章节确定性规则评测",
        "",
        f"- 规则启用：{summary['rules_enabled']}",
        f"- 样本数：{summary['sample_count']}",
        f"- 零 ERROR 通过率：{summary['zero_error_pass_rate']}",
        "",
        "## 检查项通过率",
        "",
        "| 检查项 | 通过 | 样本 | 通过率 |",
        "|---|---:|---:|---:|",
    ]
    for code, result in summary["checks"].items():
        lines.append(f"| {code} | {result['passed']} | {result['total']} | {result['pass_rate']} |")
    lines.extend(["", "## 样本问题清单", ""])
    for sample in summary["samples"]:
        lines.append(f"### {sample['id']}")
        if sample["issues"]:
            for issue in sample["issues"]:
                lines.append(f"- {issue['severity']} {issue['code']}: {issue['message']}")
        else:
            lines.append("- 无问题")
        lines.append("")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL，每行含 id/text/task_card/characters")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rules-off", action="store_true", help="仅输出零规则的消融对照")
    args = parser.parse_args()
    summary = evaluate_samples(_read_jsonl(args.input), rules_enabled=not args.rules_off)
    _write_outputs(summary, args.output_dir)
    print(json.dumps({
        "sample_count": summary["sample_count"],
        "zero_error_pass_rate": summary["zero_error_pass_rate"],
        "summary": str(args.output_dir / "summary.json"),
        "report": str(args.output_dir / "report.md"),
        "rules_enabled": summary["rules_enabled"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
