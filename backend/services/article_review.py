"""文章/文案审校流水线 — content_writer 后轻量审校链

审校节点：
1. structure_review   — 结构/标题检查
2. audience_review    — 受众匹配检查
3. platform_review    — 平台适配/CTA 检查
4. risk_review        — 风险/事实性提醒

每个节点返回结构化 JSON 结果，由 routes.py 转为 SSE 事件。
不落库，只返回审校意见供前端展示和用户参考。
"""

import json
import logging
import re

from agents.llm_provider import LLMProvider
from schemas.api import GenerateRequest

logger = logging.getLogger(__name__)


# --- 审校 system_prompts（严格禁止小说口吻） ---
_STRUCTURE_SYSTEM = (
    "你是一位专业文章结构编辑。检查文章/文案的结构和标题："
    "标题是否吸引目标受众、开头是否切入痛点、段落逻辑是否连贯、收束是否有力。"
    "输出JSON：{\"title_score\": 1-5, \"structure_score\": 1-5, \"issues\": [...], \"suggestions\": [...]}"
    "禁止使用小说章节/角色/剧情/世界观等术语。"
)

_AUDIENCE_SYSTEM = (
    "你是一位受众研究专家。检查文章/文案是否对准目标受众："
    "用语是否匹配受众认知水平、是否回应受众痛点、是否能引发受众共鸣或行动。"
    "输出JSON：{\"match_score\": 1-5, \"audience_issues\": [...], \"audience_suggestions\": [...]}"
    "禁止使用小说章节/角色/剧情/世界观等术语。"
)

_PLATFORM_SYSTEM = (
    "你是一位平台内容运营专家。检查文章/文案的平台适配度："
    "格式是否符合目标平台风格、CTA/行动引导是否清晰、是否利于转化或传播。"
    "输出JSON：{\"platform_score\": 1-5, \"cta_found\": bool, \"platform_issues\": [...], \"platform_suggestions\": [...]}"
    "禁止使用小说章节/角色/剧情/世界观等术语。"
)

_RISK_SYSTEM = (
    "你是一位内容合规和事实核查顾问。检查文章/文案的风险点："
    "是否包含不实断言、是否有敏感表述、是否缺少必要免责声明、是否有版权风险暗示。"
    "输出JSON：{\"risk_level\": \"low\"|\"medium\"|\"high\", \"fact_issues\": [...], \"compliance_warnings\": [...]}"
    "禁止使用小说章节/角色/剧情/世界观等术语。"
)


def _review_user_prompt(brief: str, content: str) -> str:
    """构建审校的通用用户 prompt"""
    return f"## 内容 brief\n{brief}\n\n## 待审校正文\n{content}\n\n请审校："


async def run_structure_review(
    provider: LLMProvider,
    req: GenerateRequest,
    content: str,
) -> dict:
    """结构/标题检查"""
    brief = _article_brief_for_review(req)
    result = await provider.generate(
        _STRUCTURE_SYSTEM,
        _review_user_prompt(brief, content),
        temperature=0.2,
        max_tokens=1024,
    )
    return _parse_review_json(result, "structure_review")


async def run_audience_review(
    provider: LLMProvider,
    req: GenerateRequest,
    content: str,
) -> dict:
    """受众匹配检查"""
    brief = _article_brief_for_review(req)
    result = await provider.generate(
        _AUDIENCE_SYSTEM,
        _review_user_prompt(brief, content),
        temperature=0.2,
        max_tokens=1024,
    )
    return _parse_review_json(result, "audience_review")


async def run_platform_review(
    provider: LLMProvider,
    req: GenerateRequest,
    content: str,
) -> dict:
    """平台适配/CTA 检查"""
    brief = _article_brief_for_review(req)
    result = await provider.generate(
        _PLATFORM_SYSTEM,
        _review_user_prompt(brief, content),
        temperature=0.2,
        max_tokens=1024,
    )
    return _parse_review_json(result, "platform_review")


async def run_risk_review(
    provider: LLMProvider,
    req: GenerateRequest,
    content: str,
) -> dict:
    """风险/事实性提醒"""
    brief = _article_brief_for_review(req)
    result = await provider.generate(
        _RISK_SYSTEM,
        _review_user_prompt(brief, content),
        temperature=0.1,
        max_tokens=1024,
    )
    return _parse_review_json(result, "risk_review")


def _article_brief_for_review(req: GenerateRequest) -> str:
    """为审校节点构建精简 brief"""
    items = []
    if req.content_type:
        items.append(f"类型：{req.content_type}")
    if req.platform:
        items.append(f"平台：{req.platform}")
    if req.audience:
        items.append(f"受众：{req.audience}")
    if req.content_goal:
        items.append(f"目标：{req.content_goal}")
    if req.tone:
        items.append(f"语气：{req.tone}")
    return "\n".join(items) if items else "通用文章/文案"


def _parse_review_json(raw: str, review_type: str) -> dict:
    """容错解析审校 JSON 输出"""
    import json
    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"{review_type}: LLM output not valid JSON, wrapping as raw")
    return {"raw": raw.strip(), "parse_error": True}