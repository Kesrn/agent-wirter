"""Phase I-6 测试 — Story Recorder + Memory Curator

验证：
1. parse_story_record 容错解析（纯 JSON / 散文 / 失败 fallback）
2. run_story_recorder 调 LLM 返回 Story Record
3. story_record_to_facts 把 Story Record 转为 staging facts
4. PLOT_FACT 不硬塞 CharacterEvent（通过 memory_type 映射验证）
5. EVENT 有 character_name 时保留（后续 confirm 入 CharacterEvent）
6. story-recorder skill 可扫描
7. run_story_recorder LLM 失败 fallback
"""

import asyncio
import json

import pytest

from test_smoke import setup_db  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


# ── parse_story_record ────────────────────────────────


class TestParseStoryRecord:
    def test_parse_pure_json(self):
        from agents.story_recorder import parse_story_record

        raw = json.dumps({
            "summary": "主角觉醒魔法",
            "events": [{"title": "觉醒", "description": "雷系觉醒", "character_names": ["程璇"]}],
            "character_state_changes": [{"character_name": "程璇", "change": "变为觉醒者"}],
            "relationship_changes": [],
            "ability_changes": [{"character_name": "程璇", "ability": "雷系", "change": "获得"}],
            "foreshadowing_new": [{"title": "神秘观察", "description": "有人暗中观察"}],
            "foreshadowing_resolved": [],
            "timeline": {"time_point": "觉醒日"},
            "knowledge_state_changes": [],
        })
        result = parse_story_record(raw)
        assert result["parse_error"] is False
        assert result["summary"] == "主角觉醒魔法"
        assert len(result["events"]) == 1
        assert result["events"][0]["title"] == "觉醒"
        assert len(result["ability_changes"]) == 1

    def test_parse_from_prose(self):
        from agents.story_recorder import parse_story_record

        raw = '分析结果如下：\n{"summary": "测试", "events": [{"title": "事件1", "description": "描述"}], "character_state_changes": [], "relationship_changes": [], "ability_changes": [], "foreshadowing_new": [], "foreshadowing_resolved": [], "timeline": {}, "knowledge_state_changes": []}\n以上。'
        result = parse_story_record(raw)
        assert result["parse_error"] is False
        assert result["summary"] == "测试"
        assert len(result["events"]) == 1

    def test_parse_failure_returns_empty(self):
        from agents.story_recorder import parse_story_record

        result = parse_story_record("这不是JSON")
        assert result["parse_error"] is True
        assert result["events"] == []
        assert result["summary"] == ""

    def test_parse_empty_string(self):
        from agents.story_recorder import parse_story_record

        result = parse_story_record("")
        assert result["parse_error"] is True
        assert result["events"] == []


# ── run_story_recorder ────────────────────────────────


class TestRunStoryRecorder:
    def test_llm_success(self):
        from agents.story_recorder import run_story_recorder

        result = asyncio.new_event_loop().run_until_complete(
            run_story_recorder(
                llm_config=None,
                content="程璇感到体内涌起一股雷电之力。",
                context="## 本章大纲\n第1章 觉醒",
            )
        )
        assert result["parse_error"] is False
        assert "程璇" in result["summary"] or result["summary"]  # mock 返回非空 summary
        assert len(result["events"]) >= 1
        assert len(result["ability_changes"]) >= 1

    def test_llm_exception_fallback(self):
        from agents.story_recorder import run_story_recorder
        from agents.llm_provider import MockProvider

        class FailingProvider(MockProvider):
            async def generate(self, *args, **kwargs):
                raise RuntimeError("LLM 不可用")

        import agents.llm_provider as llm_mod
        original = llm_mod.get_llm_provider
        llm_mod.get_llm_provider = lambda config: FailingProvider()
        try:
            result = asyncio.new_event_loop().run_until_complete(
                run_story_recorder(llm_config=None, content="正文", context="")
            )
        finally:
            llm_mod.get_llm_provider = original

        assert result["parse_error"] is True
        assert result["events"] == []


# ── story_record_to_facts (memory curator) ────────────


