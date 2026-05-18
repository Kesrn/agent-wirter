"""LLM Provider 抽象层

支持 mock / openai / deepseek / siliconflow / zhipu / moonshot / qwen / yi / minimax / custom。
所有非 mock 厂商均走 OpenAI 兼容 API。
默认 mock，无需 API key 即可运行。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        ...

    @abstractmethod
    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        ...


class MockProvider(LLMProvider):
    """Mock provider for testing — echoes context-aware responses"""

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        await asyncio.sleep(0.3)
        prompt_lower = (system_prompt + user_prompt).lower()

        # Extract key context info for more realistic mock output
        context_chars = []
        if "角色资料" in user_prompt:
            import re
            char_matches = re.findall(r"- (.+?)\(", user_prompt)
            context_chars.extend(char_matches)
        if "世界观设定" in user_prompt:
            import re
            we_matches = re.findall(r"\[.+?\] (.+?):", user_prompt)
            context_chars.extend(we_matches)

        char_note = f"（涉及角色：{'、'.join(context_chars[:3])}）" if context_chars else ""

        if ("给出" in prompt_lower or "输出" in prompt_lower or "generate" in prompt_lower) and ("方向" in prompt_lower or "建议" in prompt_lower or "direction" in prompt_lower or "suggestion" in prompt_lower):
            # Direction/suggestion generation requests (step 1 only)
            if "润色" in prompt_lower or "enhance" in prompt_lower:
                return '["加强氛围描写，让场景更具沉浸感", "精简冗余段落，提升叙事节奏", "深化角色内心，增加情感层次"]'
            elif "续写" in prompt_lower or "转折" in prompt_lower or "continue" in prompt_lower or "turn" in prompt_lower:
                return '["主角发现隐藏的秘密，揭开真相", "新角色登场，打破现有格局", "意外事件打乱计划，迫使主角做出艰难选择", "回忆闪回，揭示过去的关键经历", "盟友背叛，信任崩塌引发连锁反应"]'
            else:
                return '["方向一", "方向二", "方向三"]'
        elif "审校" in prompt_lower or "critic" in prompt_lower:
            return f"【审校意见】\n1. 情节推进自然{char_note}\n2. 人物对话可更生动\n3. 建议加强场景描写"
        elif "一致性" in prompt_lower or "consistency" in prompt_lower:
            return f"【一致性检查】\n文本与已知设定基本一致{char_note}，未发现明显矛盾。"
        elif "编辑" in prompt_lower or "润色" in prompt_lower or "editor" in prompt_lower:
            return f"（润色后文本）\n\n在月光下，故事缓缓展开{char_note}。远方的山峦如墨，近处的溪流似银，一切都在这静谧的夜里酝酿着变化。\n\n风起时，命运的齿轮开始转动。"
        else:
            # Writer: include context references
            outline_note = ""
            if "大纲" in user_prompt:
                import re
                ch_matches = re.findall(r"### 第(\d+)章 (.+)", user_prompt)
                if ch_matches:
                    outline_note = f"，基于第{ch_matches[0][0]}章「{ch_matches[0][1]}」大纲"
            return f"（生成内容）{outline_note}{char_note}\n\n夜色渐深，城市的灯火在远处明灭不定。她站在窗前，手指轻轻拂过冰凉的玻璃，心中涌起一股难以名状的情绪。\n\n\"你来了。\"她的声音很轻，像是怕惊扰了什么。\n\n身后的脚步声停顿了一瞬，然后继续靠近。\"我别无选择。\""

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        result = await self.generate(system_prompt, user_prompt, temperature, max_tokens)
        for i in range(0, len(result), 5):
            yield result[i:i+5]
            await asyncio.sleep(0.05)


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容 API Provider（DeepSeek / SiliconFlow 等也走此路径）"""

    def __init__(self, api_key: str, base_url: str | None = None, model: str = "gpt-4o-mini"):
        if not api_key:
            raise ValueError("LLM_API_KEY 未设置，无法使用 OpenAI Provider")
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


def get_llm_provider(config: dict | None = None) -> LLMProvider:
    """根据配置返回 LLM Provider 实例

    Args:
        config: 用户 LLM 配置字典 {"provider", "api_key", "base_url", "model"}。
            若为 None，走 settings 默认值（向后兼容）。
    """
    if config:
        provider = config.get("provider", settings.LLM_PROVIDER)
        if provider == "mock":
            return MockProvider()
        else:
            return OpenAIProvider(
                api_key=config.get("api_key", ""),
                base_url=config.get("base_url"),
                model=config.get("model") or settings.LLM_MODEL,
            )

    # 无 config → 走 settings 默认值
    if settings.LLM_PROVIDER == "mock":
        return MockProvider()
    else:
        return OpenAIProvider(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL or None,
            model=settings.LLM_MODEL,
        )
