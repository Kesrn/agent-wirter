"""Evaluation dataset runner and LLM-as-judge helpers."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from agents.llm_provider import LLMProvider
from models.evaluation import EvaluationCase
from models.project import Project

DEFAULT_RUBRICS: dict[str, dict[str, str]] = {
    "novel": {
        "requirement_following": "是否遵守输入里的硬性要求、角色、世界观和章节目标",
        "context_consistency": "是否不违背已有设定，是否避免引入无依据的新关键设定",
        "plot_progress": "是否推动剧情或信息增量，而不是空转",
        "prose_quality": "中文表达、节奏、画面感和可读性",
    },
    "article": {
        "requirement_following": "是否遵守 brief、平台、受众和内容目标",
        "structure_quality": "标题、开头、主体、收束和行动引导是否清晰",
        "audience_match": "语言和论证是否匹配目标受众",
        "risk_control": "是否避免事实、合规、夸大或版权风险",
    },
}


def default_rubric(project_mode: str) -> dict[str, str]:
    return dict(DEFAULT_RUBRICS.get(project_mode, DEFAULT_RUBRICS["novel"]))


def normalize_expected_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(value)]


def normalize_rubric(value: Any, project_mode: str) -> dict[str, str]:
    if isinstance(value, dict) and value:
        return {str(key): str(val) for key, val in value.items() if str(key).strip()}
    return default_rubric(project_mode)


def _json_block(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _number(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return max(1.0, min(5.0, num))


def parse_judge_response(text: str, rubric: dict[str, str]) -> dict[str, Any]:
    parsed = _json_block(text) or {}
    raw_scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    scores: dict[str, float] = {}
    for key in rubric:
        score = _number(raw_scores.get(key))
        if score is not None:
            scores[key] = score

    overall = _number(parsed.get("overall_score"))
    if overall is None and scores:
        overall = round(sum(scores.values()) / len(scores), 2)
    if overall is None:
        overall = 1.0

    passed = parsed.get("passed")
    if not isinstance(passed, bool):
        passed = overall >= 3.5

    feedback = parsed.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        feedback = "评测模型未返回明确反馈。"

    return {
        "score": overall,
        "scores": scores,
        "passed": passed,
        "feedback": feedback.strip(),
        "raw": parsed or {"text": text},
    }


def build_generation_prompt(project: Project, case: EvaluationCase, rubric: dict[str, str], expected: list[str]) -> tuple[str, str]:
    if project.mode == "article":
        system = (
            "你是一位中文文章/文案创作者。根据评测样本输入生成候选输出。"
            "不要解释评测规则，只输出可评测的正文。"
        )
    else:
        system = (
            "你是一位中文小说创作者。根据评测样本输入生成候选输出。"
            "不要解释评测规则，只输出可评测的正文。"
        )

    user = (
        f"## 项目\n{project.title}\n\n"
        f"## 任务类型\n{case.task_type}\n\n"
        f"## 评测样本输入\n{case.input_text or '无'}\n\n"
        f"## 期望属性\n{json.dumps(expected, ensure_ascii=False)}\n\n"
        f"## 评分维度\n{json.dumps(rubric, ensure_ascii=False)}\n\n"
        "请生成候选输出。"
    )
    return system, user


def build_judge_prompt(case: EvaluationCase, rubric: dict[str, str], expected: list[str], output: str) -> tuple[str, str]:
    system = (
        "你是严格的创作质量评测裁判。请只输出严格 JSON，不要输出 Markdown。"
        "JSON 格式：{\"overall_score\": 1-5, \"scores\": {维度: 1-5}, "
        "\"passed\": true|false, \"feedback\": \"简短中文反馈\"}。"
        "分数 1 表示严重不合格，5 表示优秀。"
    )
    user = (
        f"## 任务类型\n{case.task_type}\n\n"
        f"## 输入\n{case.input_text or '无'}\n\n"
        f"## 期望属性\n{json.dumps(expected, ensure_ascii=False)}\n\n"
        f"## 评分维度\n{json.dumps(rubric, ensure_ascii=False)}\n\n"
        f"## 参考输出\n{case.reference_output or '无'}\n\n"
        f"## 候选输出\n{output}\n\n"
        "请按评分维度逐项评分，并给出 overall_score。"
    )
    return system, user


async def run_evaluation_case(
    *,
    provider: LLMProvider,
    project: Project,
    case: EvaluationCase,
    generation_mode: str,
) -> dict[str, Any]:
    start = time.perf_counter()
    rubric = normalize_rubric(case.rubric, project.mode)
    expected = normalize_expected_properties(case.expected_properties)

    generated_output = case.actual_output or ""
    if generation_mode == "generate_and_judge":
        system, user = build_generation_prompt(project, case, rubric, expected)
        generated_output = await provider.generate(system, user, temperature=0.7, max_tokens=2048)
    elif not generated_output.strip():
        raise ValueError("judge_only 模式需要样本提供候选输出")

    judge_system, judge_user = build_judge_prompt(case, rubric, expected, generated_output)
    judge_text = await provider.generate(judge_system, judge_user, temperature=0.1, max_tokens=1200)
    parsed = parse_judge_response(judge_text, rubric)
    parsed["generated_output"] = generated_output
    parsed["latency_ms"] = int((time.perf_counter() - start) * 1000)
    return parsed