class TestStoryRecordToFacts:
    def test_events_become_plot_fact(self):
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [{"title": "觉醒", "description": "雷系觉醒", "character_names": ["程璇"], "evidence": "原文"}],
            "character_state_changes": [],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        assert len(facts) == 1
        assert facts[0]["memory_type"] == "PLOT_FACT"
        assert facts[0]["title"] == "觉醒"
        assert facts[0]["evidence"] == "原文"

    def test_character_state_becomes_character(self):
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [],
            "character_state_changes": [{"character_name": "程璇", "change": "变为觉醒者", "evidence": "原文"}],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        assert len(facts) == 1
        assert facts[0]["memory_type"] == "CHARACTER"
        assert facts[0]["title"] == "程璇"
        assert facts[0]["payload"]["name"] == "程璇"

    def test_ability_becomes_event_with_character_name(self):
        """ability_changes → EVENT，保留 character_name 供后续 confirm 入 CharacterEvent"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [],
            "character_state_changes": [],
            "relationship_changes": [],
            "ability_changes": [{"character_name": "程璇", "ability": "雷系魔法", "change": "获得", "evidence": "原文"}],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        assert len(facts) == 1
        assert facts[0]["memory_type"] == "EVENT"
        assert facts[0]["payload"]["character_name"] == "程璇"

    def test_foreshadowing_new_becomes_foreshadowing(self):
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [],
            "character_state_changes": [],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [{"title": "神秘观察", "description": "有人暗中观察", "evidence": "原文"}],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        assert len(facts) == 1
        assert facts[0]["memory_type"] == "FORESHADOWING"
        assert facts[0]["title"] == "神秘观察"
        assert 1 in facts[0]["payload"]["chapter_nums"]

    def test_plot_fact_not_event(self):
        """PLOT_FACT 不硬塞 CharacterEvent — events/relationship/knowledge 都是 PLOT_FACT"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [{"title": "事件", "description": "描述"}],
            "character_state_changes": [],
            "relationship_changes": [{"characters": ["A", "B"], "change": "关系变化"}],
            "ability_changes": [],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [{"title": "伏笔", "description": "回收"}],
            "knowledge_state_changes": [{"description": "信息揭示"}],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        # 普通事件/关系/知识是 PLOT_FACT；伏笔回收保留 FORESHADOWING 语义，
        # 但不能退化成泛化 EVENT。
        assert [f["memory_type"] for f in facts] == [
            "PLOT_FACT", "PLOT_FACT", "FORESHADOWING", "PLOT_FACT",
        ]
        assert all(f["memory_type"] != "EVENT" for f in facts)
        assert len(facts) == 4  # events + relationship + foreshadowing_resolved + knowledge

    def test_empty_record_returns_empty(self):
        from agents.memory_curator import story_record_to_facts

        assert story_record_to_facts({}) == []
        assert story_record_to_facts({"parse_error": True}) == []

    def test_items_without_title_skipped(self):
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [{"title": "", "description": "无标题"}],
            "character_state_changes": [{"character_name": "", "change": "无名字"}],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [{"title": "", "description": "无标题"}],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [{"description": ""}],
        }
        facts = story_record_to_facts(record, chapter_seq=1)
        assert len(facts) == 0


# ── Skill 扫描 ─────────────────────────────────────────


class TestStoryRecorderSkill:
    def test_skill_scannable(self):
        from skills.registry import scan_skills

        skills = scan_skills()
        assert "story-recorder" in skills
        assert "memory-curator" in skills


# ── 集成：Story Record → facts → staging 格式 ──────────


class TestStoryRecordToStagingFormat:
    def test_facts_have_required_fields(self):
        """转换后的 facts 有 memory_type/title/payload/evidence"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "events": [{"title": "事件", "description": "描述", "evidence": "证据"}],
            "character_state_changes": [],
            "relationship_changes": [],
            "ability_changes": [],
            "foreshadowing_new": [],
            "foreshadowing_resolved": [],
            "knowledge_state_changes": [],
        }
        facts = story_record_to_facts(record, chapter_seq=2)
        for f in facts:
            assert "memory_type" in f
            assert "title" in f
            assert "payload" in f
            assert "evidence" in f
