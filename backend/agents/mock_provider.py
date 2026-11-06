"""Mock LLM provider for deterministic tests and demos."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from typing import AsyncIterator

from agents.llm_provider import LLMProvider, _set_call_metadata
from agents.llm_types import LLMResult, TokenUsage, ToolSpec


class MockProvider(LLMProvider):
    """测试/演示用 Provider。

    它不调用真实模型，而是根据 prompt 里的关键词返回稳定文本或 JSON。
    这样测试可以覆盖章节生成、结构化抽取、知识库问答、评测等链路，
    不依赖外部 API、网络和真实 token 消耗。
    """

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        await asyncio.sleep(0.3)
        prompt_lower = (system_prompt + user_prompt).lower()

        # 从上下文里提取角色/世界观关键词，让 mock 结果能体现“上下文确实被注入了”。
        # 这对前端联调和测试断言很有用：如果缺少上下文，mock 文本里的 char_note 会消失。
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
        # 下面的分支按业务类型模拟不同 LLM 输出。真实模型可能返回自然语言或 JSON，
        # 所以服务层仍然要做容错解析，不能因为 mock 稳定就假设生产也稳定。
        is_structure_extraction_request = (
            "结构提炼" in prompt_lower
            or ("只输出严格 json" in prompt_lower and "character_relations" in prompt_lower)
            or ("world_entries" in prompt_lower and "hidden_threads" in prompt_lower)
        )
        # 小说知识库结构化抽取（章节级人物/能力/事件/世界规则）
        is_novel_extraction_request = (
            "小说知识库结构化抽取引擎" in prompt_lower
            and "章节正文" in prompt_lower
        )
        is_query_planner_request = (
            "query planner" in prompt_lower
            and "rewritten_question" in prompt_lower
            and "sub_queries" in prompt_lower
        )
        is_evaluation_judge_request = (
            "评测裁判" in prompt_lower
            or ("overall_score" in prompt_lower and "\"scores\"" in prompt_lower and "候选输出" in prompt_lower)
        )
        if is_evaluation_judge_request:
            return json.dumps(
                {
                    "overall_score": 4.0,
                    "scores": {
                        "requirement_following": 4,
                        "context_consistency": 4,
                        "plot_progress": 4,
                        "prose_quality": 4,
                        "structure_quality": 4,
                        "audience_match": 4,
                        "risk_control": 4,
                    },
                    "passed": True,
                    "feedback": "Mock 评测：候选输出整体满足样本要求，可作为回归测试通过。",
                },
                ensure_ascii=False,
            )
        if is_query_planner_request:
            return self._mock_query_plan(user_prompt)
        if is_structure_extraction_request:
            return self._mock_structure_extraction(user_prompt)
        if is_novel_extraction_request:
            return self._mock_novel_extraction(user_prompt)
        is_knowledge_qa_request = (
            "小说资料库的 ai 助手" in prompt_lower
            or ("输出格式" in prompt_lower and "\"citations\"" in prompt_lower and "用户问题" in prompt_lower)
            or ("## 用户问题" in user_prompt and (
                "人物直接证据" in user_prompt
                or "普通资料" in user_prompt
                or "技能表 / 能力设定" in user_prompt
            ))
        )
        if is_knowledge_qa_request:
            return self._mock_knowledge_qa(user_prompt)

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

        # ── Expert System v2 节点响应 ──
        # ChapterTaskCard (architect)
        # 只在 architect 明确要求输出任务卡 JSON 时返回结构化数据。Writer 的
        # skill/context 也可能提到 ChapterTaskCard，不能因此把任务卡 JSON 当正文。
        if "请输出 chaptertaskcard json" in user_prompt.lower():
            return json.dumps({
                "chapter_number": 1,
                "chapter_title": "mock章节标题",
                "core_task": "mock 核心任务：推进主线剧情",
                "opening_anchor": "承接前文结尾",
                "scenes": [
                    {"title": "场景一", "location": "mock地点", "characters": [],
                     "scene_goal": "mock场景目标", "conflict": "mock冲突",
                     "must_include": [], "must_not_include": [], "word_budget": 1000}
                ],
                "character_goals": [],
                "information_rules": {"may_reveal": [], "hint_only": [], "forbidden": []},
                "tension_design": [],
                "word_budget": 2000,
                "forbidden": [],
            }, ensure_ascii=False)

        # StructuralCritique (critic)
        if "请诊断并输出 structuralcritique json" in user_prompt.lower():
            return json.dumps({
                "summary": "mock 审稿意见：整体结构合理",
                "p0": [],
                "p1": ["建议加强场景描写", "人物动机可更明确"],
                "p2": ["部分对话可精简"],
                "must_keep": ["开篇氛围"],
                "edit_instructions": {
                    "delete": [], "merge": [], "move_forward": [],
                    "move_later": [], "rewrite": ["开篇段落"], "keep": ["结尾悬念"],
                },
            }, ensure_ascii=False)

        # ClarificationResult (clarification-planner)
        if "clarificationresult" in prompt_lower:
            return json.dumps({
                "needs_clarification": True,
                "confidence": 0.6,
                "missing_fields": ["chapter_goal", "pov"],
                "questions": [
                    {
                        "id": "chapter_goal",
                        "type": "single_choice",
                        "question": "这一章最重要的推进目标是什么？",
                        "options": [
                            {"value": "adapt", "label": "适应新环境", "description": "重点写主角进入新环境"},
                            {"value": "conflict", "label": "触发冲突", "description": "重点写主角与同学冲突"},
                        ],
                        "required": True,
                        "reason": "章节目标决定 writer 的事件选择",
                    },
                ],
                "assumptions_if_skipped": ["默认采用第三人称有限视角"],
                "clarification_summary": "",
            }, ensure_ascii=False)

        # Story Record (story-recorder)
        if "story record" in prompt_lower or "剧情记录员" in prompt_lower:
            return json.dumps({
                "summary": "mock 剧情摘要：主角在本章经历了关键事件",
                "events": [
                    {
                        "title": "主角觉醒魔法",
                        "description": "主角在危机中觉醒了雷系魔法能力",
                        "character_names": ["程璇"],
                        "evidence": "程璇感到体内涌起一股雷电之力",
                    },
                ],
                "character_state_changes": [
                    {
                        "character_name": "程璇",
                        "change": "从普通人变为觉醒者",
                        "evidence": "程璇感到体内涌起一股雷电之力",
                    },
                ],
                "relationship_changes": [],
                "ability_changes": [
                    {
                        "character_name": "程璇",
                        "ability": "雷系魔法",
                        "change": "获得",
                        "evidence": "程璇感到体内涌起一股雷电之力",
                    },
                ],
                "foreshadowing_new": [
                    {
                        "title": "神秘组织观察",
                        "description": "有人暗中观察主角的觉醒",
                        "evidence": "暗处有人在记录着什么",
                    },
                ],
                "foreshadowing_resolved": [],
                "timeline": {"time_point": "觉醒日", "events": ["主角觉醒魔法"]},
                "knowledge_state_changes": [],
            }, ensure_ascii=False)

        # EditedDraft (narrative-editor)：检测审稿指令 + 修订稿
        if "审稿指令" in user_prompt and "修订稿" in prompt_lower:
            draft_text = self._extract_marked_text(user_prompt, ("## 待修改正文",))
            if draft_text:
                return self._mock_novel_prose(user_prompt, edited=True)
            return self._mock_novel_prose(user_prompt, edited=True)

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
            return self._mock_novel_prose(user_prompt, char_note=char_note)

    def _mock_novel_prose(self, user_prompt: str, *, edited: bool = False, char_note: str = "") -> str:
        """生成能通过确定性章节质量门的稳定 Mock 正文。"""
        target_match = re.search(r"目标字数[：:\s]*约?\s*(\d+)", user_prompt)
        target_chars = int(target_match.group(1)) if target_match else 480
        target_chars = max(120, min(target_chars, 6000))
        if edited:
            if "雨声敲在窗沿" in user_prompt:
                seed = (
                    "清晨的雾从河面缓慢升起，她沿着空旷的石阶向上走，终于看见等在钟楼下的人。"
                    "昨夜没有说完的话被重新摆到两人之间，这一次谁都没有回避。"
                    "钟声响起时，她先一步打破沉默，把真正的条件和最后的底线逐一说明。"
                )
            else:
                seed = (
                    "雨声敲在窗沿上，她没有立即回头，而是先把掌心那张折皱的纸压平。"
                    "走廊里的脚步由远及近，每一步都让先前的犹豫变得更清晰。"
                    "当门锁轻响，她终于抬起眼睛，平静地说出了已经准备许久的决定。"
                )
        else:
            seed = (
                f"夜色渐深，城市的灯火在远处明灭不定{char_note}。"
                "她站在窗前，手指轻轻拂过冰凉的玻璃，心中那股难以名状的情绪逐渐沉静。"
                "身后的脚步声停顿了一瞬，然后继续靠近，她知道这场迟来的谈话已经无法回避。"
            )
        repeated = (seed * ((target_chars // max(len(seed), 1)) + 2))[: max(target_chars - 1, 1)]
        return repeated.rstrip("，；：、") + "。"

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

    def _mock_knowledge_qa(self, user_prompt: str) -> str:
        question = ""
        if "## 用户问题" in user_prompt:
            question = user_prompt.rsplit("## 用户问题", 1)[1].strip().splitlines()[0].strip()

        evidence_text = user_prompt.split("## 用户问题", 1)[0]
        if "（未找到相关证据）" in evidence_text or not evidence_text.strip():
            answer = "当前资料中没有找到足够证据回答这个问题。"
        else:
            known_systems = (
                "雷霆系", "寒冰系", "火炎系", "凌水系", "圣光系",
                "暗影系", "召唤系", "空间系", "混沌系", "治愈系", "亡灵系", "心灵系",
                "诅咒系", "植物系", "音系", "毒系", "土系", "火系", "雷系", "冰系",
                "水系", "风系", "光系",
            )
            normalize = {"雷霆系": "雷系", "寒冰系": "冰系", "火炎系": "火系", "凌水系": "水系", "圣光系": "光系"}
            systems: list[str] = []
            for raw in known_systems:
                if raw not in evidence_text:
                    continue
                name = normalize.get(raw, raw)
                if name not in systems:
                    systems.append(name)
            if "什么系" in question and systems:
                answer = "根据当前资料，明确提到的法系包括：\n\n" + "\n".join(f"- {name}" for name in systems[:8])
            else:
                lines = [
                    re.sub(r"^\s*-\s*\[[^\]]+\]\s*", "", line).strip()
                    for line in evidence_text.splitlines()
                    if line.strip().startswith("- [")
                ]
                summary = lines[0][:220] if lines else evidence_text.strip()[:220]
                answer = f"根据当前资料片段：{summary}"

        return json.dumps({"answer": answer, "citations": []}, ensure_ascii=False)

    def _mock_query_plan(self, user_prompt: str) -> str:
        question = ""
        if "## 当前问题" in user_prompt:
            question = user_prompt.split("## 当前问题", 1)[1].strip().splitlines()[0].strip()
        try:
            from services.knowledge_query_plan import build_knowledge_query_plan

            plan = build_knowledge_query_plan(question)
            intent_map = {
                "ability": "character_ability",
                "character_profile": "character_profile",
                "character_by_ability": "character_by_ability",
                "relationship": "relationship",
                "worldbuilding": "worldbuilding",
                "timeline": "timeline",
                "plot": "plot_event",
                "general": "general",
            }
            attributes = [entity for entity in plan.entities if entity.endswith("系")]
            entities = [entity for entity in plan.entities if entity not in attributes]
            payload = {
                "rewritten_question": None,
                "intent": intent_map.get(plan.intent, "general"),
                "entities": entities,
                "attributes": attributes,
                "relation_targets": [],
                "time_scope": None,
                "sub_queries": plan.search_queries,
                "required_terms": plan.required_terms,
                "optional_terms": plan.keywords,
                "evidence_policy": "基于资料片段回答。",
                "answer_policy": "如资料不足则说明不足，不编造。",
                "confidence": 0.1,
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {
                    "rewritten_question": question,
                    "intent": "general",
                    "entities": [],
                    "attributes": [],
                    "relation_targets": [],
                    "time_scope": None,
                    "sub_queries": [question] if question else [],
                    "required_terms": [],
                    "optional_terms": [],
                    "evidence_policy": "基于资料片段回答。",
                    "answer_policy": "如资料不足则说明不足，不编造。",
                    "confidence": 0.5,
                },
                ensure_ascii=False,
            )

    def _mock_novel_extraction(self, user_prompt: str) -> str:
        """Mock 小说章节结构化抽取，返回符合 Schema 的 JSON。

        根据章节正文里的关键词生成人物/能力/事件/世界规则，便于测试流水线。
        """
        import json as _json
        # 提取章节编号
        no_match = re.search(r"章节编号：(\d+)", user_prompt)
        chapter_no = int(no_match.group(1)) if no_match else 1
        # 提取章节标题
        title_match = re.search(r"章节标题：([^\n]*)", user_prompt)
        chapter_title = title_match.group(1).strip() if title_match else ""

        content_lower = user_prompt.lower()
        characters = []
        abilities = []
        events = []
        world_rules = []

        # 根据正文关键词生成 mock 数据
        if "莫凡" in user_prompt:
            characters.append({
                "name": "莫凡", "aliases": [], "identity": "法师", "status": "觉醒",
                "importance": 5, "confidence": 0.9, "evidence": "莫凡觉醒了魔法",
            })
            if "火系" in user_prompt or "火" in user_prompt:
                abilities.append({
                    "character": "莫凡", "ability_type": "magic_element",
                    "ability_name": "火系", "level": "初阶", "status": "new",
                    "importance": 5, "confidence": 0.9, "evidence": "莫凡觉醒了火系",
                })
            if "雷" in user_prompt:
                abilities.append({
                    "character": "莫凡", "ability_type": "magic_element",
                    "ability_name": "雷系", "level": "初阶", "status": "new",
                    "importance": 4, "confidence": 0.8, "evidence": "莫凡的雷霆系星尘",
                })
        if "张小侯" in user_prompt:
            characters.append({
                "name": "张小侯", "aliases": [], "identity": "法师", "status": "活跃",
                "importance": 3, "confidence": 0.8, "evidence": "张小侯释放风轨",
            })
            abilities.append({
                "character": "张小侯", "ability_type": "magic_element",
                "ability_name": "风系", "level": "初阶", "status": "used",
                "importance": 3, "confidence": 0.8, "evidence": "张小侯释放风轨击退敌人",
            })

        events.append({
            "event_title": f"第{chapter_no}章事件",
            "event_desc": "本章主要事件",
            "characters": [c["name"] for c in characters],
            "location": "", "cause": "", "effect": "",
            "importance": 3, "confidence": 0.7, "evidence": "章节事件摘要",
        })
        world_rules.append({
            "category": "魔法体系", "rule_text": "法师可觉醒多种魔法系别",
            "priority": "medium", "confidence": 0.8, "evidence": "魔法体系设定",
        })

        result = {
            "chapter_no": chapter_no,
            "chapter_title": chapter_title,
            "chapter_summary": "mock 抽取摘要",
            "characters": characters,
            "abilities": abilities,
            "events": events,
            "world_rules": world_rules,
        }
        return _json.dumps(result, ensure_ascii=False)

    def _mock_structure_extraction(self, user_prompt: str) -> str:
        title_match = re.search(r"###\s*第?(\d+|\?)章\s+(.+)", user_prompt)
        sequence = 1
        title = "导入章节"
        if title_match:
            try:
                sequence = int(title_match.group(1))
            except ValueError:
                sequence = 1
            title = title_match.group(2).strip()[:80] or title

        return json.dumps(
            {
                "outlines": [
                    {
                        "sequence_number": sequence,
                        "title": title,
                        "summary": "主角在关键场景中面对新的信息与选择。",
                        "turning_point": "隐藏线索浮出水面。",
                    }
                ],
                "characters": [
                    {
                        "name": "林澈",
                        "role_type": "protagonist",
                        "profile": "冷静敏锐的主角，正在追查事件真相。",
                        "faction": None,
                        "appearance_count": 1,
                        "metadata": {},
                    }
                ],
                "world_entries": [
                    {
                        "title": "旧城档案馆",
                        "category": "地点",
                        "content": "保存旧城历史记录的场所，可能藏有关键资料。",
                        "rules": {},
                        "confidence": "medium",
                    }
                ],
                "hidden_threads": [
                    {
                        "name": "失踪档案",
                        "description": "多年前遗失的档案与当前事件存在关联。",
                        "chapter_nums": [sequence],
                    }
                ],
                "character_relations": [],
                "character_events": [
                    {
                        "character_name": "林澈",
                        "sequence_number": sequence,
                        "appearance_type": "appeared",
                        "event_summary": "林澈在旧城档案馆发现失踪档案，并意识到它与当前事件有关。",
                        "actions": ["进入旧城档案馆", "发现失踪档案"],
                        "state_change": "掌握新的调查线索",
                        "location": "旧城档案馆",
                        "emotion": "警觉",
                        "importance": 4,
                    }
                ],
            },
            ensure_ascii=False,
        )

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
        del tools, response_format, timeout
        started = time.perf_counter()
        text = await self.generate(system_prompt, user_prompt, temperature, max_tokens)
        input_tokens = math.ceil((len(system_prompt) + len(user_prompt)) / 2)
        output_tokens = math.ceil(len(text) / 2)
        usage = TokenUsage(input_tokens, output_tokens, input_tokens + output_tokens, estimated=True)
        _set_call_metadata(retries=0, finish_reason=None)
        return LLMResult(
            text=text,
            usage=usage,
            tool_calls=[],
            finish_reason=None,
            model=getattr(self, "model", None),
            retries=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def generate_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        *,
        usage_sink: list[TokenUsage] | None = None,
    ) -> AsyncIterator[str]:
        """把 mock 完整结果切成小块，模拟真实模型流式 token。"""
        result = await self.generate(system_prompt, user_prompt, temperature, max_tokens)
        for i in range(0, len(result), 5):
            yield result[i:i+5]
            await asyncio.sleep(0.05)
        if usage_sink is not None:
            input_tokens = math.ceil((len(system_prompt) + len(user_prompt)) / 2)
            output_tokens = math.ceil(len(result) / 2)
            usage_sink.append(
                TokenUsage(input_tokens, output_tokens, input_tokens + output_tokens, estimated=True)
            )
        _set_call_metadata(retries=0, finish_reason=None)
