"""Pydantic-validated structured generation helpers for LLM providers."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ValidationError

from agents.llm_types import LLMResult, TokenUsage

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)
_JSON_MODE_SUPPORT: WeakKeyDictionary[Any, bool] = WeakKeyDictionary()


class StructuredOutputError(ValueError):
    """Structured response could not be parsed and validated after repairs."""

    def __init__(self, schema_name: str, last_errors: list[str], raw_text: str):
        self.schema_name = schema_name
        self.last_errors = list(last_errors[:10])
        self.raw_text = raw_text
        detail = "; ".join(self.last_errors) or "未知结构错误"
        super().__init__(f"{schema_name} 输出校验失败：{detail}")


@dataclass
class StructuredResult:
    value: BaseModel
    raw_text: str
    repairs: int
    usage: TokenUsage | None
    json_mode_used: bool


StructuredLogUpdater = Callable[[list[str], dict[str, Any]], Awaitable[None]]


@dataclass
class StructuredLogContext:
    """Mutable trace shared with LoggedLLMProvider until validation finishes."""

    schema_name: str
    context_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    repairs: int = 0
    ok: bool = False
    _logs: list[tuple[str, StructuredLogUpdater]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {"schema": self.schema_name, "repairs": self.repairs, "ok": self.ok}

    def register(self, log_id: str, updater: StructuredLogUpdater) -> None:
        self._logs.append((log_id, updater))

    async def flush(self) -> None:
        if not self._logs:
            return
        updates: dict[int, tuple[StructuredLogUpdater, list[str]]] = {}
        for log_id, updater in self._logs:
            key = id(updater.__self__) if getattr(updater, "__self__", None) else id(updater)
            if key not in updates:
                updates[key] = (updater, [])
            updates[key][1].append(log_id)
        for updater, log_ids in updates.values():
            try:
                await updater(log_ids, self.snapshot())
            except Exception:
                logger.warning("结构化输出日志元数据更新失败", exc_info=True)


_STRUCTURED_LOG_CONTEXT: ContextVar[StructuredLogContext | None] = ContextVar(
    "structured_llm_log_context", default=None
)


def current_structured_log_context() -> StructuredLogContext | None:
    return _STRUCTURED_LOG_CONTEXT.get()


def _extract_balanced_object(raw: str) -> str | None:
    for start, char in enumerate(raw):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(raw)):
            current = raw[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    return raw[start : end + 1]
    return None


def _escape_control_characters(payload: str) -> str:
    escaped_chars = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    result: list[str] = []
    in_string = False
    escaped = False
    for char in payload:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == '"':
                result.append(char)
                in_string = False
                continue
            if ord(char) < 0x20:
                result.append(escaped_chars.get(char, f"\\u{ord(char):04x}"))
                continue
            result.append(char)
            continue
        result.append(char)
        if char == '"':
            in_string = True
    return "".join(result)


def extract_json_object(raw: str | None) -> str | None:
    """Return the first balanced JSON object, sanitized for raw control chars.

    Markdown fences and surrounding prose are ignored by locating the first complete
    object. The return value is the JSON source string so callers can preserve their
    own parse and validation errors.
    """
    payload = _extract_balanced_object(raw or "")
    return _escape_control_characters(payload) if payload is not None else None


def _format_validation_errors(errors: list[dict[str, Any]]) -> list[str]:
    formatted: list[str] = []
    for error in errors[:10]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "$"
        message = str(error.get("msg") or "无效值")
        formatted.append(f"{location}: {message}")
    return formatted or ["$: 输出不符合 schema"]


def _parse_error(raw_text: str) -> tuple[dict[str, Any] | None, list[str]]:
    extracted = extract_json_object(raw_text)
    if extracted is None:
        return None, ["$: 未找到完整 JSON 对象"]
    try:
        parsed = json.loads(extracted)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"$: JSON 解析失败（位置 {getattr(exc, 'pos', '?')}）：{exc.msg if isinstance(exc, json.JSONDecodeError) else exc}"]
    if not isinstance(parsed, dict):
        return None, ["$: JSON 根节点必须是对象"]
    return parsed, []


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_json_mode_unsupported(error: BaseException) -> bool:
    if _status_code(error) != 400:
        return False
    message = str(error).lower()
    unsupported = any(
        word in message
        for word in ("unsupported", "not supported", "not support", "unknown", "unrecognized", "invalid")
    )
    return "response_format" in message or ("json" in message and unsupported)


async def _complete(
    provider: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
) -> LLMResult:
    complete_method = getattr(provider, "complete", None)
    if callable(complete_method):
        kwargs: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        if response_format is not None:
            kwargs["response_format"] = response_format
        result = await complete_method(system_prompt, user_prompt, **kwargs)
        if isinstance(result, LLMResult):
            return result
        if isinstance(result, str):
            return LLMResult(result, None, [], None, getattr(provider, "model", None), 0, 0)
        raise TypeError("provider.complete 必须返回 LLMResult")

    # Older duck-typed test/demo providers implement only generate(); keep them usable.
    text = await provider.generate(
        system_prompt,
        user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return LLMResult(text, None, [], None, getattr(provider, "model", None), 0, 0)


def _remember_json_mode(provider: Any, supported: bool) -> None:
    try:
        setattr(provider, "_structured_json_mode_supported", supported)
    except (AttributeError, TypeError):
        try:
            _JSON_MODE_SUPPORT[provider] = supported
        except TypeError:
            # Objects that cannot be weakly referenced (or hashed) are not cached.
            pass


def _json_mode_supported(provider: Any) -> bool:
    remembered = getattr(provider, "_structured_json_mode_supported", None)
    if remembered is not None:
        return bool(remembered)
    try:
        return _JSON_MODE_SUPPORT.get(provider, True)
    except TypeError:
        return True


def _merge_usage(usages: list[TokenUsage]) -> TokenUsage | None:
    if not usages:
        return None
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        total_tokens=sum(item.total_tokens for item in usages),
        estimated=any(item.estimated for item in usages),
    )


async def generate_structured(
    provider,
    schema: type[SchemaT],
    system_prompt: str,
    user_prompt: str,
    *,
    max_repairs: int = 1,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    json_mode: str = "auto",
) -> StructuredResult:
    """Generate JSON, validate it, and ask the model to repair invalid output."""
    if json_mode not in {"auto", "on", "off"}:
        raise ValueError("json_mode 必须为 auto、on 或 off")
    max_repairs = max(0, int(max_repairs))
    schema_name = schema.__name__
    trace = StructuredLogContext(schema_name=schema_name)
    trace_token = _STRUCTURED_LOG_CONTEXT.set(trace)
    usage_items: list[TokenUsage] = []
    raw_text = ""
    repairs = 0
    json_mode_used = False
    should_use_json_mode = json_mode != "off" and _json_mode_supported(provider)

    try:
        for attempt in range(max_repairs + 1):
            if attempt:
                errors_text = "\n".join(f"- {item}" for item in last_errors[:10])
                schema_repair_instruction = (
                    "请诊断并输出 StructuralCritique JSON；本次只做上次结果的结构修复。\n"
                    if schema_name == "StructuralCritique"
                    else f"请输出符合 {schema_name} 的 JSON。\n"
                )
                repair_prompt = (
                    schema_repair_instruction
                    + f"这是 {schema_name} 上一次输出的校验结果。请只修复字段错误并输出一个 JSON 对象，"
                    "不要输出 Markdown、解释或其他文字。\n"
                    f"具体校验错误：\n{errors_text}\n\n"
                    f"上一次输出（最多 2000 字符）：\n{raw_text[:2000]}"
                )
                current_user_prompt = repair_prompt
                repairs = attempt
                trace.repairs = repairs
            else:
                current_user_prompt = user_prompt

            response_format = {"type": "json_object"} if should_use_json_mode else None
            try:
                result = await _complete(
                    provider,
                    system_prompt,
                    current_user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except Exception as exc:
                if response_format is not None and _is_json_mode_unsupported(exc):
                    should_use_json_mode = False
                    _remember_json_mode(provider, False)
                    result = await _complete(
                        provider,
                        system_prompt,
                        current_user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=None,
                    )
                    json_mode_used = False
                else:
                    raise
            else:
                if response_format is not None:
                    _remember_json_mode(provider, True)
                    json_mode_used = True

            if result.usage is not None:
                usage_items.append(result.usage)
            raw_text = result.text or ""
            parsed, last_errors = _parse_error(raw_text)
            if parsed is not None:
                try:
                    value = schema.model_validate(parsed)
                except ValidationError as exc:
                    last_errors = _format_validation_errors(exc.errors())
                except (TypeError, ValueError) as exc:
                    last_errors = [f"$: {exc}"]
                else:
                    trace.repairs = repairs
                    trace.ok = True
                    return StructuredResult(
                        value=value,
                        raw_text=raw_text,
                        repairs=repairs,
                        usage=_merge_usage(usage_items),
                        json_mode_used=json_mode_used,
                    )

        trace.repairs = repairs
        trace.ok = False
        raise StructuredOutputError(schema_name, last_errors, raw_text)
    finally:
        try:
            await trace.flush()
        finally:
            _STRUCTURED_LOG_CONTEXT.reset(trace_token)
