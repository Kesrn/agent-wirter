#!/usr/bin/env python3
"""CLI for offline batch evaluation of the real chapter workflow."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from eval_chapters.runner import load_examples_file, load_examples_readonly_db, run_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线运行章节流水线评测与消融对比")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--provider", choices=("mock", "config"), required=True)
    parser.add_argument("--judge-provider", choices=("mock", "config"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-total-tokens", type=int, default=250000)
    parser.add_argument("--yes", action="store_true", help="确认开始真实模型调用")
    parser.add_argument("--confirm-holdout", action="store_true")
    parser.add_argument("--limit", type=int)
    examples = parser.add_mutually_exclusive_group()
    examples.add_argument("--examples-from-db", type=Path)
    examples.add_argument("--examples-file", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> dict:
    pairs, identifiers, source = [], [], None
    if args.examples_file:
        pairs, identifiers = load_examples_file(args.examples_file)
        source = f"file:{args.examples_file}"
    elif args.examples_from_db:
        pairs, identifiers = load_examples_readonly_db(args.examples_from_db)
        source = f"sqlite-readonly:{args.examples_from_db}"
    return await run_batch(
        dataset_path=args.dataset,
        split=args.split,
        variant_names=args.variants,
        provider_name=args.provider,
        judge_provider_name=args.judge_provider,
        output_dir=args.out,
        seed=args.seed,
        max_total_tokens=args.max_total_tokens,
        yes=args.yes,
        confirm_holdout=args.confirm_holdout,
        limit=args.limit,
        example_pairs=pairs,
        example_ids=identifiers,
        examples_source=source,
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (ValueError, OSError) as exc:
        print(f"评测未启动或已停止：{exc}", file=sys.stderr)
        return 2
    if result["holdout_warning"]:
        print(f"⚠️ {result['holdout_warning']}", file=sys.stderr)
    if result["stopped_reason"]:
        print(f"⚠️ {result['stopped_reason']}；已保存已完成结果", file=sys.stderr)
    print(f"估算 token：{result['estimate']['total_tokens_estimate']}")
    print(f"已完成结果：{result['run_meta']['result_rows']}")
    print(f"输出目录：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
