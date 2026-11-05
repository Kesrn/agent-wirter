"""Pure result and tool data types shared by LLM providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    arguments_error: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    usage: TokenUsage | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None
    retries: int = 0
    latency_ms: int = 0
