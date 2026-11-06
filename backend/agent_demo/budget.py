"""Budget controls and stop reasons for the standalone agent demo."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class Budget:
    max_steps: int = 8
    max_total_tokens: int = 20_000
    max_wall_seconds: float = 120.0
    max_tool_errors: int = 3
    max_repeat_calls: int = 2
    max_observation_chars: int = 1_500


class StopReason(str, Enum):
    FINAL_ANSWER = "final_answer"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"
    TIME_BUDGET = "time_budget"
    TOO_MANY_TOOL_ERRORS = "too_many_tool_errors"
    REPEATED_CALL = "repeated_call"
    LLM_ERROR = "llm_error"
