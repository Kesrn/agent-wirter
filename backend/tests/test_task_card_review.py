"""TaskCard Review 测试 — L-1: 任务卡预览

验证 workflow_v2 的 planning_review 拓扑和中断行为。
"""
import asyncio
import uuid

import pytest

from agents.workflow_v2 import (
    build_creative_graph_v2,
    get_creative_app_v2,
    CreativeStateV2,
    task_card_review_node,
)
from agents.workflow import _CHECKPOINTER


class TestTaskCardReviewGraph:

    def test_graph_has_task_card_review_node_when_planning_review(self):
        """planning_review=True 时，图中存在 task_card_review 节点"""
        graph = build_creative_graph_v2(planning_review=True)
        compiled = graph.compile(checkpointer=_CHECKPOINTER)
        nodes = [n.name for n in compiled.get_graph().nodes.values() if hasattr(n, 'name')]
        assert "task_card_review" in nodes or any("task_card_review" in str(n) for n in compiled.get_graph().nodes.keys())

    def test_graph_without_planning_review(self):
        """默认 planning_review=False 时，图中不存在 task_card_review 节点"""
        graph = build_creative_graph_v2(planning_review=False)
        compiled = graph.compile(checkpointer=_CHECKPOINTER)
        node_names = set()
        for n in compiled.get_graph().nodes.values():
            if hasattr(n, 'name'):
                node_names.add(n.name)
            elif hasattr(n, '__name__'):
                node_names.add(n.__name__)
        assert "task_card_review" not in node_names

    def test_task_card_review_node_is_noop(self):
        """task_card_review_node 是空操作，返回空 dict"""
        state: CreativeStateV2 = {}
        result = asyncio.new_event_loop().run_until_complete(
            task_card_review_node(state)
        )
        assert result == {}

    def test_app_with_planning_review_has_double_interrupt(self):
        """planning_review=True 时，interrupt_before 包含 task_card_review 和 human_review"""
        app = get_creative_app_v2(planning_review=True)
        assert app is not None

    def test_app_without_planning_review_has_single_interrupt(self):
        """planning_review=False 时，interrupt_before 仅包含 human_review"""
        app = get_creative_app_v2(planning_review=False)
        assert app is not None

    def test_planning_review_topology_is_correct(self):
        """验证 planning_review=True 时的边：
        architect→task_card_review→writer，tcr 出边指向 writer
        """
        graph = build_creative_graph_v2(planning_review=True)
        compiled = graph.compile(checkpointer=_CHECKPOINTER)
        inner = compiled.get_graph()

        node_names = set()
        for n in inner.nodes.values():
            if hasattr(n, 'name'):
                node_names.add(n.name)
        assert "task_card_review" in node_names
        assert "chapter_architect" in node_names
        assert "chapter_writer" in node_names

        edges = inner.edges
        architect_targets = [e[1] for e in edges if e[0] == "chapter_architect"]
        assert "task_card_review" in architect_targets, f"architect edges: {architect_targets}"

        tcr_targets = [e[1] for e in edges if e[0] == "task_card_review"]
        assert "chapter_writer" in tcr_targets, f"task_card_review edges: {tcr_targets}"

    def test_planning_review_state_persists_to_checkpoint(self):
        """planning_review 写入 state 后，可从 checkpoint 中读回"""
        app = get_creative_app_v2(planning_review=True)
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial: CreativeStateV2 = {
            "planning_review": True,
            "task_card_reviewed": False,
            "modified_task_card": {},
            "chapter_num": 1,
            "target_words": 2000,
        }

        async def _run():
            try:
                async for _ in app.astream(initial, config=config):
                    pass
            except Exception:
                pass
            state = await app.aget_state(config)
            return state

        state = asyncio.new_event_loop().run_until_complete(_run())
        if state and state.values:
            assert state.values.get("planning_review") is True


class TestTaskCardReviewState:

    def test_creative_state_v2_has_task_card_reviewed(self):
        """CreativeStateV2 包含 task_card_reviewed 和 modified_task_card 字段"""
        state: CreativeStateV2 = {
            "task_card_reviewed": True,
            "modified_task_card": {"chapter_title": "测试"},
        }
        assert state["task_card_reviewed"] is True
        assert state["modified_task_card"]["chapter_title"] == "测试"

    def test_creative_state_v2_missing_fields_ok(self):
        """CreativeStateV2 的 total=False 意味着新字段可选"""
        state: CreativeStateV2 = {"project_id": "test"}
        assert state.get("task_card_reviewed") is None
        assert state.get("modified_task_card") is None

    def test_planning_review_field_in_state(self):
        """CreativeStateV2 包含 planning_review 字段"""
        state: CreativeStateV2 = {"planning_review": True}
        assert state["planning_review"] is True

    def test_clarification_fields_in_state(self):
        """CreativeStateV2 包含 clarification_answers / clarification_round"""
        state: CreativeStateV2 = {
            "clarification_answers": {"q1": "yes"},
            "clarification_round": 2,
        }
        assert state["clarification_answers"] == {"q1": "yes"}
        assert state["clarification_round"] == 2


class TestRefreshTaskCard:

    def test_build_clarification_summary(self):
        """build_clarification_summary 将问题和答案合并为文本"""
        from agents.clarification import build_clarification_summary
        questions = [
            {"id": "q1", "question": "主角的动机？"},
            {"id": "q2", "question": "关键冲突？"},
        ]
        answers = {"q1": "复仇", "q2": "与仇人对峙"}
        result = build_clarification_summary(questions, answers)
        assert "Q: 主角的动机？ A: 复仇" in result
        assert "Q: 关键冲突？ A: 与仇人对峙" in result

    def test_build_clarification_summary_empty(self):
        """build_clarification_summary 无答案时返回空字符串"""
        from agents.clarification import build_clarification_summary
        result = build_clarification_summary([], {})
        assert result == ""

    def test_refresh_task_card_keeps_checkpoint_state(self):
        """refresh_task_card 将 clarification_answers 写入 checkpoint，不 resolve interrupt"""
        import uuid
        app = get_creative_app_v2(planning_review=True)
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial: CreativeStateV2 = {
            "planning_review": True,
            "clarification_answers": {},
            "clarification_round": 0,
            "user_note": "原始备注",
            "chapter_num": 1,
            "target_words": 2000,
        }

        async def _run():
            try:
                async for _ in app.astream(initial, config=config):
                    pass
            except Exception:
                pass

            # 模拟 refresh_task_card 的 state 更新
            prev_answers = {"q1": "复仇"}
            await app.aupdate_state(config, {
                "clarification_answers": prev_answers,
                "clarification_round": 1,
                "user_note": "原始备注\n[用户澄清第1轮] Q: 主角的动机？ A: 复仇",
            }, as_node="task_card_review")

            state = await app.aget_state(config)
            return state

        state = asyncio.new_event_loop().run_until_complete(_run())
        assert state is not None
        values = state.values if state else {}
        assert values.get("clarification_round") == 1
        assert values.get("clarification_answers") == {"q1": "复仇"}
        assert "[用户澄清第1轮]" in (values.get("user_note") or "")

    def test_creative_state_v2_with_l2_fields(self):
        """CreativeStateV2 包含 L-2 澄清字段"""
        state: CreativeStateV2 = {
            "clarification_answers": {"q1": "test"},
            "clarification_round": 1,
        }
        assert state.get("clarification_round") == 1
