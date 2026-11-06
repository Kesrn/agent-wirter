"""Deterministic provider for tests and the offline demonstration."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agents.llm_provider import LLMProvider
from agents.llm_types import LLMResult


class ScriptedProvider(LLMProvider):
    def __init__(self, agent_results: list[LLMResult], scene_texts: list[str]):
        self.agent_results = list(agent_results)
        self.scene_texts = list(scene_texts)
        self.agent_calls = 0
        self.scene_calls = 0

    async def complete(self, system_prompt: str, user_prompt: str, *, temperature: float = 0.7,
                        max_tokens: int = 4096, tools=None, response_format=None,
                        timeout: float | None = None) -> LLMResult:
        del system_prompt, user_prompt, temperature, max_tokens, response_format, timeout
        if tools is not None:
            if not self.agent_results:
                raise RuntimeError("script exhausted")
            self.agent_calls += 1
            return self.agent_results.pop(0)
        if not self.scene_texts:
            raise RuntimeError("script exhausted")
        self.scene_calls += 1
        return LLMResult(text=self.scene_texts.pop(0))

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7,
                       max_tokens: int = 4096) -> str:
        del system_prompt, user_prompt, temperature, max_tokens
        raise NotImplementedError("ScriptedProvider 用 complete() 提供脚本结果")

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7,
                              max_tokens: int = 4096, *, usage_sink=None) -> AsyncIterator[str]:
        del system_prompt, user_prompt, temperature, max_tokens, usage_sink
        raise NotImplementedError("ScriptedProvider 不支持流式生成")
