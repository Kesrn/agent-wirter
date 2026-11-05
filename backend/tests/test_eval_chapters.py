"""Offline tests for the chapter evaluation runner."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.llm_types import LLMResult, TokenUsage
from eval_chapters.dataset import load_dataset
from eval_chapters.runner import run_batch
from eval_chapters.stats import paired_bootstrap, wilson_interval


DATASET = Path(__file__).parent / "data" / "eval_chapters_sample.jsonl"


def test_wilson_interval_matches_known_value_and_bootstrap_is_reproducible():
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=0.0001)
    assert high == pytest.approx(0.7634, abs=0.0001)

    first = paired_bootstrap([1.0, -1.0, 1.0, -1.0], seed=41)
    second = paired_bootstrap([1.0, -1.0, 1.0, -1.0], seed=41)
    assert first == second
    assert first["n"] == 4
    assert first["ci95"][0] <= 0 <= first["ci95"][1]
    with pytest.raises(ValueError, match="至少重采样 5000 次"):
        paired_bootstrap([1.0], resamples=4999)


def test_dataset_has_three_synthetic_rows_and_rejects_duplicate_ids(tmp_path):
    rows = load_dataset(DATASET, split="dev")
    assert len(rows) == 3
    assert all(row.reference is None for row in rows)

    duplicate = tmp_path / "duplicate.jsonl"
    row = {
        "id": "same", "split": "dev", "source": "合成内容", "target_words": 100,
        "context": {}, "task_card": {}, "reference": None,
    }
    duplicate.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="样本 id 重复"):
        load_dataset(duplicate, split="dev")


@pytest.mark.asyncio
async def test_run_batch_writes_reconcilable_outputs_and_captures_fewshot(tmp_path):
    prompts = []

    async def pipeline(sample, variant, config, *, provider_factory, example_block, example_ids):
        provider = provider_factory()
        await provider.complete("system", "user")
        prompts.append((variant, example_block if config["fewshot_enabled"] else "", config.copy()))
        return {
            "final_output": f"输出-{sample.id}-{variant}",
            "rule_first_pass": variant != "baseline",
            "rule_final_pass": True,
            "writer_retries": 0,
            "editor_retries": 0,
            "fewshot_example_ids": example_ids if config["fewshot_enabled"] else [],
            "auto_approved": True,
        }

    async def judge(sample, output, *, provider_factory):
        await provider_factory().complete("judge", output)
        return 4.0

    class CountedProvider:
        model = "test-mock"

        async def complete(self, system, user, **kwargs):
            return LLMResult(
                text="ok", model=self.model,
                usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5),
            )

        async def generate(self, *args, **kwargs):
            return "ok"

    from services.edit_examples import EditPair

    pairs = [EditPair(before="旧句子包含足够多的内容", after="新句子体现更清晰的表达")]
    sample = load_dataset(DATASET, split="dev", limit=1)[0]
    result = await run_batch(
        dataset_path=DATASET, split="dev", variant_names=["baseline", "rules_fewshot"],
        provider_name="mock", output_dir=tmp_path / "run", seed=3,
        max_total_tokens=70000, example_pairs=pairs, example_ids=["example-1"],
        provider_factory=CountedProvider, judge_provider_factory=CountedProvider,
        pipeline_runner=pipeline, judge_runner=judge, limit=1,
    )
    out = tmp_path / "run"
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    summary = json.loads((out / "summary.json").read_text())
    assert len(rows) == 2
    assert all(row["sample_id"] == sample.id for row in rows)
    assert all(row["input_tokens"] == 4 and row["output_tokens"] == 6 for row in rows)
    assert summary["by_variant"]["baseline"]["sample_count"] == 1
    assert summary["by_variant"]["rules_fewshot"]["rule_first_pass"]["successes"] == 1
    compare = (out / "compare.md").read_text()
    assert "样本量警告" in compare
    assert "baseline" in compare and "rules_fewshot" in compare
    assert len((out / "run_meta.json").read_text()) > 0
    assert result["run_meta"]["result_rows"] == 2
    assert prompts[0][0] == "baseline" and prompts[0][1] == ""
    assert prompts[1][0] == "rules_fewshot" and prompts[1][2]["rules_enabled"] is True
    assert "人工修改示例" in prompts[1][1]
    assert summary["baseline_variant"] == "baseline"


@pytest.mark.asyncio
async def test_budget_preflight_makes_no_provider_calls(tmp_path):
    calls = 0

    class NeverProvider:
        async def complete(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("预检超预算后不得调用 provider")

    with pytest.raises(ValueError, match="预算预检中止"):
        await run_batch(
            dataset_path=DATASET, split="dev", variant_names=["baseline", "rules"],
            provider_name="mock", output_dir=tmp_path / "run", max_total_tokens=1,
            provider_factory=NeverProvider,
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_real_graph_runs_in_memory_and_fewshot_reaches_writer_prompt(tmp_path):
    from agents.mock_provider import MockProvider
    from services.edit_examples import EditPair

    prompts = []

    class CapturingMockProvider(MockProvider):
        async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
            prompts.append((system_prompt, user_prompt))
            return await super().generate(system_prompt, user_prompt, temperature, max_tokens)

    database_url = os.environ["DATABASE_URL"]
    sqlite_tail = database_url.split("////", 1)[-1]
    database_path = Path("/") / sqlite_tail
    assert not database_path.exists()
    result = await run_batch(
        dataset_path=DATASET,
        split="dev",
        variant_names=["baseline", "rules_fewshot"],
        provider_name="mock",
        output_dir=tmp_path / "graph-run",
        max_total_tokens=70000,
        limit=1,
        example_pairs=[EditPair(before="旧表达有些含混，人物没有说明顾虑。", after="她停了一会儿，才把真正担心的事情说出来。")],
        example_ids=["synthetic-edit-1"],
        provider_factory=CapturingMockProvider,
        judge_provider_factory=CapturingMockProvider,
    )
    assert result["run_meta"]["result_rows"] == 2
    assert any(row["auto_approved"] for row in result["results"]), result["results"]
    writer_prompts = [
        user for _, user in prompts
        if "请根据章节任务卡和上下文生成本章完整正文" in user
    ]
    assert len(writer_prompts) >= 2
    assert "【过往人工修改示例" not in writer_prompts[0]
    assert any("【过往人工修改示例" in user for user in writer_prompts[1:])
    assert any(row["fewshot_example_ids"] == ["synthetic-edit-1"] for row in result["results"])
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_holdout_requires_confirmation_and_warns_on_repeat(tmp_path):
    async def pipeline(sample, variant, config, **kwargs):
        return {"final_output": "文本", "rule_first_pass": True, "rule_final_pass": True}

    async def judge(*args, **kwargs):
        return 4.0

    holdout_dataset = tmp_path / "holdout.jsonl"
    row = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    row["split"] = "holdout"
    holdout_dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必须显式提供 --confirm-holdout"):
        await run_batch(
            dataset_path=holdout_dataset, split="holdout", variant_names=["baseline"],
            provider_name="mock", output_dir=tmp_path / "out" / "run", max_total_tokens=40000,
            pipeline_runner=pipeline, judge_runner=judge,
        )
    kwargs = dict(
        dataset_path=holdout_dataset, split="holdout", variant_names=["baseline"],
        provider_name="mock", output_dir=tmp_path / "out" / "run", max_total_tokens=40000,
        confirm_holdout=True, limit=1, pipeline_runner=pipeline, judge_runner=judge,
    )
    first = await run_batch(**kwargs)
    second = await run_batch(**kwargs)
    assert first["holdout_warning"] is None
    assert second["holdout_warning"].startswith("留出集已被运行过 1 次")
    assert len((tmp_path / "out" / "holdout_runs.log").read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_runtime_budget_stop_preserves_completed_results(tmp_path):
    class ExpensiveProvider:
        model = "test-expensive"
        calls = 0

        async def complete(self, *args, **kwargs):
            type(self).calls += 1
            usage = (10000, 10000) if type(self).calls <= 2 else ((40000, 0) if type(self).calls == 3 else (20000, 0))
            return LLMResult(
                text="ok", model=self.model,
                usage=TokenUsage(input_tokens=usage[0], output_tokens=usage[1], total_tokens=sum(usage)),
            )

        async def generate(self, *args, **kwargs):
            return "ok"

    async def pipeline(sample, variant, config, *, provider_factory, **kwargs):
        await provider_factory().complete("system", "user")
        return {"final_output": "文本", "rule_first_pass": True, "rule_final_pass": True}

    async def judge(sample, output, *, provider_factory):
        await provider_factory().complete("judge", output)
        return 4.0

    result = await run_batch(
        dataset_path=DATASET, split="dev", variant_names=["baseline", "rules"],
        provider_name="mock", output_dir=tmp_path / "run", max_total_tokens=70000,
        limit=1, provider_factory=ExpensiveProvider, judge_provider_factory=ExpensiveProvider,
        pipeline_runner=pipeline, judge_runner=judge,
    )
    rows = [json.loads(line) for line in (tmp_path / "run" / "results.jsonl").read_text().splitlines()]
    assert result["stopped_reason"] is not None, result
    assert len(rows) == 1
