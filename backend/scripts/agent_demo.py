"""Run the standalone agent demo in scripted offline or explicit real mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    # Allow the documented `python scripts/agent_demo.py` invocation from backend/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_demo.budget import Budget
from agent_demo.demo import build_demo_store, build_offline_provider
from agent_demo.loop import AgentRunResult, run_agent


DEFAULT_GOAL = "写第 4 章第 2 场：林晚在灯塔读第三封信，必须提到钥匙与潮声，不得揭示寄信人身份，约 600 字，并通过校验。"


def _get_real_provider():
    from agents.llm_provider import get_llm_provider

    return get_llm_provider()


def _print_run(result: AgentRunResult) -> None:
    for step in result.steps:
        print(f"\n第 {step['index']} 步")
        if step.get("error"):
            print(f"模型错误：{step['error']}")
        elif step.get("model_text"):
            print(f"模型：{step['model_text']}")
        for call in step.get("tool_calls", []):
            state = "成功" if call["ok"] else "失败"
            if call["ok"] and call["name"] == "run_validators":
                try:
                    passed = json.loads(call["summary"])["data"]["passed"]
                    state = "校验通过" if passed else "校验失败"
                except (KeyError, TypeError, ValueError):
                    pass
            print(f"工具 {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)}) → {state}")
            print(f"  结果：{call['summary']}")
    if result.final_answer:
        print(f"\n最终答案：{result.final_answer}")
    print(f"\n停止原因：{result.stop_reason.value}")
    print("totals：" + json.dumps(result.totals, ensure_ascii=False))


async def _run(args: argparse.Namespace) -> int:
    budget = Budget()
    budget = replace(
        budget,
        max_steps=args.max_steps if args.max_steps is not None else budget.max_steps,
        max_total_tokens=args.max_tokens if args.max_tokens is not None else budget.max_total_tokens,
        max_wall_seconds=args.max_seconds if args.max_seconds is not None else budget.max_wall_seconds,
    )
    if args.real:
        print("即将使用真实模型；预算：" + json.dumps({
            "max_steps": budget.max_steps,
            "max_total_tokens": budget.max_total_tokens,
            "max_wall_seconds": budget.max_wall_seconds,
        }, ensure_ascii=False))
        if not args.yes:
            print("未执行：真实模式需要同时提供 --yes。")
            return 2
        # Real configuration and provider are intentionally loaded only after the
        # explicit confirmation gate. Offline mode never constructs a real provider.
        provider = _get_real_provider()
    else:
        provider = build_offline_provider()

    result = await run_agent(provider, args.goal, build_demo_store(), budget)
    _print_run(result)
    runs_dir = Path(__file__).resolve().parents[1] / "agent_demo" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_path = runs_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    run_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"JSON 轨迹：{run_path.relative_to(Path(__file__).resolve().parents[1])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="最小 Agent 工具循环与预算演示")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--offline", action="store_true", help="使用预制脚本离线演示（默认模式）")
    parser.add_argument("--real", action="store_true", help="明确启用真实 LLM；需要再提供 --yes")
    parser.add_argument("--yes", action="store_true", help="确认允许真实模型调用")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args()
    if args.real and args.offline:
        parser.error("--real 与 --offline 不能同时使用")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
