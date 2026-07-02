"""FactExtractionAgent — 从章节内容抽取写作记忆（人物/规则/事件/伏笔）。

章节 approve 后后台调用，抽取结果写入 writing_memory_staging（不直接进正式设定库）。
parse 失败容错为空列表，不影响 approve 流程。
镜像 guardrail.py 的 parse 模式 + extraction_schema.py 的 prompt 风格。
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VALID_MEMORY_TYPES = {"CHARACTER", "WORLD_RULE", "PLOT_FACT", "EVENT", "FORESHADOWING"}

FACT_EXTRACTION_PROMPT = """你是一个小说写作记忆抽取引擎。
你的任务是从给定章节正文中抽取对后续写作有价值的新设定信息，供作者确认后加入正式设定库。

抽取维度：
1. CHARACTER — 新出场或信息更新的角色（名字、身份、性格、阵营等）
2. WORLD_RULE — 世界观规则/设定（魔法体系、地理、历史、社会规则等）
3. PLOT_FACT — 剧情事实（重要事件、因果关系、时间线节点）
4. EVENT — 角色事件（角色在章节中的行动、状态变化、情感转折）
5. FORESHADOWING — 伏笔/暗线线索

重要规则：
1. 只根据当前章节正文抽取，不允许使用外部知识补充。
2. 不允许编造。
3. 每条记忆必须提供 evidence（来自当前章节的原文短句或准确摘要）。
4. 只抽取新出现或有重大更新的信息，不要重复已有设定。
5. 普通气氛描写、重复动作、无后续影响的闲聊不要抽。
6. 输出必须是合法 JSON，不要输出 Markdown，不要解释。

输出 JSON Schema（严格按此结构，数组无内容时返回 []）：
{
  "memories": [
    {
      "memory_type": "CHARACTER|WORLD_RULE|PLOT_FACT|EVENT|FORESHADOWING",
      "title": "简短标题（如角色名、规则名、事件名）",
      "payload": {"key": "value"},
      "evidence": "原文证据"
    }
  ]
}

payload 字段说明：
- CHARACTER: {"name": "角色名", "role_type": "protagonist|antagonist|supporting", "profile": "简介", "faction": "阵营"}
- WORLD_RULE: {"category": "类别", "content": "规则内容"}
- PLOT_FACT: {"description": "事实描述", "chapter": "章节序号"}
- EVENT: {"character_name": "角色名", "event_summary": "事件摘要", "state_change": "状态变化"}
- FORESHADOWING: {"description": "伏笔描述", "chapter_nums": [章节序号]}

单章最多输出 15 条记忆，只保留明确且重要的信息。"""


def _coerce_memory_type(value: Any) -> str:
    if isinstance(value, str) and value.upper() in VALID_MEMORY_TYPES:
        return value.upper()
    return "PLOT_FACT"


def _coerce_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def parse_facts(raw: str) -> list[dict[str, Any]]:
    """容错解析 FactExtractionAgent 的 LLM 输出为 memories 列表。

    解析策略（与 guardrail.parse_guardrail_result 一致）：
    1. regex 提取 {...}
    2. json.loads
    3. 失败时返回空列表

    Args:
        raw: LLM 返回的原始文本

    Returns:
        memories 列表，每条含 memory_type/title/payload/evidence
    """
    raw = raw or ""
    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                raw_memories = parsed.get("memories", [])
                if not isinstance(raw_memories, list):
                    return []
                facts: list[dict[str, Any]] = []
                for item in raw_memories:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title", "")
                    if not isinstance(title, str) or not title.strip():
                        continue
                    facts.append({
                        "memory_type": _coerce_memory_type(item.get("memory_type")),
                        "title": title.strip(),
                        "payload": _coerce_payload(item.get("payload")),
                        "evidence": item.get("evidence") if isinstance(item.get("evidence"), str) else None,
                    })
                return facts
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("fact_extraction: LLM output JSON parse failed: %s", e)

    return []


async def extract_facts(llm, content: str, context: str = "") -> list[dict[str, Any]]:
    """调用 LLM 抽取写作记忆。

    Args:
        llm: LLMProvider 实例（已配置用户模型）
        content: 章节正文
        context: 章节上下文（设定/前文摘要，可选）

    Returns:
        memories 列表（parse 失败时为空列表）
    """
    user_prompt = f"## 上下文/设定\n{context or '(无)'}\n\n## 章节正文\n{content}\n\n请抽取写作记忆："
    raw = await llm.generate(FACT_EXTRACTION_PROMPT, user_prompt, temperature=0.2, max_tokens=4096)
    return parse_facts(raw)
