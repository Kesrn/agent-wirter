"""Offline, standalone minimal agent demonstration."""

from .budget import Budget, StopReason
from .loop import AgentRunResult, run_agent

__all__ = ["AgentRunResult", "Budget", "StopReason", "run_agent"]
