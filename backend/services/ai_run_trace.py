"""Build the bounded, privacy-safe response for a single AI run trace."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from harness.llm_call_logger import MAX_PROMPT_SNAPSHOT_CHARS


def _duration_ms(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    if started_at is None or ended_at is None:
        return None
    start = started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None else started_at
    end = ended_at.replace(tzinfo=timezone.utc) if ended_at.tzinfo is None else ended_at
    return max(0, int((end - start).total_seconds() * 1000))


def _quality_snapshot(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    candidates = [output]
    for key in ("draft_quality_report", "edited_quality_report", "final_quality_report", "quality"):
        value = output.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for report in candidates:
        issues = report.get("issues")
        if "stage" not in report or "passed" not in report or not isinstance(issues, list):
            continue
        return {
            "stage": report.get("stage"),
            "passed": bool(report.get("passed")),
            "error_codes": [
                str(issue.get("code"))
                for issue in issues
                if isinstance(issue, dict)
                and str(issue.get("severity", "")).upper() == "ERROR"
                and issue.get("code") is not None
            ],
        }
    return None


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def build_ai_run_trace(run, steps: list[Any], logs: list[Any], *, include_prompts: bool = False) -> dict[str, Any]:
    """Format an already-authorized run, with 500/50 response caps."""
    ordered_steps = sorted(steps, key=lambda item: (item.step_order, item.started_at is None, item.started_at or item.created_at))
    failed = next(
        (
            step for step in ordered_steps
            if str(step.status).upper() in {"FAILED", "ERROR"} or bool(step.error_message)
        ),
        None,
    )
    visible_steps = ordered_steps[:500]
    logs_by_step: dict[str, list[Any]] = {}
    structured_context_repairs: dict[str, int] = {}
    for log in sorted(logs, key=lambda item: (item.created_at, str(item.id))):
        if log.step_id is not None:
            logs_by_step.setdefault(str(log.step_id), []).append(log)
        request = log.request if isinstance(log.request, dict) else {}
        structured = request.get("structured")
        if isinstance(structured, dict):
            key = str(
                request.get("structured_context_id")
                or f"{log.step_id}:{log.agent_name}:{structured.get('schema')}"
            )
            structured_context_repairs[key] = max(
                structured_context_repairs.get(key, 0), _integer(structured.get("repairs")),
            )

    truncated = len(ordered_steps) > 500
    rendered_steps: list[dict[str, Any]] = []
    rendered_calls: list[dict[str, Any]] = []
    for step in visible_steps:
        step_logs = logs_by_step.get(str(step.id), [])
        if len(step_logs) > 50:
            truncated = True
        calls: list[dict[str, Any]] = []
        for log in step_logs[:50]:
            request = log.request if isinstance(log.request, dict) else {}
            structured = request.get("structured")
            prompt = log.rendered_prompt_snapshot
            prompt_truncated = bool(request.get("prompt_snapshot_truncated"))
            if include_prompts and isinstance(prompt, str) and len(prompt) > MAX_PROMPT_SNAPSHOT_CHARS:
                prompt = prompt[:MAX_PROMPT_SNAPSHOT_CHARS]
                prompt_truncated = True
            elif not include_prompts:
                prompt = None
                prompt_truncated = None

            input_tokens = log.input_tokens
            output_tokens = log.output_tokens
            total_tokens = log.total_tokens
            if total_tokens is None and (input_tokens is not None or output_tokens is not None):
                total_tokens = _integer(input_tokens) + _integer(output_tokens)
            call = {
                "id": log.id,
                "agent_name": log.agent_name,
                "model": log.model,
                "latency_ms": log.latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost": float(log.cost) if log.cost is not None else None,
                "retries": _integer(request.get("retries")),
                "usage_estimated": bool(request.get("usage_estimated", False)),
                "structured": structured if isinstance(structured, dict) else None,
                "error_message": log.error_message,
                "rendered_prompt_snapshot": prompt,
                "prompt_truncated": prompt_truncated,
            }
            calls.append(call)
            rendered_calls.append(call)

        status = str(step.status)
        if status.upper() == "SUCCESS":
            status = "COMPLETED"
        rendered_steps.append({
            "step_order": step.step_order,
            "step_name": step.step_name,
            "node_key": step.node_key,
            "agent_name": step.agent_name,
            "status": status,
            "started_at": step.started_at,
            "ended_at": step.ended_at,
            "duration_ms": _duration_ms(step.started_at, step.ended_at),
            "retry_count": step.retry_count,
            "max_retry": step.max_retry,
            "error_message": step.error_message,
            "quality": _quality_snapshot(step.output),
            "llm_calls": calls,
        })

    input_tokens = sum(_integer(call["input_tokens"]) for call in rendered_calls)
    output_tokens = sum(_integer(call["output_tokens"]) for call in rendered_calls)
    total_tokens = sum(_integer(call["total_tokens"]) for call in rendered_calls)
    costs = sum(float(call["cost"] or 0) for call in rendered_calls)
    retries = sum(_integer(call["retries"]) for call in rendered_calls)
    return {
        "run": {
            "id": run.id,
            "request_id": run.request_id,
            "status": run.status,
            "run_type": run.run_type,
            "mode": run.mode,
            "workflow_key": run.workflow_key,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_ms": _duration_ms(run.started_at, run.finished_at),
            "error_message": run.error_message,
        },
        "totals": {
            "llm_calls": len(rendered_calls),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost": round(costs, 6),
            "retries": retries,
            "estimated_calls": sum(bool(call["usage_estimated"]) for call in rendered_calls),
            "structured_repairs": sum(structured_context_repairs.values()),
        },
        "failed_step": ({
            "step_order": failed.step_order,
            "step_name": failed.step_name,
            "agent_name": failed.agent_name,
            "error_message": failed.error_message,
        } if failed is not None else None),
        "steps": rendered_steps,
        "truncated": truncated,
    }
