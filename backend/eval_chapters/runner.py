"""Run an ablation matrix through the real chapter graph without a database."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

from agents.llm_types import LLMResult, TokenUsage
from eval_chapters.dataset import EvalSample, load_dataset, sample_input_text
from eval_chapters.stats import build_summary, render_compare_markdown
from eval_chapters.variants import get_variants
from services.edit_examples import EditPair, build_edit_pairs, render_edit_examples_block


# One pipeline attempt budget covers: architect 4096; writer initial + retry
# 8192; structured critic + repair 4096; editor initial + two retries 12288;
# continuity 2048; judge + one structured repair 2400.
MAX_PIPELINE_OUTPUT_TOKENS_PER_SAMPLE = 33024


class BudgetExceeded(RuntimeError):
    def __init__(self, used: int, limit: int):
        self.used = used
        self.limit = limit
        super().__init__(f"运行中累计 token {used} 超出预算 {limit}，已停止")


@dataclass
class UsageTracker:
    max_total_tokens: int
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    calls: int = 0
    estimated_calls: int = 0
    total_cost: float = 0.0
    all_costs_known: bool = True
    unknown_cost_calls: int = 0
    prompt_hook: Callable[[str, str], None] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(
        self,
        system_prompt: str,
        user_prompt: str,
        text: str,
        *,
        usage: TokenUsage | None,
        model: str | None,
        latency_ms: int,
    ) -> None:
        estimated = usage is None or usage.estimated
        if usage is None:
            input_count = math.ceil((len(system_prompt) + len(user_prompt)) / 2)
            output_count = math.ceil(len(text) / 2)
            usage = TokenUsage(input_count, output_count, input_count + output_count, estimated=True)
        self.input_tokens += int(usage.input_tokens)
        self.output_tokens += int(usage.output_tokens)
        self.latency_ms += max(0, int(latency_ms or 0))
        self.calls += 1
        self.estimated_calls += int(estimated)
        try:
            from harness.llm_call_logger import _calculate_cost

            cost = _calculate_cost(model, usage)
        except Exception:
            cost = None
        if cost is None:
            self.all_costs_known = False
            self.unknown_cost_calls += 1
        else:
            self.total_cost += float(cost)
        if self.prompt_hook is not None:
            self.prompt_hook(system_prompt, user_prompt)
        if self.max_total_tokens > 0 and self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded(self.total_tokens, self.max_total_tokens)

    def snapshot(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.total_cost, 6) if self.all_costs_known else None,
            "latency_ms": self.latency_ms,
            "calls": self.calls,
            "estimated_calls": self.estimated_calls,
        }

    def mark(self) -> tuple[int, int, int, int, float, int, int]:
        return (
            self.input_tokens, self.output_tokens, self.latency_ms, self.calls,
            self.total_cost, self.unknown_cost_calls, self.estimated_calls,
        )

    def snapshot_since(self, mark: tuple[int, int, int, int, float, int, int]) -> dict[str, Any]:
        input_before, output_before, latency_before, calls_before, cost_before, unknown_before, estimated_before = mark
        input_tokens = self.input_tokens - input_before
        output_tokens = self.output_tokens - output_before
        unknown = self.unknown_cost_calls - unknown_before
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost": None if unknown else round(self.total_cost - cost_before, 6),
            "latency_ms": self.latency_ms - latency_before,
            "calls": self.calls - calls_before,
            "estimated_calls": self.estimated_calls - estimated_before,
        }


class MeteredProvider:
    """Transparent provider wrapper that counts 1.1 LLMResult usage."""

    def __init__(self, wrapped, tracker: UsageTracker):
        self._wrapped = wrapped
        self._tracker = tracker

    @property
    def model(self) -> str | None:
        return getattr(self._wrapped, "model", None)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools=None,
        response_format=None,
        timeout: float | None = None,
    ) -> LLMResult:
        started = time.perf_counter()
        complete = getattr(self._wrapped, "complete", None)
        if callable(complete):
            result = await complete(
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                response_format=response_format,
                timeout=timeout,
            )
            if not isinstance(result, LLMResult):
                raise TypeError("provider.complete 必须返回 LLMResult")
        else:
            text = await self._wrapped.generate(
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            result = LLMResult(text=text, usage=None, model=self.model)
        elapsed = int((time.perf_counter() - started) * 1000)
        self._tracker.record(
            system_prompt,
            user_prompt,
            result.text or "",
            usage=result.usage,
            model=result.model or self.model,
            latency_ms=result.latency_ms if result.latency_ms is not None else elapsed,
        )
        return result

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        return (await self.complete(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )).text

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096, **kwargs):
        chunks: list[str] = []
        started = time.perf_counter()
        async for chunk in self._wrapped.generate_stream(
            system_prompt, user_prompt, temperature=temperature, max_tokens=max_tokens, **kwargs,
        ):
            chunks.append(chunk)
            yield chunk
        text = "".join(chunks)
        usage_sink = kwargs.get("usage_sink")
        usage = usage_sink[-1] if usage_sink else None
        self._tracker.record(
            system_prompt,
            user_prompt,
            text,
            usage=usage,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def estimate_total_tokens(samples: list[EvalSample], variant_count: int) -> dict[str, int | str]:
    input_characters = sum(len(sample_input_text(sample)) for sample in samples)
    input_estimate = math.ceil(input_characters / 2)
    output_estimate = len(samples) * MAX_PIPELINE_OUTPUT_TOKENS_PER_SAMPLE
    return {
        "input_characters": input_characters,
        "input_tokens_estimate": input_estimate * variant_count,
        "max_output_tokens_per_sample": MAX_PIPELINE_OUTPUT_TOKENS_PER_SAMPLE,
        "max_output_tokens_estimate": output_estimate * variant_count,
        "total_tokens_estimate": (input_estimate + output_estimate) * variant_count,
        "formula": "(全部样本输入字符数 / 2 + 样本数 × 33024 输出 token 上限) × 变体数",
    }


def load_examples_file(path: str | Path) -> tuple[list[EditPair], list[str]]:
    pairs: list[EditPair] = []
    identifiers: list[str] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"示例文件第 {line_number} 行 JSON 无效") from exc
        if not isinstance(row, dict) or not isinstance(row.get("before"), str) or not isinstance(row.get("after"), str):
            raise ValueError(f"示例文件第 {line_number} 行必须包含字符串 before/after")
        pairs.extend(build_edit_pairs(row["before"], row["after"], max_pairs=3))
        identifier = row.get("generation_record_id")
        if identifier:
            identifiers.append(str(identifier))
    return pairs, list(dict.fromkeys(identifiers))


def load_examples_readonly_db(path: str | Path, *, limit: int = 3) -> tuple[list[EditPair], list[str]]:
    """Read applied edit examples from a user-selected SQLite file in mode=ro."""
    uri = f"file:{Path(path).expanduser().resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT id, chapter_id, accepted_version_id, human_edit_metrics "
            "FROM generation_records WHERE status = 'applied' "
            "ORDER BY updated_at DESC, id ASC LIMIT 20"
        ).fetchall()
        pairs: list[EditPair] = []
        identifiers: list[str] = []
        for record_id, chapter_id, anchor_id, raw_metrics in rows:
            try:
                metrics = json.loads(raw_metrics) if isinstance(raw_metrics, str) else raw_metrics
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(metrics, dict) or metrics.get("edited") is not True:
                continue
            human_id = metrics.get("human_version_id")
            metrics_anchor_id = metrics.get("anchor_version_id")
            if not human_id or not metrics_anchor_id or str(anchor_id) != str(metrics_anchor_id):
                continue
            try:
                ratio = float(metrics.get("edit_ratio"))
            except (TypeError, ValueError):
                continue
            if not 0.03 <= ratio <= 0.6:
                continue
            versions = connection.execute(
                "SELECT id, chapter_id, content FROM chapter_versions WHERE id IN (?, ?)",
                (str(metrics_anchor_id), str(human_id)),
            ).fetchall()
            by_id = {str(item[0]): (str(item[1]), item[2] or "") for item in versions}
            before = by_id.get(str(metrics_anchor_id))
            after = by_id.get(str(human_id))
            if before is None or after is None or before[0] != after[0] or before[0] != str(chapter_id):
                continue
            record_pairs = build_edit_pairs(before[1], after[1], max_pairs=3)
            if record_pairs:
                pairs.extend(record_pairs)
                identifiers.append(str(record_id))
            if len(identifiers) >= limit:
                break
        return pairs, identifiers
    finally:
        connection.close()


def _context_text(sample: EvalSample) -> tuple[str, dict[str, Any]]:
    context = sample.context
    parts = [f"## 项目简介\n{context.get('project_brief', '')}".strip()]
    characters = context.get("characters", [])
    character_lines = []
    if isinstance(characters, list):
        for item in characters:
            if not isinstance(item, dict):
                continue
            aliases = "、".join(str(alias) for alias in item.get("aliases", []) if alias)
            suffix = f"，别名：{aliases}" if aliases else ""
            character_lines.append(f"- {item.get('name', '')}{suffix}：{item.get('desc', '')}")
    if character_lines:
        parts.append("## 角色资料\n" + "\n".join(character_lines))
    if context.get("prior_summary"):
        parts.append(f"## 前情摘要\n{context['prior_summary']}")
    return "\n\n".join(part for part in parts if part), {"characters": characters if isinstance(characters, list) else []}


async def run_pipeline(
    sample: EvalSample,
    variant_name: str,
    variant_config: dict[str, bool],
    *,
    provider_factory: Callable[[], MeteredProvider],
    example_block: str,
    example_ids: list[str],
) -> dict[str, Any]:
    """Run production graph nodes with sample-fed context and an auto-approve route.

    The eval adapter supplies context/task card from JSONL and replaces only the
    external-data context loader, architect input source, and human-review routing.
    Generation, quality, rules, writer, critic, editor and continuity nodes remain
    the production ``workflow_v2`` graph nodes. No session or database is opened.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END

    from agents import workflow, workflow_v2

    context_text, context_summary = _context_text(sample)
    first_pass_report: dict[str, Any] | None = None
    first_pass_draft = ""
    first_pass_seen = False

    async def context_loader(_state):
        return {"context": context_text, "context_summary": context_summary}

    async def fixed_task_card(_state):
        return {"chapter_task_card": sample.task_card}

    async def example_loader(_state):
        return example_block, list(example_ids)

    original_validator = workflow_v2.draft_validator_node

    async def observed_validator(state):
        nonlocal first_pass_report, first_pass_draft, first_pass_seen
        if not first_pass_seen:
            first_pass_draft = str(state.get("draft") or "")
            first_pass_seen = True
        output = await original_validator(state)
        if first_pass_report is None:
            first_pass_report = output.get("draft_quality_report")
        return output

    def provider_factory_proxy(_config=None):
        return provider_factory()

    config = {"configurable": {"thread_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sample.id}:{variant_name}"))}}
    state = {
        "project_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"eval-project:{sample.id}")),
        "chapter_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"eval-chapter:{sample.id}")),
        "chapter_num": 1,
        "mode": "full_pipeline",
        "context": context_text,
        "context_summary": context_summary,
        "target_words": sample.target_words,
        "writer_prompt": "", "critic_prompt": "", "editor_prompt": "", "consistency_prompt": "",
        "llm_config": None,
        "selected_outline_ids": [], "selected_character_ids": [], "selected_world_entry_ids": [],
        "selected_hidden_thread_ids": [], "include_knowledge_sources": False,
        "include_previous_summary": False, "excluded_context_keys": [], "selected_direction": "",
        "user_note": "", "skill_packs": [], "harness_run_id": "", "harness_step_id": None,
        "chapter_task_card": sample.task_card, "structural_critique": {}, "edit_report": {},
        "workflow_key": "eval-chapter-pipeline", "revision_count": 0,
        "writer_retry_count": 0, "writer_max_retries": 1, "editor_retry_count": 0,
        "editor_max_retries": 2, "rules_enabled": variant_config["rules_enabled"],
        "fewshot_enabled": variant_config["fewshot_enabled"], "fewshot_example_ids": [],
        "pre_generation_mode": "FAST",
    }

    # Globals are replaced only while this sequential evaluation call is active.
    # The graph runner never stores AsyncSession or user database handles in state.
    with (
        patch.object(workflow_v2, "context_loader_node", context_loader),
        patch.object(workflow_v2, "chapter_architect_node", fixed_task_card),
        patch.object(workflow_v2, "route_after_review_v2", lambda _state: END),
        patch.object(workflow_v2, "_load_writer_edit_examples", example_loader),
        patch.object(workflow_v2, "draft_validator_node", observed_validator),
        patch.object(workflow_v2, "get_llm_provider", provider_factory_proxy),
        patch.object(workflow, "get_llm_provider", provider_factory_proxy),
    ):
        graph = workflow_v2.build_creative_graph_v2(pre_generation_mode="FAST")
        app = graph.compile(checkpointer=MemorySaver(), interrupt_before=[])
        final_state = await app.ainvoke(state, config=config)

    first_errors = [
        issue for issue in (first_pass_report or {}).get("issues", [])
        if isinstance(issue, dict) and issue.get("severity") == "ERROR"
    ]
    raw_final_report = final_state.get("final_quality_report")
    final_report = raw_final_report if isinstance(raw_final_report, dict) else {}
    final_errors = [
        issue for issue in final_report.get("issues", [])
        if isinstance(issue, dict) and issue.get("severity") == "ERROR"
    ]
    return {
        "first_draft": first_pass_draft,
        "final_output": final_state.get("draft") or "",
        "pipeline_first_pass": not first_errors,
        "pipeline_final_pass": isinstance(raw_final_report, dict) and not final_errors,
        "writer_retries": int(final_state.get("writer_retry_count", 0) or 0),
        "editor_retries": int(final_state.get("editor_retry_count", 0) or 0),
        "fewshot_example_ids": list(final_state.get("fewshot_example_ids") or []),
        "first_pass_report": first_pass_report,
        "final_quality_report": final_report,
        "auto_approved": isinstance(raw_final_report, dict) and not final_errors,
    }


