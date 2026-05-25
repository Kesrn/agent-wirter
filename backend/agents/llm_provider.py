"""LLM Provider 抽象层

支持 mock / openai / deepseek / siliconflow / zhipu / moonshot / qwen / yi / minimax / custom。
所有非 mock 厂商均走 OpenAI 兼容 API。
默认 mock，无需 API key 即可运行。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator
import re

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
            char_matches = re.findall(r"- (.+?)\(", user_prompt)
            context_chars.extend(char_matches)
        if "世界观设定" in user_prompt:
            we_matches = re.findall(r"\[.+?\] (.+?):", user_prompt)
            context_chars.extend(we_matches)

        char_note = f"（涉及角色：{'、'.join(context_chars[:3])}）" if context_chars else ""
        is_article_request = any(
            marker in prompt_lower
            for marker in ("文章/文案", "当前稿件", "内容 brief", "目标受众", "发布平台", "文案")
        )

        is_direction_request = (
            ("给出3个" in prompt_lower or "给出5个" in prompt_lower or "json数组" in prompt_lower or "json字符串数组" in prompt_lower)
            and ("方向" in prompt_lower or "建议" in prompt_lower or "direction" in prompt_lower or "suggestion" in prompt_lower)
        )
        if is_direction_request:
            # Direction/suggestion generation requests (step 1 only)
            if is_article_request:
                return '["突出目标受众痛点，强化开头吸引力", "重组段落结构，让卖点表达更清晰", "增加行动引导，提升转化意图"]'
            elif "修改方向" in prompt_lower or "候选章节" in prompt_lower or "候选稿" in prompt_lower:
                return '["压缩重复段落，增强章节节奏", "补足人物动机，让情绪转折更可信", "强化场景细节，并保持原结尾不变"]'
            elif "润色" in prompt_lower or "enhance" in prompt_lower:
                return '["加强氛围描写，让场景更具沉浸感", "精简冗余段落，提升叙事节奏", "深化角色内心，增加情感层次"]'
            elif "续写" in prompt_lower or "转折" in prompt_lower or "continue" in prompt_lower or "turn" in prompt_lower:
                return '["主角发现隐藏的秘密，揭开真相", "新角色登场，打破现有格局", "意外事件打乱计划，迫使主角做出艰难选择", "回忆闪回，揭示过去的关键经历", "盟友背叛，信任崩塌引发连锁反应"]'
            else:
                return '["方向一", "方向二", "方向三"]'

        original_chapter = self._extract_marked_text(user_prompt, ("## 原文章节", "## 待润色文本"))
        if original_chapter and ("编辑" in prompt_lower or "润色" in prompt_lower or "editor" in prompt_lower):
            return self._mock_polish(original_chapter)

        original_article = self._extract_marked_text(user_prompt, ("## 原文案/文章", "## 当前稿件"))
        if is_article_request and original_article and ("改写" in prompt_lower or "优化" in prompt_lower or "编辑" in prompt_lower):
            return self._mock_article(original_article, rewrite=True)

        revision_candidate = self._extract_marked_text(user_prompt, ("## 当前候选稿",))
        if revision_candidate and ("修改方向" in user_prompt or "完整修改" in user_prompt):
            return self._mock_polish(revision_candidate)

        elif "审校" in prompt_lower or "critic" in prompt_lower:
            return f"【审校意见】\n1. 情节推进自然{char_note}\n2. 人物对话可更生动\n3. 建议加强场景描写"
        elif "一致性" in prompt_lower or "consistency" in prompt_lower:
            return f"【一致性检查】\n文本与已知设定基本一致{char_note}，未发现明显矛盾。"
        elif "编辑" in prompt_lower or "润色" in prompt_lower or "editor" in prompt_lower:
            return f"在月光下，原有段落被打磨得更清晰、更有层次{char_note}。"
        elif is_article_request:
            return self._mock_article(user_prompt)
        else:
            # Writer: include context references
            outline_note = ""
            if "大纲" in user_prompt:
                ch_matches = re.findall(r"### 第(\d+)章 (.+)", user_prompt)
                if ch_matches:
                    outline_note = f"，基于第{ch_matches[0][0]}章「{ch_matches[0][1]}」大纲"
            return f"（生成内容）{outline_note}{char_note}\n\n夜色渐深，城市的灯火在远处明灭不定。她站在窗前，手指轻轻拂过冰凉的玻璃，心中涌起一股难以名状的情绪。\n\n\"你来了。\"她的声音很轻，像是怕惊扰了什么。\n\n身后的脚步声停顿了一瞬，然后继续靠近。\"我别无选择。\""

    def _extract_original_chapter(self, user_prompt: str) -> str:
        return self._extract_marked_text(user_prompt, ("## 原文章节", "## 待润色文本"))

    def _extract_marked_text(self, user_prompt: str, markers: tuple[str, ...]) -> str:
        for marker in markers:
            if marker not in user_prompt:
                continue
            text = user_prompt.split(marker, 1)[1].strip()
            if "\n\n请" in text:
                text = text.split("\n\n请", 1)[0]
            if "\n\n硬性要求" in text:
                text = text.split("\n\n硬性要求", 1)[0]
            if text.startswith("（") and "）\n" in text:
                text = text.split("）\n", 1)[1]
            return text.strip()
        return ""

    def _mock_polish(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        replacements = {
            "原始": "经过打磨的",
            "文本": "文字",
            "内容": "段落",
        }
        polished = stripped
        for old, new in replacements.items():
            polished = polished.replace(old, new)
        if polished == stripped:
            polished = f"{stripped}\n\n这段文字的节奏被收束得更清晰，语气也更稳定。"
        return polished

    def _mock_article(self, text: str, rewrite: bool = False) -> str:
        source = text.strip()
        if rewrite and source and not source.startswith("（当前稿件为空"):
            return (
                f"{source}\n\n"
                "这版稿件进一步压缩了重复表达，强化了核心卖点，并在结尾补足了清晰的行动引导。"
            )
        return (
            "如何把一个好想法变成可执行的内容\n\n"
            "很多内容效果不稳定，并不是因为观点不够好，而是因为表达没有对准受众。"
            "一篇有效的文章或文案，需要先说明读者为什么应该关心，再给出清晰的信息结构，最后提供明确的下一步行动。\n\n"
            "建议先用一句话点出核心问题，再用三到四个小段落展开价值、证据和使用场景。"
            "表达上保持具体，少用空泛形容词，多给读者能立刻理解的例子。\n\n"
            "如果目标是转化，结尾不要停在总结，而要给出明确行动：了解更多、预约咨询、下载资料或开始试用。"
        )

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
