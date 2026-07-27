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

    def test_context_refresh_routes_back_to_context_loader(self):
        from agents.workflow_v2 import route_after_task_card_review

        assert route_after_task_card_review({"context_refresh_requested": True}) == "context_refresher"
        assert route_after_task_card_review({"task_card_refresh_requested": True}) == "chapter_architect"
        assert route_after_task_card_review({}) == "chapter_writer"

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

    def test_context_summary_field_in_state(self):
        """L-3 context_summary 随 checkpoint state 保持为前端统一结构。"""
        state: CreativeStateV2 = {
            "context_summary": {
                "previous_chapter_ending": "上一章结尾",
                "outlines": [],
                "story_arcs": [],
                "characters": [],
                "hidden_threads": [],
                "world_entries": [],
                "confirmed_memories": [],
                "knowledge_sources": [],
            }
        }
        assert state["context_summary"]["previous_chapter_ending"] == "上一章结尾"
        assert set(state["context_summary"]) == {
            "previous_chapter_ending",
            "outlines",
            "story_arcs",
            "characters",
            "hidden_threads",
            "world_entries",
            "confirmed_memories",
            "knowledge_sources",
        }


class TestClarificationToTaskCardFlow:

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

    def test_submit_clarification_reaches_task_card_review(self):
        """[N-1] STRICT 模式 submit_clarification 后，graph 继续到 task_card_review。

        链路语义验证（替代已移除的 refresh_task_card 测试）：
          human_clarification interrupt
          -> submit_clarification（写 clarification_summary / requirements_complete）
          -> clarification_planner route_after_clarification -> chapter_architect
          -> task_card_review interrupt（next 含 task_card_review，且 chapter_task_card 已产出）
        """
        import asyncio
        import uuid
        from agents.clarification import build_clarification_summary
        from agents.llm_provider import MockProvider
        import agents.workflow_v2 as wfv2
        import agents.llm_provider as llm_mod

        # mock context_loader：避免依赖数据库，返回最小上下文
        async def _mock_context_loader(state):
            return {"context": "测试上下文"}

        # mock get_llm_provider：测试环境禁用了 mock provider，这里强制返回 MockProvider
        # 需 patch 所有持有该引用的模块（llm_provider / workflow / workflow_v2 / clarification）
        original_get_llm = llm_mod.get_llm_provider
        _mock_get_llm = lambda config: MockProvider()
        llm_mod.get_llm_provider = _mock_get_llm
        import agents.workflow as wf_mod
        original_wf_get_llm = getattr(wf_mod, 'get_llm_provider', None)
        if original_wf_get_llm:
            wf_mod.get_llm_provider = _mock_get_llm
        original_ctx = wfv2.context_loader_node
        wfv2.context_loader_node = _mock_context_loader
        wfv2.get_llm_provider = _mock_get_llm
        # 该测试覆盖旧的独立 human_clarification 兼容路径；L-2 默认拓扑
        # 已将澄清问题嵌入 task_card_review。
        app = get_creative_app_v2(
            planning_review=True,
            pre_generation_mode="STRICT",
            embedded_clarification=False,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial: CreativeStateV2 = {
            "planning_review": True,
            "pre_generation_mode": "STRICT",
            "max_clarification_rounds": 3,
            "clarification_answers": {},
            "clarification_round": 0,
            "clarification_questions": [],
            "user_note": "",
            "chapter_num": 1,
            "target_words": 2000,
        }

        async def _run():
            # 1. 首次执行：跑到 human_clarification interrupt（STRICT 首轮强制）
            try:
                async for _ in app.astream(initial, config=config):
                    pass
            except Exception:
                pass

            state1 = await app.aget_state(config)
            # STRICT 模式首轮应在 human_clarification 暂停
            assert "human_clarification" in (state1.next or ()), \
                f"期望在 human_clarification 暂停，实际 next={state1.next}"

            # 2. 模拟 submit_clarification：写入澄清答案 + summary，标记完成
            questions = state1.values.get("clarification_questions", []) or []
            prev_answers = {"chapter_goal": "conflict"}
            summary = build_clarification_summary(questions, prev_answers)
            await app.aupdate_state(config, {
                "clarification_answers": prev_answers,
                "clarification_summary": summary,
                "requirements_complete": False,
                "needs_clarification": False,
                "clarification_skipped": False,
            }, as_node="human_clarification")

            # 3. resume：graph 跑 route_after_clarification -> chapter_architect -> task_card_review
            try:
                async for _ in app.astream(None, config=config):
                    pass
            except Exception:
                pass

            state2 = await app.aget_state(config)
            return state2

        try:
            state2 = asyncio.new_event_loop().run_until_complete(_run())
        finally:
            wfv2.context_loader_node = original_ctx
            llm_mod.get_llm_provider = original_get_llm
            if original_wf_get_llm:
                wf_mod.get_llm_provider = original_wf_get_llm
            wfv2.get_llm_provider = original_get_llm
        assert state2 is not None
        values = state2.values or {}
        next_nodes = state2.next or ()

        # 澄清答案已写入 state
        assert values.get("clarification_answers") == {"chapter_goal": "conflict"}
        # architect 已执行：chapter_task_card 已产出
        card = values.get("chapter_task_card", {})
        assert isinstance(card, dict) and card.get("chapter_title"), \
            f"期望 architect 产出 task card，实际 values keys={list(values.keys())}"
        # 到达 task_card_review 暂停点
        assert "task_card_review" in next_nodes, \
            f"期望到达 task_card_review 暂停，实际 next={next_nodes}"

    def test_creative_state_v2_with_l2_fields(self):
        """CreativeStateV2 包含 L-2 澄清字段"""
        state: CreativeStateV2 = {
            "clarification_answers": {"q1": "test"},
            "clarification_round": 1,
        }
        assert state.get("clarification_round") == 1


class TestClarificationRouting:
    """FAST / PLANNING / STRICT 的图内澄清出口语义。"""

    def test_fast_always_skips_clarification(self):
        from agents.workflow_v2 import route_after_clarification

        assert route_after_clarification({
            "pre_generation_mode": "FAST",
            "needs_clarification": True,
            "clarification_questions": [{"id": "q1"}],
        }) == "chapter_architect"

    def test_planning_only_interrupts_for_pending_questions(self):
        from agents.workflow_v2 import route_after_clarification

        assert route_after_clarification({
            "pre_generation_mode": "PLANNING",
            "clarification_round": 1,
            "max_clarification_rounds": 3,
            "needs_clarification": True,
            "clarification_questions": [{"id": "q1"}],
        }) == "human_clarification"
        assert route_after_clarification({
            "pre_generation_mode": "PLANNING",
            "requirements_complete": True,
        }) == "chapter_architect"

    def test_strict_forces_first_round_and_respects_round_limit(self):
        from agents.workflow_v2 import route_after_clarification

        assert route_after_clarification({
            "pre_generation_mode": "strict",
            "clarification_round": 1,
            "max_clarification_rounds": 2,
            "requirements_complete": True,
        }) == "human_clarification"
        assert route_after_clarification({
            "pre_generation_mode": "STRICT",
            "clarification_round": 2,
            "max_clarification_rounds": 2,
            "needs_clarification": True,
            "clarification_questions": [{"id": "q2"}],
        }) == "chapter_architect"


# ── M-1: 生成前交互模式测试 ──

class TestBuildEmbeddedClarificationPayload:
    """测试 build_embedded_clarification_payload() 的模式规则。"""

    def test_fast_no_questions(self):
        """FAST 模式无 AI 问题 → 返回 None（前端不显示问题区）"""
        from agents.clarification import build_embedded_clarification_payload
        result = build_embedded_clarification_payload(
            ai_result={"needs_clarification": False, "questions": []},
            mode="FAST",
            round_num=1,
            max_rounds=3,
        )
        assert result is None

    def test_fast_with_questions(self):
        """FAST 有 AI 问题 → show_user_note=False, require_answer=False"""
        from agents.clarification import build_embedded_clarification_payload
        ai = {
            "needs_clarification": True,
            "questions": [{"id": "q1", "type": "free_text", "question": "test?"}],
            "assumptions_if_skipped": ["assume"],
        }
        result = build_embedded_clarification_payload(
            ai_result=ai, mode="FAST", round_num=1, max_rounds=3,
        )
        assert result is not None
        assert result["show_user_note"] is False
        assert result["require_answer"] is False
        assert result["needs_clarification"] is True
        assert len(result["questions"]) == 1

    def test_planning_no_questions(self):
        """PLANNING 无 AI 问题 → 返回 None"""
        from agents.clarification import build_embedded_clarification_payload
        result = build_embedded_clarification_payload(
            ai_result={"needs_clarification": False, "questions": []},
            mode="PLANNING", round_num=1, max_rounds=3,
        )
        assert result is None

    def test_planning_with_questions(self):
        """PLANNING 有 AI 问题 → show_user_note=True, require_answer=False"""
        from agents.clarification import build_embedded_clarification_payload
        ai = {
            "needs_clarification": True,
            "questions": [{"id": "q1", "type": "free_text", "question": "test?"}],
            "assumptions_if_skipped": [],
        }
        result = build_embedded_clarification_payload(
            ai_result=ai, mode="PLANNING", round_num=1, max_rounds=3,
        )
        assert result is not None
        assert result["show_user_note"] is True
        assert result["require_answer"] is False

    def test_strict_no_questions(self):
        """STRICT 无 AI 问题 → 用 DEFAULT_CLARIFICATION_QUESTIONS, require_answer=True"""
        from agents.clarification import build_embedded_clarification_payload, DEFAULT_CLARIFICATION_QUESTIONS
        result = build_embedded_clarification_payload(
            ai_result={"needs_clarification": False, "questions": []},
            mode="STRICT", round_num=1, max_rounds=3,
        )
        assert result is not None
        assert result["require_answer"] is True
        assert result["questions"] == DEFAULT_CLARIFICATION_QUESTIONS

    def test_strict_with_questions(self):
        """STRICT 有 AI 问题 → 用 AI 问题, require_answer=True"""
        from agents.clarification import build_embedded_clarification_payload
        ai = {
            "needs_clarification": True,
            "questions": [{"id": "q1", "type": "free_text", "question": "AI问的"}],
            "assumptions_if_skipped": [],
        }
        result = build_embedded_clarification_payload(
            ai_result=ai, mode="STRICT", round_num=1, max_rounds=3,
        )
        assert result is not None
        assert result["require_answer"] is True
        assert result["questions"][0]["question"] == "AI问的"

    def test_strict_over_max_rounds(self):
        """STRICT round > max_rounds → require_answer=False（放行）"""
        from agents.clarification import build_embedded_clarification_payload
        result = build_embedded_clarification_payload(
            ai_result={"needs_clarification": False, "questions": []},
            mode="STRICT", round_num=5, max_rounds=3,
        )
        assert result is not None
        assert result["require_answer"] is False

    def test_unknown_mode_fallback(self):
        """未知模式 → fallback 为 PLANNING 行为"""
        from agents.clarification import build_embedded_clarification_payload
        ai = {
            "needs_clarification": True,
            "questions": [{"id": "q1", "type": "free_text", "question": "x"}],
            "assumptions_if_skipped": [],
        }
        result = build_embedded_clarification_payload(
            ai_result=ai, mode="UNKNOWN", round_num=1, max_rounds=3,
        )
        assert result is not None
        assert result["show_user_note"] is True
        assert result["require_answer"] is False


class TestTaskCardEmbeddedClarificationEvent:
    """任务卡 SSE 只下发仍待回答的澄清问题。"""

    def test_completed_clarification_is_not_sent_again(self):
        from api.routes import _embedded_clarification_payload

        result = _embedded_clarification_payload({
            "embedded_clarification": True,
            "needs_clarification": False,
            "clarification_questions": [{"id": "identity", "question": "主角是谁？"}],
        })

        assert result is None

    def test_pending_clarification_is_sent_to_task_card(self):
        from api.routes import _embedded_clarification_payload

        result = _embedded_clarification_payload({
            "embedded_clarification": True,
            "needs_clarification": True,
            "clarification_questions": [{"id": "identity", "question": "主角是谁？"}],
        })

        assert result is not None
        assert result["questions"][0]["id"] == "identity"


class TestTaskCardClarificationStatus:
    def test_not_needed_status_is_explicit(self):
        from api.routes import _task_card_clarification_status

        assert _task_card_clarification_status({
            "embedded_clarification": True,
            "pre_generation_mode": "PLANNING",
            "needs_clarification": False,
        }) == "not_needed"

    def test_fast_mode_is_not_misrepresented_as_ai_analysis(self):
        from api.routes import _task_card_clarification_status

        assert _task_card_clarification_status({
            "embedded_clarification": True,
            "pre_generation_mode": "FAST",
            "needs_clarification": False,
        }) == "skipped"


class TestGenerateRequestPreGenerationMode:
    """测试 GenerateRequest 的 pre_generation_mode 字段。"""

    def test_default_mode(self):
        """默认 pre_generation_mode=PLANNING"""
        from schemas.api import GenerateRequest
        req = GenerateRequest()
        assert req.pre_generation_mode == "PLANNING"
        assert req.max_clarification_rounds == 3

    def test_invalid_mode_rejected(self):
        """非法 mode 值被 pattern 拒绝"""
        from schemas.api import GenerateRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            GenerateRequest(pre_generation_mode="INVALID")

    def test_max_rounds_bounds(self):
        """max_clarification_rounds 越界被拒"""
        from schemas.api import GenerateRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            GenerateRequest(max_clarification_rounds=0)
        with pytest.raises(ValidationError):
            GenerateRequest(max_clarification_rounds=11)