async def judge_output(
    sample: EvalSample,
    output: str,
    *,
    provider_factory: Callable[[], MeteredProvider],
) -> float:
    from agents.structured import generate_structured
    from services.evaluation import (
        EvaluationJudgeOutput,
        build_judge_prompt,
        default_rubric,
        parse_judge_response,
    )

    rubric = default_rubric("novel")
    case = SimpleNamespace(
        task_type="chapter_generation",
        input_text=sample_input_text(sample),
        reference_output=sample.reference,
    )
    system_prompt, user_prompt = build_judge_prompt(case, rubric, [], output)
    provider = provider_factory()
    result = await generate_structured(
        provider,
        EvaluationJudgeOutput,
        system_prompt,
        user_prompt,
        temperature=0.1,
        max_tokens=1200,
        max_repairs=1,
    )
    return float(parse_judge_response(result.raw_text, rubric)["score"])


def _has_errors(report: dict[str, Any] | None) -> bool:
    return any(
        isinstance(issue, dict) and issue.get("severity") == "ERROR"
        for issue in (report or {}).get("issues", [])
    )


def _offline_rule_pass(text: str, sample: EvalSample, *, final: bool) -> bool:
    """Apply the same deterministic quality and task-card rules to every variant."""
    from services.chapter_quality import validate_draft_quality, validate_final_quality
    from services.chapter_rules import RulesConfig, validate_task_card_compliance

    body = text or ""
    if final:
        report = validate_final_quality(body, sample.target_words)
    else:
        report = validate_draft_quality(body, sample.target_words)

    raw_characters = sample.context.get("characters", [])
    characters = [
        {
            "name": str(item.get("name") or item.get("label") or ""),
            "aliases": item.get("aliases", []),
        }
        for item in raw_characters
        if isinstance(item, dict) and (item.get("name") or item.get("label"))
    ] if isinstance(raw_characters, list) else []
    rule_issues = validate_task_card_compliance(
        body,
        sample.task_card,
        characters,
        config=RulesConfig(),
    )
    return not any(
        getattr(issue, "severity", None) == "ERROR"
        for issue in (*report.issues, *rule_issues)
    )


