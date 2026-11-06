"""Validated, bounded tools for the minimal agent demo."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.llm_types import LLMResult, ToolSpec
from services.chapter_quality import count_non_space_chars
from services.chapter_rules import validate_task_card_compliance

from .store import ProjectStore


_TRUNCATION = "…[已截断]"


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ReadOutlineArgs(_ToolArgs):
    chapter_no: int | None = None


class _GetCharacterArgs(_ToolArgs):
    name: str = Field(min_length=1)


class _SearchChaptersArgs(_ToolArgs):
    query: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=5)


class _WriteSceneArgs(_ToolArgs):
    scene_brief: str = Field(min_length=1)
    character_names: list[str] = Field(default_factory=list)
    target_chars: int = Field(default=600, ge=1)
    must_include: list[str] = Field(default_factory=list)


class _RunValidatorsArgs(_ToolArgs):
    draft_id: str = Field(min_length=1)
    must_include: list[str] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    _rendered_observation: str | None = field(default=None, repr=False)

    def observation(self, max_chars: int = 1_500) -> str:
        if self._rendered_observation is not None:
            if len(self._rendered_observation) <= max_chars:
                return self._rendered_observation
            if max_chars <= len(_TRUNCATION):
                return self._rendered_observation[:max_chars]
            return self._rendered_observation[: max_chars - len(_TRUNCATION)] + _TRUNCATION
        value = {"ok": self.ok}
        if self.error is not None:
            value["error"] = self.error
        if self.data is not None:
            value["data"] = self.data
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(rendered) <= max_chars:
            return rendered
        if max_chars <= len(_TRUNCATION):
            return rendered[:max_chars]
        return rendered[: max_chars - len(_TRUNCATION)] + _TRUNCATION


@dataclass(slots=True)
class ToolRuntime:
    store: ProjectStore
    provider: Any
    drafts: dict[str, str] = field(default_factory=dict)
    record_usage: Callable[[LLMResult, str, str], int] | None = None


@dataclass(slots=True)
class _Tool:
    name: str
    description: str
    args_model: type[_ToolArgs]
    handler: Callable[..., Any]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _bigrams(value: str) -> set[str]:
    compact = _compact(value)
    return {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}


def _error_from_validation(exc: ValidationError) -> str:
    parts = []
    for issue in exc.errors(include_input=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "参数"
        parts.append(f"{location}: {issue.get('msg', '无效值')}")
    return "参数校验失败：" + "；".join(parts)


class ToolRegistry:
    def __init__(self, runtime: ToolRuntime, *, max_observation_chars: int = 1_500):
        self.runtime = runtime
        self.max_observation_chars = max_observation_chars
        self._tools: dict[str, _Tool] = {
            "read_outline": _Tool("read_outline", "读取全部大纲或指定章节的大纲。", _ReadOutlineArgs, self._read_outline),
            "get_character": _Tool("get_character", "按姓名或别名读取角色卡。", _GetCharacterArgs, self._get_character),
            "search_chapters": _Tool("search_chapters", "在已写章节中按关键词检索段落。", _SearchChaptersArgs, self._search_chapters),
            "write_scene": _Tool("write_scene", "调用模型写场景，只返回草稿编号和短预览。", _WriteSceneArgs, self._write_scene),
            "run_validators": _Tool("run_validators", "对草稿运行确定性任务卡规则校验。", _RunValidatorsArgs, self._run_validators),
        }

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(tool.name, tool.description, tool.args_model.model_json_schema())
            for tool in self._tools.values()
        ]

    def _bounded(self, result: ToolResult) -> ToolResult:
        result._rendered_observation = result.observation(self.max_observation_chars)
        return result

    async def execute(self, name: str, arguments: dict[str, Any], *, arguments_error: str | None = None) -> ToolResult:
        if arguments_error:
            return self._bounded(ToolResult(False, error=f"模型参数解析失败：{arguments_error}"))
        tool = self._tools.get(name)
        if tool is None:
            return self._bounded(ToolResult(False, error=f"未知工具：{name}。可用工具：{', '.join(self._tools)}"))
        try:
            parsed = tool.args_model.model_validate(arguments)
            result = await tool.handler(**parsed.model_dump())
            if not isinstance(result, ToolResult):
                result = ToolResult(True, data=result)
            # Materialize the bounded observation here so even an unexpectedly large
            # tool result cannot enter the next model prompt unbounded.
            return self._bounded(result)
        except ValidationError as exc:
            return self._bounded(ToolResult(False, error=_error_from_validation(exc)))
        except Exception as exc:  # Tool failures are observations, never loop failures.
            return self._bounded(ToolResult(False, error=f"工具执行失败：{type(exc).__name__}: {exc}"))

    async def _read_outline(self, chapter_no: int | None = None) -> ToolResult:
        outline = self.runtime.store.get_outline()
        if chapter_no is not None:
            chapter = next((row for row in outline if row.get("chapter_no") == chapter_no), None)
            if chapter is None:
                return ToolResult(False, error=f"大纲中不存在第 {chapter_no} 章。")
            return ToolResult(True, data=chapter)
        return ToolResult(True, data=[
            {key: row.get(key) for key in ("chapter_no", "title", "summary")}
            for row in outline
        ])

    async def _get_character(self, name: str) -> ToolResult:
        character = self.runtime.store.get_character(name)
        if character is None:
            available = "、".join(self.runtime.store.list_character_names())
            return ToolResult(False, error=f"找不到角色“{name}”。可用角色名：{available}")
        return ToolResult(True, data=character)

    async def _search_chapters(self, query: str, top_k: int = 3) -> ToolResult:
        query_grams = _bigrams(query)
        ranked: list[tuple[float, int, int, str]] = []
        for chapter in self.runtime.store.get_chapters():
            body = str(chapter.get("text", ""))
            paragraphs = [part.strip() for part in re.split(r"\n+", body) if part.strip()]
            for paragraph_no, paragraph in enumerate(paragraphs, 1):
                compact_para = _compact(paragraph)
                if query_grams:
                    overlap = len(query_grams & _bigrams(paragraph)) / len(query_grams)
                    score = overlap + (1.0 if _compact(query) in compact_para else 0.0)
                else:
                    score = 1.0 if _compact(query) in compact_para else 0.0
                if score > 0:
                    ranked.append((score, int(chapter.get("chapter_no", 0)), paragraph_no, paragraph))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        matches = [
            {
                "chapter_no": chapter_no,
                "paragraph_no": paragraph_no,
                "snippet": paragraph[:120],
                "score": round(score, 4),
            }
            for score, chapter_no, paragraph_no, paragraph in ranked[:top_k]
        ]
        return ToolResult(True, data=matches)

    async def _write_scene(
        self,
        scene_brief: str,
        character_names: list[str],
        target_chars: int = 600,
        must_include: list[str] | None = None,
    ) -> ToolResult:
        cards = [card for name in character_names if (card := self.runtime.store.get_character(name))]
        system_prompt = "你是小说场景写作者。只输出场景正文，不解释规则。"
        user_prompt = json.dumps({
            "scene_brief": scene_brief,
            "target_chars": target_chars,
            "must_include": must_include or [],
            "characters": cards,
        }, ensure_ascii=False)
        max_tokens = max(64, min(1_500, math.ceil(target_chars * 1.5)))
        started = time.monotonic()
        result = await self.runtime.provider.complete(
            system_prompt,
            user_prompt,
            temperature=0.8,
            max_tokens=max_tokens,
            tools=None,
        )
        if self.runtime.record_usage is not None:
            self.runtime.record_usage(result, system_prompt, user_prompt)
        draft_id = f"d{len(self.runtime.drafts) + 1}"
        self.runtime.drafts[draft_id] = result.text
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ToolResult(True, data={
            "draft_id": draft_id,
            "chars": count_non_space_chars(result.text),
            "preview": result.text[:80],
            "elapsed_ms": elapsed_ms,
        })

    async def _run_validators(
        self,
        draft_id: str,
        must_include: list[str] | None = None,
        must_not_include: list[str] | None = None,
    ) -> ToolResult:
        if draft_id not in self.runtime.drafts:
            return ToolResult(False, error=f"不存在草稿：{draft_id}")
        task_card = {
            "scenes": [{
                "must_include": must_include or [],
                "must_not_include": must_not_include or [],
            }],
            "forbidden": [],
        }
        get_all = getattr(self.runtime.store, "get_all_characters", None)
        characters = get_all() if callable(get_all) else [
            card for name in self.runtime.store.list_character_names()
            if (card := self.runtime.store.get_character(name))
        ]
        text = self.runtime.drafts[draft_id]
        issues = validate_task_card_compliance(text, task_card, characters)
        return ToolResult(True, data={
            "passed": not any(issue.severity == "ERROR" for issue in issues),
            "chars": count_non_space_chars(text),
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "evidence": (issue.evidence or "")[:200],
                }
                for issue in issues
            ],
        })
