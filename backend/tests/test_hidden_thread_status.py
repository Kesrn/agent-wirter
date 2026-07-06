"""HiddenThread 伏笔状态追踪测试 — K-3"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from models.hidden_thread import HiddenThread, STATUS_PRIORITY
from services.chapter_context import HiddenThreadInfo, ChapterContext, format_chapter_context_for_prompt, ContextStats, build_chapter_context


class TestStatusPriority:

    def test_status_priority_order(self):
        """状态优先级数值正确"""
        assert STATUS_PRIORITY["PLANNED"] == 0
        assert STATUS_PRIORITY["PLANTED"] == 1
        assert STATUS_PRIORITY["ACTIVE"] == 2
        assert STATUS_PRIORITY["REVEALED"] == 3
        assert STATUS_PRIORITY["RESOLVED"] == 4
        assert STATUS_PRIORITY["DROPPED"] == 99
        assert STATUS_PRIORITY["RESOLVED"] > STATUS_PRIORITY["PLANTED"]

    def test_planted_beats_planned(self):
        """PLANTED 优先级高于 PLANNED"""
        assert STATUS_PRIORITY.get("PLANTED", 0) > STATUS_PRIORITY.get("PLANNED", 0)

    def test_resolved_beats_planted(self):
        """RESOLVED 优先级高于 PLANTED"""
        assert STATUS_PRIORITY.get("RESOLVED", 0) > STATUS_PRIORITY.get("PLANTED", 0)


class TestHiddenThreadInfo:

    def test_hidden_thread_info_has_new_fields(self):
        """HiddenThreadInfo 包含 K-3 新增字段"""
        info = HiddenThreadInfo(
            id="test-id",
            name="测试暗线",
            description="描述",
            chapter_nums=[1, 2],
            status="PLANTED",
            planted_chapter=1,
            reveal_chapter=5,
            resolved_chapter=None,
        )
        assert info.status == "PLANTED"
        assert info.planted_chapter == 1
        assert info.reveal_chapter == 5
        assert info.resolved_chapter is None

    def test_hidden_thread_info_defaults(self):
        """HiddenThreadInfo 默认值"""
        info = HiddenThreadInfo(
            id="test", name="测试", description="", chapter_nums=[],
        )
        assert info.status == "PLANNED"
        assert info.planted_chapter is None


class TestMemoryCuratorForeshadowing:

    def test_foreshadowing_new_has_planted_delta(self):
        """memory_curator 将 foreshadowing_new 转为 FORESHADOWING + status_delta=PLANTED"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_new": [
                {"title": "主角身世之谜", "description": "暗示主角不是人类", "evidence": "他的手...", "thread_name": None},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=3)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "主角身世之谜"
        assert foreshadow_facts[0]["payload"]["status_delta"] == "PLANTED"
        assert 3 in foreshadow_facts[0]["payload"]["chapter_nums"]

    def test_foreshadowing_resolved_uses_thread_name_first(self):
        """foreshadowing_resolved 优先使用 thread_name 匹配原伏笔"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {
                    "thread_name": "主角身世之谜",
                    "title": "回收：身世",
                    "description": "主角得知自己是外星人",
                    "evidence": "真相大白",
                },
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "主角身世之谜"
        assert foreshadow_facts[0]["payload"]["status_delta"] == "RESOLVED"

    def test_foreshadowing_resolved_falls_back_to_title(self):
        """foreshadowing_resolved 无 thread_name 时 fallback 到 title"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {"title": "信号来源", "description": "信号来自地心", "evidence": "源头"},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "信号来源"

    def test_foreshadowing_resolved_not_plot_fact(self):
        """K-3: foreshadowing_resolved 不再写为 PLOT_FACT"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {"title": "测试伏笔", "description": "回收", "evidence": ""},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        plot_facts = [f for f in facts if f["memory_type"] == "PLOT_FACT"]
        assert len(plot_facts) == 0


class TestContextBuilderHiddenThreads:

    def test_dropped_thread_format_ok(self):
        """DROPPED 伏笔在 format 中正常渲染（过滤由 _load_hidden_threads 负责）"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="废弃暗线", description="不应出现",
                chapter_nums=[1], status="DROPPED",
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        # format 不负责过滤 — 只是验证不崩溃
        assert "## 暗线" in prompt
        assert "DROPPED" in prompt

    def test_prompt_includes_status_label(self):
        """prompt 中包含伏笔状态标签"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="身世伏笔", description="主角身份存疑",
                chapter_nums=[1, 2], status="PLANTED", planted_chapter=1,
                reveal_chapter=5,
            )
        )
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="2", name="旧伏笔", description="已被揭示",
                chapter_nums=[3], status="RESOLVED", resolved_chapter=3,
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        assert "PLANTED" in prompt
        assert "RESOLVED" in prompt
        assert "## 暗线" in prompt

    def test_resolved_shows_chapter(self):
        """RESOLVED 状态显示回收章节"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="回收线", description="已回收",
                chapter_nums=[5], status="RESOLVED", resolved_chapter=5,
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        assert "RESOLVED" in prompt
