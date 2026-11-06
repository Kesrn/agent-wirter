"""Story Recorder — 从最终正文提取 Story Record 结构化 JSON。

章节 approve 后后台调用，提取已发生事件、状态变化、关系变化、伏笔等。
输出 Story Record JSON，交给 memory-curator 转为 writing_memory_staging。
不直接写正式设定表。失败只记录 ai_run_step FAILED，不影响 approve。

与旧 FactExtractionAgent 的区别：
- Story Recorder 输出更结构化的 Story Record（events/character_state_changes/...）
- memory-curator 负责把 Story Record 转为 staging 行（职责分离）
- 复用现有 writing_memory_staging 表和 confirm 逻辑
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


STORY_RECORDER_PROMPT = """你是一个小说剧情记录员。
你的任务是从给定章节正文（已通过最终审核）中提取本章实际发生的剧情信息。

提取维度：
1. events — 本章发生的关键事件
2. character_state_changes — 角色状态变化（能力、身份、心理）
3. relationship_changes — 角色间关系变化
4. ability_changes — 能力/技能变化
5. foreshadowing_new — 本章新埋的伏笔
6. foreshadowing_resolved — 本章回收的伏笔
7. timeline — 时间线节点
8. knowledge_state_changes — 信息揭示/知识状态变化

重要规则：
1. 只记录正文明确发生的信息，不推测、不脑补。
2. 只根据当前章节正文提取，不允许使用外部知识补充。
3. 每条信息必须能从正文中找到依据。
4. 不记录气氛描写、重复动作、无后续影响的闲聊。
5. 输出必须是合法 JSON，不要输出 Markdown，不要解释。

输出 JSON Schema（严格按此结构，无内容时返回空数组）：
{
  "summary": "一句话摘要",
  "events": [
    {
      "title": "事件标题",
      "description": "事件描述",
      "character_names": ["涉及角色名"],
      "evidence": "原文证据短句"
    }
  ],
  "character_state_changes": [
    {
      "character_name": "角色名",
      "change": "状态变化描述",
      "evidence": "原文证据"
    }
  ],
  "relationship_changes": [
    {
      "characters": ["角色A", "角色B"],
      "change": "关系变化描述",
      "evidence": "原文证据"
    }
  ],
  "ability_changes": [
    {
      "character_name": "角色名",
      "ability": "能力名称",
      "change": "获得/失去/提升",
      "evidence": "原文证据"
    }
  ],
  "foreshadowing_new": [
    {
      "title": "伏笔标题",
      "description": "伏笔描述",
      "evidence": "原文证据"
    }
  ],
  "foreshadowing_resolved": [
    {
      "title": "伏笔标题",
      "description": "回收描述",
      "evidence": "原文证据"
    }
  ],
  "timeline": {
    "time_point": "时间点描述",
    "events": ["相关事件"]
  },
  "knowledge_state_changes": [
    {
      "description": "信息揭示描述",
      "evidence": "原文证据"
    }
  ]
}"""


def _coerce_list(value: Any) -> list[dict[str, Any]]:
    """将任意值强制为 dict 列表，过滤非 dict 元素。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value) if value else ""


def parse_story_record(raw: str) -> dict[str, Any]:
    """容错解析 LLM 输出的 Story Record JSON。

    解析策略（与 parse_guardrail_result / parse_clarification_result 一致）：
    1. regex 提取 {...}
    2. json.loads
    3. 失败时返回空 Story Record

    Args:
        raw: LLM 返回的原始文本

    Returns:
        Story Record dict（永远非 None，永远有所有字段）
    """
    raw = raw or ""
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return {
                    "summary": _coerce_str(parsed.get("summary")),
                    "events": _coerce_list(parsed.get("events")),
                    "character_state_changes": _coerce_list(parsed.get("character_state_changes")),
                    "relationship_changes": _coerce_list(parsed.get("relationship_changes")),
                    "ability_changes": _coerce_list(parsed.get("ability_changes")),
                    "foreshadowing_new": _coerce_list(parsed.get("foreshadowing_new")),
                    "foreshadowing_resolved": _coerce_list(parsed.get("foreshadowing_resolved")),
                    "timeline": parsed.get("timeline") if isinstance(parsed.get("timeline"), dict) else {},
                    "knowledge_state_changes": _coerce_list(parsed.get("knowledge_state_changes")),
                    "parse_error": False,
                }
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("story_recorder: LLM output JSON parse failed: %s", e)

    return {
        "summary": "",
        "events": [],
        "character_state_changes": [],
        "relationship_changes": [],
        "ability_changes": [],
        "foreshadowing_new": [],
        "foreshadowing_resolved": [],
        "timeline": {},
        "knowledge_state_changes": [],
        "parse_error": True,
        "raw": raw.strip(),
    }


async def run_story_recorder(
    *,
    llm_config: dict | None,
    content: str,
    context: str = "",
    harness_run_id: str = "",
    harness_step_id: str | None = None,
) -> dict[str, Any]:
    """调 LLM 运行剧情记录员，返回 Story Record dict。

    使用 LoggedLLMProvider 记录 run_id / step_id / agent_name=story_recorder。
    失败时返回空 Story Record（parse_error=True），不阻断流程。

    Args:
        llm_config: 用户 LLM 配置
        content: 章节最终正文
        context: 章节上下文（设定/前文摘要，可选）
        harness_run_id: AiRun.id（用于 LLM call log）
        harness_step_id: 当前 AiRunStep.id

    Returns:
        Story Record dict
    """
    from agents.llm_provider import get_llm_provider
    from agents.workflow import _maybe_wrap_llm
    from skills.runner import build_expert_skill_pack, build_expert_system_prompt

    # 构建 prompt
    user_prompt = (
        f"## 上下文/设定\n{context or '(无)'}\n\n"
        f"## 章节正文（已通过最终审核）\n{content}\n\n"
        "请提取 Story Record JSON："
    )

    # 构建 system prompt（含 SKILL.md 内容）
    pack = build_expert_skill_pack(
        "editor",  # story-recorder 用 editor role_type 加载 skill
        skill_dir="story-recorder",
        context=context,
        mode="generate",
    )
    system_prompt = build_expert_system_prompt("editor", STORY_RECORDER_PROMPT, pack)

    # 获取 LLM 并包装日志
    llm = get_llm_provider(llm_config)
    state_for_wrap = {
        "harness_run_id": harness_run_id,
        "harness_step_id": harness_step_id,
        "llm_config": llm_config,
        "context": context,
    }
    llm = _maybe_wrap_llm(llm, state_for_wrap, agent_name="story_recorder", include_context=True)

    try:
        raw = await llm.generate(system_prompt, user_prompt, temperature=0.2, max_tokens=4096)
        result = parse_story_record(raw)
    except Exception as e:
        logger.warning("run_story_recorder LLM 调用失败，fallback 空 Story Record: %s", e)
        result = {
            "summary": "",
            "events": [],
            "character_state_changes": [],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [],
            "timeline": {},
            "knowledge_state_changes": [],
            "parse_error": True,
            "raw": str(e),
        }

    return result
