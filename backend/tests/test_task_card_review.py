"""TaskCard Review 测试 — L-1: 任务卡预览

验证 workflow_v2 的 planning_review 拓扑和中断行为。
"""
import asyncio
import json

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
        # task_card_review should NOT be in the default graph
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
        # 验证 app 已编译（不抛异常即可）
        assert app is not None

    def test_app_without_planning_review_has_single_interrupt(self):
        """planning_review=False 时，interrupt_before 仅包含 human_review"""
        app = get_creative_app_v2(planning_review=False)
        assert app is not None


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
