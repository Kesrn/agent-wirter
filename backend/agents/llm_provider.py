"""LLM Provider abstraction for OpenAI-compatible APIs and test mocks."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, AsyncIterator

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from agents.llm_types import LLMResult, TokenUsage, ToolCall, ToolSpec
from config.settings import settings
from observability.langfuse import current_langfuse_metadata, get_langfuse_async_openai_class
from utils.crypto import decrypt_api_key

logger = logging.getLogger(__name__)

# LoggedLLMProvider creates a fresh mapping in each call context. Providers update it
# with retry and finish metadata without sharing mutable state between concurrent calls.
_CALL_METADATA: ContextVar[dict[str, Any] | None] = ContextVar("llm_call_metadata", default=None)


def _set_call_metadata(**values: Any) -> None:
    metadata = _CALL_METADATA.get()
    if metadata is None:
        metadata = {}
        _CALL_METADATA.set(metadata)
    metadata.update(values)


class LLMProvider(ABC):
    """Provider interface. Existing callers continue to use string/stream methods."""

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        ...

    @abstractmethod
    async def generate_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        *,
        usage_sink: list[TokenUsage] | None = None,
    ) -> AsyncIterator[str]:
        ...

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolSpec] | None = None,
        response_format: dict | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        """Compatibility implementation for providers that only return text."""
        del tools, response_format, timeout
        started = time.perf_counter()
        text = await self.generate(system_prompt, user_prompt, temperature, max_tokens)
        _set_call_metadata(retries=0, finish_reason=None)
        return LLMResult(
            text=text,
            usage=None,
            tool_calls=[],
            finish_reason=None,
            model=getattr(self, "model", None),
            retries=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class LLMConfigError(ValueError):
    """Raised when a provider cannot be constructed from the available settings."""


def _status_code(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            return None
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, (asyncio.CancelledError, LLMConfigError)):
        return False
    if isinstance(
        error,
        (APITimeoutError, APIConnectionError, httpx.TimeoutException, httpx.NetworkError,
         TimeoutError, ConnectionError),
    ):
        return True
    status = _status_code(error)
    return status == 429 or (status is not None and 500 <= status <= 599)


def _retry_delay(error: BaseException, attempt: int) -> float:
    maximum = max(0.0, float(settings.LLM_RETRY_MAX_SECONDS))
    headers = getattr(getattr(error, "response", None), "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is not None:
        try:
            return min(maximum, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    base = max(0.0, float(settings.LLM_RETRY_BASE_SECONDS))
    capped = min(maximum, base * (2**attempt))
    return min(maximum, capped * random.uniform(0.5, 1.0))


def _retry_warning(operation: str, attempt: int, max_retries: int, error: BaseException) -> None:
    # Provider error bodies can contain user data; log only safe diagnostics.
    logger.warning(
        "LLM %s retry attempt=%s/%s error_type=%s status=%s",
        operation,
        attempt,
        max_retries,
        type(error).__name__,
        _status_code(error),
    )


def _read_usage(raw_usage: Any) -> TokenUsage | None:
    if raw_usage is None:
        return None
    try:
        input_tokens = int(raw_usage.prompt_tokens)
        output_tokens = int(raw_usage.completion_tokens)
        total = getattr(raw_usage, "total_tokens", None)
        total_tokens = int(total) if total is not None else input_tokens + output_tokens
    except (AttributeError, TypeError, ValueError):
        return None
    return TokenUsage(input_tokens, output_tokens, total_tokens, estimated=False)


def _read_tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw_call in raw_calls or []:
        function = getattr(raw_call, "function", None)
        raw_arguments = getattr(function, "arguments", None)
        arguments_error: str | None = None
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            arguments = {}
            arguments_error = f"{type(exc).__name__}: {exc}"
        calls.append(
            ToolCall(
                id=str(getattr(raw_call, "id", "") or ""),
                name=str(getattr(function, "name", "") or ""),
                arguments=arguments,
                arguments_error=arguments_error,
            )
        )
    return calls


def _stream_options_unsupported(error: BaseException) -> bool:
    if _status_code(error) != 400:
        return False
    message = str(error).lower()
    if "stream_options" not in message:
        return False
    return any(word in message for word in ("unsupported", "unknown", "unrecognized", "not support", "invalid"))


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible chat completions provider with safe retry behavior."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
        *,
        http_client: httpx.AsyncClient | None = None,
    ):
        if not api_key:
            raise LLMConfigError("当前模型配置缺少 API Key，请在设置里重新保存 API Key 后再试")
        langfuse_async_openai = get_langfuse_async_openai_class()
        self._use_langfuse_metadata = langfuse_async_openai is not None
        async_openai = langfuse_async_openai
        if async_openai is None:
            from openai import AsyncOpenAI
            async_openai = AsyncOpenAI
        timeout_seconds = float(settings.LLM_TIMEOUT_SECONDS)
        client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self.client = async_openai(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_seconds,
            # The provider owns retry policy and reporting; disable SDK retries so
            # attempts, jitter, cancellation, and Retry-After remain deterministic.
            max_retries=0,
            http_client=client,
        )
        self.model = model
        self._stream_options_supported: bool | None = None

    def _metadata(self, *, stream: bool) -> dict | None:
        if not self._use_langfuse_metadata:
            return None
        return current_langfuse_metadata({"llm_model": self.model, "stream": stream})

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolSpec] | None = None,
        response_format: dict | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        if response_format is not None:
            payload["response_format"] = response_format
        metadata = self._metadata(stream=False)
        if metadata:
            payload["metadata"] = metadata

        request_timeout = float(timeout) if timeout is not None else float(settings.LLM_TIMEOUT_SECONDS)
        max_retries = max(0, int(settings.LLM_MAX_RETRIES))
        retries = 0
        started = time.perf_counter()
        for attempt in range(max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    **payload,
                    timeout=request_timeout,
                )
                choice = response.choices[0] if response.choices else None
                message = choice.message if choice else None
                raw_text = getattr(message, "content", None) if message else None
                text = raw_text if isinstance(raw_text, str) else ""
                finish_reason = getattr(choice, "finish_reason", None) if choice else None
                _set_call_metadata(retries=retries, finish_reason=finish_reason)
                return LLMResult(
                    text=text,
                    usage=_read_usage(getattr(response, "usage", None)),
                    tool_calls=_read_tool_calls(getattr(message, "tool_calls", None) if message else None),
                    finish_reason=finish_reason,
                    model=getattr(response, "model", None) or self.model,
                    retries=retries,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except asyncio.CancelledError:
                _set_call_metadata(retries=retries, finish_reason=None)
                raise
            except Exception as exc:
                _set_call_metadata(retries=retries, finish_reason=None)
                if attempt >= max_retries or not _is_retryable(exc):
                    raise
                retries += 1
                _set_call_metadata(retries=retries)
                _retry_warning("complete", retries, max_retries, exc)
                await asyncio.sleep(_retry_delay(exc, attempt))
        raise AssertionError("unreachable retry loop")

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """Legacy string API backed by the structured completion method."""
        return (
            await self.complete(
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        ).text

    async def generate_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        *,
        usage_sink: list[TokenUsage] | None = None,
    ) -> AsyncIterator[str]:
        """Yield text chunks; retries are forbidden after the first visible character."""
        base_payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        metadata = self._metadata(stream=True)
        if metadata:
            base_payload["metadata"] = metadata
        request_timeout = float(settings.LLM_TIMEOUT_SECONDS)
        max_retries = max(0, int(settings.LLM_MAX_RETRIES))
        retries = 0
        transient_retries = 0
        yielded_character = False
        did_stream_options_fallback = False
        _set_call_metadata(retries=0, finish_reason=None)

        while True:
            payload = dict(base_payload)
            include_stream_options = self._stream_options_supported is not False
            if include_stream_options:
                payload["stream_options"] = {"include_usage": True}
            attempt_usage: TokenUsage | None = None
            attempt_finish_reason: str | None = None
            try:
                stream = await self.client.chat.completions.create(
                    **payload,
                    timeout=request_timeout,
                )
                if include_stream_options:
                    self._stream_options_supported = True
                async for chunk in stream:
                    chunk_usage = _read_usage(getattr(chunk, "usage", None))
                    if chunk_usage is not None:
                        attempt_usage = chunk_usage
                    for choice in getattr(chunk, "choices", None) or []:
                        if getattr(choice, "finish_reason", None):
                            attempt_finish_reason = choice.finish_reason
                        content = getattr(getattr(choice, "delta", None), "content", None)
                        if isinstance(content, str) and content:
                            yielded_character = True
                            yield content
                if usage_sink is not None and attempt_usage is not None:
                    usage_sink.append(attempt_usage)
                _set_call_metadata(retries=retries, finish_reason=attempt_finish_reason)
                return
            except asyncio.CancelledError:
                _set_call_metadata(retries=retries, finish_reason=attempt_finish_reason)
                raise
            except Exception as exc:
                if (
                    not yielded_character
                    and include_stream_options
                    and not did_stream_options_fallback
                    and _stream_options_unsupported(exc)
                ):
                    self._stream_options_supported = False
                    did_stream_options_fallback = True
                    retries += 1
                    _set_call_metadata(retries=retries, finish_reason=None)
                    continue
                if (
                    not yielded_character
                    and transient_retries < max_retries
                    and _is_retryable(exc)
                ):
                    delay = _retry_delay(exc, transient_retries)
                    transient_retries += 1
                    retries += 1
                    _set_call_metadata(retries=retries, finish_reason=None)
                    _retry_warning("stream", retries, max_retries, exc)
                    await asyncio.sleep(delay)
                    continue
                _set_call_metadata(retries=retries, finish_reason=attempt_finish_reason)
                raise


def get_llm_provider(config: dict | None = None) -> LLMProvider:
    """Construct the configured Provider while preserving the old selection API."""
    if config:
        provider = config.get("provider", settings.LLM_PROVIDER)
        if provider == "mock":
            if settings.ALLOW_MOCK_PROVIDER:
                from agents.mock_provider import MockProvider
                return MockProvider()
            logger.warning("检测到 mock provider 配置，但 mock 已被禁用，将抛出错误")
            raise LLMConfigError("Mock provider 已被禁用，请配置真实的 LLM provider (OpenAI/DeepSeek等)")
        api_key = (config.get("api_key") or "").strip()
        if not api_key and config.get("encrypted_api_key"):
            try:
                api_key = decrypt_api_key(str(config["encrypted_api_key"])).strip()
            except Exception as exc:
                raise LLMConfigError(
                    "当前模型配置的 API Key 无法解密，请在设置里重新保存后再试"
                ) from exc
        if not api_key:
            raise LLMConfigError("当前模型配置缺少 API Key，请在设置里重新保存 API Key 后再试")
        return OpenAIProvider(
            api_key=api_key,
            base_url=config.get("base_url"),
            model=config.get("model") or settings.LLM_MODEL,
        )

    if settings.LLM_PROVIDER == "mock":
        if settings.ALLOW_MOCK_PROVIDER:
            from agents.mock_provider import MockProvider
            return MockProvider()
        logger.warning("检测到 mock provider 配置，但 mock 已被禁用，将抛出错误")
        raise LLMConfigError("Mock provider 已被禁用，请在 .env 或前端设置中配置真实的 LLM provider")
    if not settings.LLM_API_KEY.strip():
        raise LLMConfigError("当前模型配置缺少 API Key，请在设置里重新保存 API Key 后再试")
    return OpenAIProvider(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL or None,
        model=settings.LLM_MODEL,
    )


def __getattr__(name: str):
    """Preserve ``from agents.llm_provider import MockProvider`` lazily.

    A lazy alias avoids a module-import cycle when ``agents.mock_provider`` itself is
    imported first.
    """
    if name == "MockProvider":
        from agents.mock_provider import MockProvider
        return MockProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