def _git_revision() -> str | None:
    repository = Path(__file__).resolve().parents[2]
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _record_holdout_run(output_dir: Path, variants: list[str], sample_count: int) -> str | None:
    log_path = output_dir.parent / "holdout_runs.log"
    previous = 0
    if log_path.exists():
        previous = sum(1 for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip())
    warning = (
        f"留出集已被运行过 {previous} 次，结论不再可信为留出"
        if previous
        else None
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{datetime.now(timezone.utc).isoformat()}\tvariants={','.join(variants)}\t"
            f"samples={sample_count}\n"
        )
    return warning


def _write_outputs(output_dir: Path, results: list[dict[str, Any]], summary: dict[str, Any], run_meta: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in results),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "compare.md").write_text(render_compare_markdown(summary, results), encoding="utf-8")
    (output_dir / "run_meta.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_batch(
    *,
    dataset_path: str | Path,
    split: str,
    variant_names: list[str],
    provider_name: str,
    output_dir: str | Path,
    seed: int = 0,
    max_total_tokens: int = 250000,
    yes: bool = False,
    confirm_holdout: bool = False,
    limit: int | None = None,
    judge_provider_name: str | None = None,
    example_pairs: list[EditPair] | None = None,
    example_ids: list[str] | None = None,
    examples_source: str | None = None,
    provider_factory: Callable[[], Any] | None = None,
    judge_provider_factory: Callable[[], Any] | None = None,
    pipeline_runner: Callable[..., Any] = run_pipeline,
    judge_runner: Callable[..., Any] = judge_output,
    prompt_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    if split == "holdout" and not confirm_holdout:
        raise ValueError("运行留出集必须显式提供 --confirm-holdout")
    selected_variants = get_variants(variant_names)
    if "baseline" not in selected_variants:
        raise ValueError("配对对比必须包含 baseline 变体")
    samples = load_dataset(dataset_path, split=split, limit=limit)
    if not samples:
        raise ValueError(f"数据集中没有 split={split} 的样本")
    estimate = estimate_total_tokens(samples, len(selected_variants))
    estimated_tokens = int(estimate["total_tokens_estimate"])
    if estimated_tokens > max_total_tokens:
        raise ValueError(
            f"预算预检中止：估算 {estimated_tokens} token，超过 --max-total-tokens {max_total_tokens}；尚未发出模型调用"
        )
    if provider_name != "mock" and estimated_tokens > 0 and not yes:
        raise ValueError("真实模型运行必须显式提供 --yes；尚未发出模型调用")
    if provider_factory is None:
        if provider_name == "mock":
            from agents.mock_provider import MockProvider

            provider_factory = MockProvider
        elif provider_name == "config":
            from agents.llm_provider import get_llm_provider

            provider_factory = get_llm_provider
        else:
            raise ValueError("provider 只能是 mock 或 config")
    judge_provider_name = judge_provider_name or provider_name
    if judge_provider_factory is None:
        if judge_provider_name == "mock":
            from agents.mock_provider import MockProvider

            judge_provider_factory = MockProvider
        elif judge_provider_name == "config":
            from agents.llm_provider import get_llm_provider

            judge_provider_factory = get_llm_provider
        else:
            raise ValueError("judge-provider 只能是 mock 或 config")

    output_path = Path(output_dir)
    holdout_warning = _record_holdout_run(output_path, variant_names, len(samples)) if split == "holdout" else None
    pairs = list(example_pairs or [])
    identifiers = list(example_ids or [])
    example_block = render_edit_examples_block(pairs) if pairs else ""
    results: list[dict[str, Any]] = []
    tracker = UsageTracker(max_total_tokens=max_total_tokens, prompt_hook=prompt_hook)
    stopped_reason: str | None = None
    started_at = datetime.now(timezone.utc).isoformat()

    async def make_pipeline_result(sample: EvalSample, variant_name: str, config: dict[str, bool]) -> dict[str, Any]:
        return await pipeline_runner(
            sample,
            variant_name,
            config,
            provider_factory=lambda: MeteredProvider(provider_factory(), tracker),
            example_block=example_block,
            example_ids=identifiers,
        )

    async def make_judge_result(sample: EvalSample, output: str) -> float:
        return await judge_runner(
            sample,
            output,
            provider_factory=lambda: MeteredProvider(judge_provider_factory(), tracker),
        )

    for sample in samples:
        for variant_name, variant_config in selected_variants.items():
            usage_mark = tracker.mark()
            try:
                pipeline = await make_pipeline_result(sample, variant_name, variant_config)
                judge_score = await make_judge_result(sample, pipeline["final_output"])
                first_draft = str(pipeline.get("first_draft") or pipeline.get("final_output") or "")
                offline_first_pass = _offline_rule_pass(first_draft, sample, final=False)
                offline_final_pass = _offline_rule_pass(str(pipeline.get("final_output") or ""), sample, final=True)
                ref_edit_ratio = None
                if sample.reference is not None:
                    from services.edit_metrics_service import compute_edit_metrics

                    metrics = await asyncio.to_thread(
                        compute_edit_metrics,
                        pipeline["final_output"],
                        sample.reference,
                        anchor_version_id=f"eval:{sample.id}:{variant_name}",
                        human_version_id=f"reference:{sample.id}",
                    )
                    ref_edit_ratio = metrics["edit_ratio"]
                usage = tracker.snapshot_since(usage_mark)
                results.append({
                    "sample_id": sample.id,
                    "split": sample.split,
                    "variant": variant_name,
                    "status": "completed",
                    "rule_first_pass": offline_first_pass,
                    "rule_final_pass": offline_final_pass,
                    "pipeline_first_pass": bool(pipeline.get("pipeline_first_pass", pipeline.get("rule_first_pass", False))),
                    "pipeline_final_pass": bool(pipeline.get("pipeline_final_pass", pipeline.get("rule_final_pass", False))),
                    "retries": int(pipeline.get("writer_retries", 0)) + int(pipeline.get("editor_retries", 0)),
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cost": usage["cost"],
                    "latency_ms": usage["latency_ms"],
                    "judge_score": judge_score,
                    "ref_edit_ratio": ref_edit_ratio,
                    "fewshot_example_ids": pipeline.get("fewshot_example_ids", []),
                    "fewshot_note": (
                        ("examples_loaded" if example_block else "无示例")
                        if variant_config["fewshot_enabled"] else None
                    ),
                    "auto_approved": bool(pipeline.get("auto_approved")),
                    "final_quality_report": pipeline.get("final_quality_report"),
                    "final_output": pipeline["final_output"],
                })
            except Exception as exc:
                budget_error = exc
                while budget_error is not None and not isinstance(budget_error, BudgetExceeded):
                    budget_error = budget_error.__cause__ or budget_error.__context__
                if budget_error is None:
                    raise
                stopped_reason = str(budget_error)
                break
        if stopped_reason:
            break

    summary = build_summary(results, list(selected_variants), seed=seed)
    dataset_bytes = Path(dataset_path).read_bytes()
    model_name = "MockProvider" if provider_name == "mock" else "配置中的当前模型"
    judge_name = "MockProvider" if judge_provider_name == "mock" else "配置中的当前模型"
    try:
        from config.settings import settings

        if provider_name == "config":
            model_name = settings.LLM_MODEL
        if judge_provider_name == "config":
            judge_name = settings.LLM_MODEL
    except Exception:
        pass
    run_meta = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "split": split,
        "variants": list(selected_variants),
        "variant_config": selected_variants,
        "provider": provider_name,
        "model_name": model_name,
        "judge_provider": judge_provider_name,
        "judge_model_name": judge_name,
        "judge_uses_generation_model": judge_provider_name == provider_name,
        "git_commit": _git_revision(),
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "dataset_samples": [sample.id for sample in samples],
        "seed": seed,
        "budget": {
            "max_total_tokens": max_total_tokens,
            **estimate,
            "actual_tokens": tracker.total_tokens,
            "formula_note": "按输入字符数 / 2 与所有节点、重试及裁判的 max_tokens 上限保守估算。",
        },
        "examples_source": examples_source or "none",
        "examples_count": len(pairs),
        "holdout_warning": holdout_warning,
        "stopped_reason": stopped_reason,
        "result_rows": len(results),
    }
    _write_outputs(output_path, results, summary, run_meta)
    return {
        "results": results,
        "summary": summary,
        "run_meta": run_meta,
        "estimate": estimate,
        "holdout_warning": holdout_warning,
        "stopped_reason": stopped_reason,
    }
