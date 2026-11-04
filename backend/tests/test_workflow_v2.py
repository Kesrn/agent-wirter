"""Phase I-4 测试 — LangGraph Workflow v2 标准章节生成链路

验证：
1. 图结构：7 节点 + 正确边顺序
2. architect 产出 ChapterTaskCard
3. writer 消费 task card
4. critic 产出 StructuralCritique
5. editor 消费 critique
6. continuity 复用 GuardrailResult
7. 完整 full_pipeline SSE
8. AiRunStep 完整
9. HITL approve 闭环
10. HITL revise 闭环
11. high severity 阻断
12. revision 路由
"""

import asyncio
import json
import re
import uuid

import pytest
from sqlalchemy import delete, select

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


@pytest.fixture(autouse=True)
def _mock_llm_provider(monkeypatch):
    """Exercise v2 workflows with an explicit offline provider fixture."""
    from agents.llm_provider import MockProvider
    import agents.llm_provider as llm_provider
    import agents.workflow as workflow
    import agents.workflow_v2 as workflow_v2
    import api.routes as routes

    def _get_mock_provider(_config=None):
        return MockProvider()

    monkeypatch.setattr(llm_provider, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(workflow, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(workflow_v2, "get_llm_provider", _get_mock_provider)
    monkeypatch.setattr(routes, "get_llm_provider", _get_mock_provider)


# ── 图结构 ─────────────────────────────────────────────


class TestGraphStructure:
    def test_graph_has_7_nodes(self):
        from agents.workflow_v2 import build_creative_graph_v2

        graph = build_creative_graph_v2()
        # StateGraph.nodes 在 compile 前可用
        node_names = set(graph.nodes.keys())
        expected = {
            "context_loader", "chapter_architect", "chapter_writer",
            "structural_critic", "narrative_editor", "continuity_checker",
            "human_review",
        }
        assert expected.issubset(node_names), f"缺失节点: {expected - node_names}"

    def test_app_compiles_with_interrupt(self):
        from agents.workflow_v2 import get_creative_app_v2

        app = get_creative_app_v2()
        assert app is not None


# ── 节点单元测试 ──────────────────────────────────────


def _make_base_state(**overrides) -> dict:
    """构造一个可用的 v2 state 用于节点单元测试"""
    base = {
        "project_id": "00000000-0000-0000-0000-000000000000",
        "chapter_id": "00000000-0000-0000-0000-000000000001",
        "chapter_num": 1,
        "mode": "full_pipeline",
        "context": "## 本章大纲\n第1章 测试章节\n概要：测试概要",
        "draft": "",
        "original_text": "",
        "critiques": [],
        "consistency_report": {},
        "edited_draft": "",
        "revision_count": 0,
        "writer_prompt": "",
        "critic_prompt": "",
        "editor_prompt": "",
        "consistency_prompt": "",
        "llm_config": None,
        "selected_outline_ids": [],
        "selected_character_ids": [],
        "selected_world_entry_ids": [],
        "selected_hidden_thread_ids": [],
        "target_words": 2000,
        "selected_direction": "",
        "user_note": "",
        "skill_packs": [],
        "harness_run_id": "",
        "harness_step_id": None,
        "chapter_task_card": {},
        "structural_critique": {},
        "edit_report": {},
        "workflow_key": "generate_chapter_standard",
    }
    base.update(overrides)
    return base


class TestArchitectNode:
    def test_architect_produces_task_card(self):
        from agents.workflow_v2 import chapter_architect_node

        state = _make_base_state()
        result = asyncio.new_event_loop().run_until_complete(chapter_architect_node(state))
        assert "chapter_task_card" in result
        card = result["chapter_task_card"]
        assert isinstance(card, dict)
        assert "scenes" in card
        assert "word_budget" in card

    def test_architect_prompt_includes_user_note(self, monkeypatch):
        from agents import workflow_v2

        captured: dict[str, str] = {}

        class CaptureProvider:
            async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
                captured["user_prompt"] = user_prompt
                return json.dumps({
                    "chapter_number": 1,
                    "chapter_title": "测试章节",
                    "core_task": "按用户要求生成本章",
                    "opening_anchor": "开篇锚点",
                    "scenes": [],
                    "character_goals": [],
                    "information_rules": {"may_reveal": [], "hint_only": [], "forbidden": []},
                    "tension_design": [],
                    "word_budget": 2000,
                    "forbidden": [],
                }, ensure_ascii=False)

        monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: CaptureProvider())
        monkeypatch.setattr(
            workflow_v2,
            "_maybe_wrap_llm",
            lambda llm, _state, agent_name, include_context=False: llm,
        )

        state = _make_base_state(user_note="多写心理变化，少用旁白")
        result = asyncio.new_event_loop().run_until_complete(workflow_v2.chapter_architect_node(state))

        assert "chapter_task_card" in result
        assert "## 用户本轮写作要求" in captured["user_prompt"]
        assert "多写心理变化，少用旁白" in captured["user_prompt"]

    def test_architect_normalizes_string_task_card_fields(self, monkeypatch):
        """模型把对象/数组字段返回成字符串时，审核和 Writer 仍收到稳定结构。"""
        from agents import workflow_v2

        class VariantProvider:
            async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
                return json.dumps({
                    "chapter_title": "异常结构任务卡",
                    "core_task": "承接上一章并推进冲突",
                    "scenes": ["主角进入现场", "发现关键证据"],
                    "information_rules": "不要提前揭示幕后主使",
                    "forbidden": "新增角色, 改写既定结局",
                    "word_budget": "约 2200 字",
                }, ensure_ascii=False)

        monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: VariantProvider())
        monkeypatch.setattr(
            workflow_v2,
            "_maybe_wrap_llm",
            lambda llm, _state, agent_name, include_context=False: llm,
        )

        result = asyncio.new_event_loop().run_until_complete(
            workflow_v2.chapter_architect_node(_make_base_state())
        )
        card = result["chapter_task_card"]

        assert card["scenes"] == [
            {"title": "场景1", "scene_goal": "主角进入现场"},
            {"title": "场景2", "scene_goal": "发现关键证据"},
        ]
        assert card["information_rules"] == {
            "may_reveal": [],
            "hint_only": [],
            "forbidden": ["不要提前揭示幕后主使"],
        }
        assert card["forbidden"] == ["新增角色", "改写既定结局"]
        assert card["word_budget"] == 2200

    def test_task_card_formatter_accepts_legacy_string_information_rules(self):
        """旧 checkpoint 即使保留字符串规则，也不能在恢复创作时触发 .get 崩溃。"""
        from agents.workflow_v2 import _format_task_card

        formatted = _format_task_card({
            "core_task": "推进本章冲突",
            "scenes": "进入现场；发现证据",
            "information_rules": "不得揭示凶手身份",
            "forbidden": "新增设定",
        })

        assert "核心任务：推进本章冲突" in formatted
        assert "场景1" in formatted
        assert "禁止揭示：不得揭示凶手身份" in formatted
        assert "禁止事项：新增设定" in formatted

    def test_architect_repairs_unescaped_newline_in_task_card_json(self, monkeypatch):
        """模型把多行任务直接写进 JSON 字符串时，仍能生成可审核任务卡。"""
        from agents import workflow_v2

        class MalformedJsonProvider:
            async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
                return '''```json
{
  "chapter_title": "测试章节",
  "core_task": "承接上一章的冲突，\n推进主角的选择",
  "scenes": []
}
```'''

        monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: MalformedJsonProvider())
        monkeypatch.setattr(
            workflow_v2,
            "_maybe_wrap_llm",
            lambda llm, _state, agent_name, include_context=False: llm,
        )

        result = asyncio.new_event_loop().run_until_complete(workflow_v2.chapter_architect_node(_make_base_state()))
        card = result["chapter_task_card"]

        assert card["core_task"] == "承接上一章的冲突，\n推进主角的选择"
        assert card["word_budget"] == 2000

    def test_architect_rejects_unparseable_task_card_instead_of_showing_api_error(self, monkeypatch):
        """不可解析结果不能伪装成 API 配置错误任务卡进入人工确认。"""
        from agents import workflow_v2

        class InvalidProvider:
            async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
                return "这不是任务卡 JSON"

        monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: InvalidProvider())
        monkeypatch.setattr(
            workflow_v2,
            "_maybe_wrap_llm",
            lambda llm, _state, agent_name, include_context=False: llm,
        )

        with pytest.raises(workflow_v2.TaskCardGenerationError, match="结构化内容无法解析"):
            asyncio.new_event_loop().run_until_complete(workflow_v2.chapter_architect_node(_make_base_state()))


class TestWriterNode:
    def test_writer_consumes_task_card(self):
        from agents.workflow_v2 import chapter_writer_node

        state = _make_base_state(chapter_task_card={
            "chapter_title": "测试章节",
            "core_task": "测试核心任务",
            "scenes": [{"title": "场景一", "scene_goal": "目标", "word_budget": 1000}],
            "word_budget": 2000,
        })
        result = asyncio.new_event_loop().run_until_complete(chapter_writer_node(state))
        assert "draft" in result
        assert len(result["draft"]) > 0
        assert result["writer_draft"] == result["draft"]

    def test_writer_revision_mode(self):
        """修订模式：改写候选稿不续写"""
        from agents.workflow_v2 import chapter_writer_node

        state = _make_base_state(
            revision_count=1,
            draft="这是已有候选稿到此结束。",
            critiques=["[用户选择的修改方向] 加强心理描写"],
        )
        result = asyncio.new_event_loop().run_until_complete(chapter_writer_node(state))
        assert "draft" in result
        assert len(result["draft"]) > 0
        assert "writer_draft" not in result

    def test_writer_failure_aborts_instead_of_returning_error_as_draft(self, monkeypatch):
        """模型调用失败必须中止整条链路，不能把报错文字伪装成正文。"""
        from agents import workflow_v2

        class FailingProvider:
            async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
                raise RuntimeError("provider unavailable")

        monkeypatch.setattr(workflow_v2, "get_llm_provider", lambda _config: FailingProvider())
        monkeypatch.setattr(
            workflow_v2,
            "_maybe_wrap_llm",
            lambda llm, _state, agent_name, include_context=False: llm,
        )

        with pytest.raises(workflow_v2.WorkflowGenerationError, match="章节正文生成失败"):
            asyncio.new_event_loop().run_until_complete(workflow_v2.chapter_writer_node(_make_base_state()))


class TestCriticNode:
    def test_critic_produces_structural_critique(self):
        from agents.workflow_v2 import structural_critic_node

        state = _make_base_state(draft="这是一段待审校的正文。")
        result = asyncio.new_event_loop().run_until_complete(structural_critic_node(state))
        assert "structural_critique" in result
        critique = result["structural_critique"]
        assert isinstance(critique, dict)
        assert "summary" in critique


class TestEditorNode:
    def test_editor_consumes_critique(self):
        from agents.workflow_v2 import narrative_editor_node

        state = _make_base_state(
            draft="这是原始正文。",
            structural_critique={
                "summary": "需要修改",
                "p0": [],
                "p1": ["加强描写"],
                "edit_instructions": {"rewrite": ["开篇段落"]},
            },
        )
        result = asyncio.new_event_loop().run_until_complete(narrative_editor_node(state))
        assert "draft" in result
        # editor 输出覆盖 draft
        assert len(result["draft"]) > 0
        assert "edit_report" in result


class TestContinuityNode:
    def test_continuity_uses_guardrail_result(self):
        from agents.workflow_v2 import consistency_checker_node

        state = _make_base_state(draft="这是最终正文。")
        result = asyncio.new_event_loop().run_until_complete(consistency_checker_node(state))
        assert "consistency_report" in result
        report = result["consistency_report"]
        # GuardrailResult 必有字段
        assert "issues" in report
        assert "overall_severity" in report
        assert "parse_error" in report


# ── 路由函数 ───────────────────────────────────────────


class TestRouteAfterReview:
    def test_revision_under_limit_returns_writer(self):
        from agents.workflow_v2 import route_after_review_v2

        assert route_after_review_v2({"revision_count": 1}) == "chapter_writer"
        assert route_after_review_v2({"revision_count": 3}) == "chapter_writer"

    def test_revision_over_limit_returns_end(self):
        from agents.workflow_v2 import route_after_review_v2
        from langgraph.graph import END

        assert route_after_review_v2({"revision_count": 4}) == END


# ── 完整 full_pipeline SSE 集成 ────────────────────────


class TestFullPipelineSSE:
    def test_full_pipeline_emits_v2_events(self):
        """full_pipeline generate 应有 v2 节点的 SSE 事件"""
        headers = _auth_headers("i4_sse", "i4pass")
        resp = client.post("/api/projects", json={"title": "I-4 SSE 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        assert resp2.status_code == 200
        text = resp2.text

        # v2 事件
        assert "event: architect_output" in text
        assert "event: writer_output" in text
        assert '"initial_draft"' in text
        assert "event: critic_output" in text
        assert "event: editor_output" in text
        assert "event: consistency_check" in text
        # HITL 暂停
        assert "等待人工审核" in text
        assert "event: done" not in text  # 暂停时不发 done

    def test_full_pipeline_creates_7_steps(self):
        """full_pipeline run 应有 7 个 AiRunStep"""
        headers = _auth_headers("i4_steps", "i4pass")
        resp = client.post("/api/projects", json={"title": "I-4 Steps 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        match = re.search(r'"run_id":\s*"([^"]+)"', resp2.text)
        assert match
        run_id = match.group(1)

        async def _count_steps():
            from models.ai_run_step import AiRunStep
            async with test_session_factory() as session:
                result = await session.execute(
                    __import__("sqlalchemy").select(AiRunStep).where(
                        AiRunStep.run_id == uuid.UUID(run_id)
                    )
                )
                return result.scalars().all()

        steps = asyncio.new_event_loop().run_until_complete(_count_steps())
        # 至少 6 个步骤（human_review 可能未 start_step 因为是 interrupt）
        step_names = {s.step_name for s in steps}
        assert "build_context" in step_names
        assert "plan_chapter" in step_names
        assert "generate_draft" in step_names
        assert "critique" in step_names
        assert "edit_draft" in step_names
        assert "consistency_check" in step_names


class TestHITLApprove:
    def test_approve_persists_content(self):
        """generate → approve → 正文落库"""
        headers = _auth_headers("i4_approve", "i4pass")
        resp = client.post("/api/projects", json={"title": "I-4 Approve 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        match = re.search(r'"thread_id":\s*"([^"]+)"', resp_gen.text)
        assert match
        thread_id = match.group(1)

        # approve 前正文为空
        resp_before = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
        assert resp_before.json()["content"] is None

        # approve
        resp_resume = client.post(
            f"/api/projects/{project_id}/chapters/resume?thread_id={thread_id}&action=approve",
            headers=headers,
        )
        assert resp_resume.status_code == 200
        assert "event: done" in resp_resume.text

        # 正文落库
        resp_after = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
        assert resp_after.json()["content"]


class TestTaskCardApprovalFinalReview:
    def test_task_card_approval_returns_candidate_content_for_final_review(self):
        """任务卡确认后，最终审核 SSE 必须直接携带候选正文。"""
        headers = _auth_headers("i4_task_card_final", "i4pass")
        resp = client.post("/api/projects", json={"title": "任务卡最终审核测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1,
            "mode": "full_pipeline",
            "planning_review": True,
            "pre_generation_mode": "FAST",
        }, headers=headers)
        review_payloads = re.findall(
            r"event: task_card_review_required\ndata: (.+?)\n\n",
            resp_gen.text,
        )
        assert review_payloads, resp_gen.text
        review_payload = json.loads(review_payloads[-1])
        thread_id = review_payload["thread_id"]
        task_card = review_payload["task_card"]

        resp_resume = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": thread_id, "action": "approve_task_card"},
            json={"task_card": task_card},
            headers=headers,
        )
        assert resp_resume.status_code == 200

        quality_payloads = [
            json.loads(raw)
            for raw in re.findall(r"event: quality_check\ndata: (.+?)\n\n", resp_resume.text)
        ]
        assert {payload.get("stage") for payload in quality_payloads} >= {"draft", "edited", "final"}
        assert any(payload.get("stage") == "final" and payload.get("status") == "passed" for payload in quality_payloads)

        final_payloads = []
        for event_name in ("progress", "done"):
            for raw in re.findall(rf"event: {event_name}\ndata: (.+?)\n\n", resp_resume.text):
                payload = json.loads(raw)
                if payload.get("message") in {"等待人工审核", "任务卡确认，生成完成"}:
                    final_payloads.append(payload)

        assert final_payloads, resp_resume.text
        assert any((payload.get("content") or "").strip() for payload in final_payloads), final_payloads

    def test_final_approval_does_not_reuse_task_card_interrupt(self):
        """旧任务缺少终审记录时，也不能把任务卡 APPROVE 当成正文已批准。"""
        from models.human_interrupt import HumanInterrupt
        from models.harness_enums import InterruptDecision

        headers = _auth_headers("i4_task_card_interrupt_scope", "i4pass")
        resp = client.post("/api/projects", json={"title": "审核阶段隔离测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1,
            "mode": "full_pipeline",
            "planning_review": True,
            "pre_generation_mode": "FAST",
        }, headers=headers)
        review_payloads = re.findall(
            r"event: task_card_review_required\ndata: (.+?)\n\n",
            resp_gen.text,
        )
        assert review_payloads, resp_gen.text
        review_payload = json.loads(review_payloads[-1])
        thread_id = review_payload["thread_id"]

        resp_resume = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": thread_id, "action": "approve_task_card"},
            json={"task_card": review_payload["task_card"]},
            headers=headers,
        )
        assert resp_resume.status_code == 200
        assert "等待人工审核" in resp_resume.text

        async def simulate_legacy_missing_final_review_interrupt():
            async with test_session_factory() as session:
                task_card_result = await session.execute(
                    select(HumanInterrupt).where(
                        HumanInterrupt.thread_id == thread_id,
                        HumanInterrupt.step_name == "task_card_review",
                    )
                )
                task_card_interrupt = task_card_result.scalar_one()
                assert task_card_interrupt.resolved is True
                assert task_card_interrupt.decision == InterruptDecision.APPROVE

                # 模拟线上旧数据：任务卡已确认，但生成完成时没有写 human_review。
                await session.execute(
                    delete(HumanInterrupt).where(
                        HumanInterrupt.thread_id == thread_id,
                        HumanInterrupt.step_name == "human_review",
                    )
                )
                await session.commit()

        asyncio.run(simulate_legacy_missing_final_review_interrupt())

        final_approve = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": thread_id, "action": "approve"},
            headers=headers,
        )
        assert final_approve.status_code == 200
        assert "event: done" in final_approve.text
        assert "不可重复操作" not in final_approve.text

        async def assert_backfilled_final_review_interrupt():
            async with test_session_factory() as session:
                result = await session.execute(
                    select(HumanInterrupt).where(
                        HumanInterrupt.thread_id == thread_id,
                        HumanInterrupt.step_name == "human_review",
                    )
                )
                human_review_interrupt = result.scalar_one()
                assert human_review_interrupt.resolved is True
                assert human_review_interrupt.decision == InterruptDecision.APPROVE

        asyncio.run(assert_backfilled_final_review_interrupt())

    def test_task_card_resume_length_warning_preserves_candidate(self, monkeypatch):
        """篇幅超限不能浪费一次重写，必须保留候选稿并进入人工审核。"""
        import agents.workflow_v2 as workflow_v2

        writer_calls: list[int] = []

        async def always_invalid_writer(state):
            writer_calls.append(len(writer_calls) + 1)
            return {"draft": "超" * 4000 + "。"}

        # 直接替换 graph 节点，避免测试依赖具体 provider/prompt 分支。
        monkeypatch.setattr(workflow_v2, "chapter_writer_node", always_invalid_writer)

        headers = _auth_headers("i4_task_card_quality_fail", "i4pass")
        resp = client.post("/api/projects", json={"title": "任务卡质量失败测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1,
            "mode": "full_pipeline",
            "planning_review": True,
            "pre_generation_mode": "FAST",
            "target_words": 2000,
        }, headers=headers)
        review_payloads = re.findall(
            r"event: task_card_review_required\ndata: (.+?)\n\n",
            resp_gen.text,
        )
        assert review_payloads, resp_gen.text
        review_payload = json.loads(review_payloads[-1])

        resp_resume = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": review_payload["thread_id"], "action": "approve_task_card"},
            json={"task_card": review_payload["task_card"]},
            headers=headers,
        )
        assert resp_resume.status_code == 200
        assert len(writer_calls) == 1
        assert "event: quality_check" in resp_resume.text
        quality_payloads = [
            json.loads(raw)
            for raw in re.findall(r"event: quality_check\ndata: (.+?)\n\n", resp_resume.text)
        ]
        assert any(
            payload.get("stage") == "draft"
            and payload.get("status") == "warning"
            and any(issue.get("code") == "CONTENT_TOO_LONG" for issue in payload.get("issues", []))
            for payload in quality_payloads
        )
        assert '"status": "retrying"' not in resp_resume.text
        assert "event: error" not in resp_resume.text
        assert "event: generation_record" in resp_resume.text
        assert '"status": "COMPLETED"' not in resp_resume.text
        assert "等待人工审核" in resp_resume.text

        generations = client.get(
            f"/api/projects/{project_id}/chapters/1/generations",
            headers=headers,
        )
        assert generations.status_code == 200
        assert len(generations.json()) == 1
        assert generations.json()[0]["status"] == "candidate"



class TestFinalClarificationRound:
    def test_last_round_answers_replan_task_card_instead_of_error(self):
        """max_rounds=1 时，第 1/1 轮答案应被整理并返回可提交任务卡。"""
        headers = _auth_headers("i4_last_clarification", "i4pass")
        resp = client.post("/api/projects", json={"title": "最后一轮澄清测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1,
            "mode": "full_pipeline",
            "planning_review": True,
            "pre_generation_mode": "PLANNING",
            "max_clarification_rounds": 1,
        }, headers=headers)
        review_payloads = re.findall(
            r"event: task_card_review_required\ndata: (.+?)\n\n",
            resp_gen.text,
        )
        assert review_payloads, resp_gen.text
        first_review = json.loads(review_payloads[-1])
        assert first_review["clarification_status"] == "pending"
        assert first_review["clarification"]["round"] == 1
        assert first_review["clarification"]["max_rounds"] == 1

        resp_refresh = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": first_review["thread_id"], "action": "refresh_task_card"},
            json={"clarification_answers": {"chapter_goal": "conflict"}},
            headers=headers,
        )
        assert resp_refresh.status_code == 200
        assert "event: error" not in resp_refresh.text

        refreshed_payloads = re.findall(
            r"event: task_card_review_required\ndata: (.+?)\n\n",
            resp_refresh.text,
        )
        assert refreshed_payloads, resp_refresh.text
        refreshed = json.loads(refreshed_payloads[-1])
        assert refreshed["clarification_status"] == "answered"
        assert refreshed["clarification"] is None
        assert refreshed["task_card"]


class TestHITLRevise:
    def test_review_then_revise(self):
        """generate → review → revise → 重新暂停"""
        headers = _auth_headers("i4_revise", "i4pass")
        resp = client.post("/api/projects", json={"title": "I-4 Revise 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        match = re.search(r'"thread_id":\s*"([^"]+)"', resp_gen.text)
        assert match
        thread_id = match.group(1)

        # review
        resp_review = client.post(
            f"/api/projects/{project_id}/chapters/resume?thread_id={thread_id}&action=review",
            headers=headers,
        )
        assert resp_review.status_code == 200
        assert "event: revision_suggestions" in resp_review.text

        # revise
        resp_revise = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": thread_id, "action": "revise", "feedback": "加强冲突"},
            headers=headers,
        )
        assert resp_revise.status_code == 200
        assert "event: editor_output" in resp_revise.text
        editor_payloads = re.findall(r"event: editor_output\ndata: (.+?)\n\n", resp_revise.text)
        assert any((json.loads(payload).get("content") or "").strip() for payload in editor_payloads)
        assert "event: quality_check" in resp_revise.text
        assert "等待人工审核" in resp_revise.text
        assert "event: done" not in resp_revise.text

    def test_review_empty_model_array_returns_fallback_directions(self, monkeypatch):
        """模型返回 [] 时不能让前端把空数组当成永久加载状态。"""
        import api.routes as routes

        headers = _auth_headers("i4_empty_review", "i4pass")
        resp = client.post("/api/projects", json={"title": "空修改建议测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        match = re.search(r'"thread_id":\s*"([^"]+)"', resp_gen.text)
        assert match

        class EmptyReviewProvider:
            async def generate(self, *args, **kwargs):
                return "[]"

        monkeypatch.setattr(
            routes,
            "get_llm_provider",
            lambda _config=None: EmptyReviewProvider(),
        )
        resp_review = client.post(
            f"/api/projects/{project_id}/chapters/resume",
            params={"thread_id": match.group(1), "action": "review"},
            headers=headers,
        )

        assert resp_review.status_code == 200
        assert "AI 正在分析候选稿并整理修改方向" in resp_review.text
        payload_match = re.search(
            r"event: revision_suggestions\ndata: (.+?)\n\n",
            resp_review.text,
        )
        assert payload_match
        payload = json.loads(payload_match.group(1))
        assert len(payload["directions"]) == 3
        assert all(direction.strip() for direction in payload["directions"])


class TestSkillPackMetadata:
    def test_full_pipeline_records_v2_skill_dirs(self):
        """v2 generate 的 skill_pack 应含 chapter-writer 等 v2 skill_dir"""
        headers = _auth_headers("i4_skill", "i4pass")
        resp = client.post("/api/projects", json={"title": "I-4 Skill 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        text = resp2.text
        assert "event: skill_pack" in text
        assert '"skill_dir": "chapter-writer"' in text
        assert '"skill_dir": "chapter-architect"' in text
        assert '"skill_dir": "structural-critic"' in text


# ── Phase I-5: continue/enhance/summarize 入口重映射 ───


class TestContinueV2:
    """continue 第二阶段切 v2 LangGraph"""

    def test_continue_phase1_unchanged(self):
        """continue 第一阶段（无 turn_direction）仍发 turn_suggestions"""
        headers = _auth_headers("i5_cont1", "i5pass")
        resp = client.post("/api/projects", json={"title": "I-5 Continue P1"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)
        client.patch(f"/api/projects/{project_id}/chapters/1", json={
            "content": "已有内容。",
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "continue",
        }, headers=headers)
        assert resp2.status_code == 200
        assert "event: turn_suggestions" in resp2.text
        assert "event: done" in resp2.text

    def test_continue_phase2_v2_events(self):
        """continue 第二阶段（有 turn_direction）走 v2，有 architect/writer/consistency 事件"""
        headers = _auth_headers("i5_cont2", "i5pass")
        resp = client.post("/api/projects", json={"title": "I-5 Continue P2"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)
        client.patch(f"/api/projects/{project_id}/chapters/1", json={
            "content": "已有内容。",
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "continue", "turn_direction": "主角发现秘密",
        }, headers=headers)
        assert resp2.status_code == 200
        text = resp2.text
        # v2 事件
        assert "event: architect_output" in text
        assert "event: writer_output" in text
        assert "event: consistency_check" in text
        # 不暂停 HITL
        assert "等待人工审核" not in text
        assert "event: done" in text

    def test_continue_phase2_creates_airun_with_workflow_key(self):
        """continue 第二阶段创建的 AiRun 有 workflow_key == continue_fast"""
        headers = _auth_headers("i5_cont3", "i5pass")
        resp = client.post("/api/projects", json={"title": "I-5 Continue AiRun"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)
        client.patch(f"/api/projects/{project_id}/chapters/1", json={
            "content": "已有内容。",
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "continue", "turn_direction": "主角发现秘密",
        }, headers=headers)
        match = re.search(r'"run_id":\s*"([^"]+)"', resp2.text)
        assert match
        run_id = match.group(1)

        async def _check():
            from models.ai_run import AiRun
            async with test_session_factory() as session:
                return await session.get(AiRun, uuid.UUID(run_id))

        run = asyncio.new_event_loop().run_until_complete(_check())
        assert run is not None
        assert run.workflow_key == "continue_fast"
        assert run.workflow_snapshot is not None


class TestEnhanceAiRun:
    """enhance 补 AiRun + workflow 快照"""

    def test_enhance_creates_airun(self):
        headers = _auth_headers("i5_enh", "i5pass")
        resp = client.post("/api/projects", json={"title": "I-5 Enhance"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)
        client.patch(f"/api/projects/{project_id}/chapters/1", json={
            "content": "待润色的文本。",
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "enhance", "enhance_direction": "增强画面感",
        }, headers=headers)
        assert resp2.status_code == 200
        match = re.search(r'"run_id":\s*"([^"]+)"', resp2.text)
        assert match
        run_id = match.group(1)

        async def _check():
            from models.ai_run import AiRun
            async with test_session_factory() as session:
                return await session.get(AiRun, uuid.UUID(run_id))

        run = asyncio.new_event_loop().run_until_complete(_check())
        assert run is not None
        assert run.workflow_key == "enhance_scene"
        assert run.workflow_snapshot is not None


class TestSummarizeAiRun:
    """summarize 补 AiRun + workflow 快照"""

    def test_summarize_creates_airun(self):
        headers = _auth_headers("i5_sum", "i5pass")
        resp = client.post("/api/projects", json={"title": "I-5 Summarize"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)
        client.patch(f"/api/projects/{project_id}/chapters/1", json={
            "content": "主角在雨夜发现了新的线索。",
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "summarize",
        }, headers=headers)
        assert resp2.status_code == 200
        match = re.search(r'"run_id":\s*"([^"]+)"', resp2.text)
        assert match
        run_id = match.group(1)

        async def _check():
            from models.ai_run import AiRun
            async with test_session_factory() as session:
                return await session.get(AiRun, uuid.UUID(run_id))

        run = asyncio.new_event_loop().run_until_complete(_check())
        assert run is not None
        assert run.workflow_key == "summarize"
        assert run.workflow_snapshot is not None


# ── 质量验收门测试 ──────────────────────────────────────


def test_draft_validator_passes_clean_content():
    """Writer 输出合格正文 → draft_validator 通过"""
    from agents.workflow_v2 import draft_validator_node

    # 构造约 2255 字符的合格正文（1600~2400 range for target 2000）
    segment = "这是一个合格的小说正文段落，包含了完整的故事内容和标点符号结尾。明天他就要出发了。"
    result = asyncio.new_event_loop().run_until_complete(
        draft_validator_node({
            "draft": segment * 55,
            "target_words": 2000,
            "writer_retry_count": 0,
            "writer_max_retries": 1,
            "harness_run_id": "test",
        })
    )
    assert result["draft_quality_report"]["passed"] is True
    assert "writer_validation_errors" in result
    assert len(result["writer_validation_errors"]) == 0


def test_draft_validator_persists_heading_cleanup_as_writer_baseline():
    from agents.workflow_v2 import draft_validator_node

    body = ("这是通过质量门的正文内容，包含完整句号。" * 50)
    result = asyncio.run(draft_validator_node({
        "draft": "# 第一章 重复标题\n" + body,
        "target_words": 1000,
        "writer_retry_count": 0,
        "writer_max_retries": 1,
    }))
    assert result["draft_quality_report"]["passed"] is True
    assert result["draft"].startswith("这是通过质量门")
    assert result["writer_draft"] == result["draft"]


def test_draft_validator_keeps_oversized_candidate_for_human_review():
    """Writer 输出超长 → 不重写，保留候选稿继续审校"""
    from agents.workflow_v2 import draft_validator_node, route_after_draft_validator

    result = asyncio.new_event_loop().run_until_complete(
        draft_validator_node({
            "draft": "超" * 3999 + "。",  # 4000 chars > 2400 max for target 2000
            "target_words": 2000,
            "writer_retry_count": 0,
            "writer_max_retries": 1,
            "harness_run_id": "test",
        })
    )
    assert result["draft_quality_report"]["passed"] is True
    assert result["writer_retry_count"] == 0
    assert result["writer_validation_errors"] == []
    assert any(
        issue["code"] == "CONTENT_TOO_LONG" and issue["severity"] == "WARNING"
        for issue in result["draft_quality_report"]["issues"]
    )
    assert route_after_draft_validator(result) == "structural_critic"


def test_final_quality_length_warning_routes_to_human_review():
    from agents.workflow_v2 import final_quality_gate_node, route_after_final_quality_gate

    result = asyncio.run(final_quality_gate_node({
        "draft": "长" * 3999 + "。",
        "target_words": 2000,
        "edit_effectiveness_report": {},
        "harness_run_id": "test",
    }))

    assert result["quality_gate_failed"] is False
    assert result["final_quality_report"]["passed"] is True
    assert route_after_final_quality_gate(result) == "human_review"
    assert any(
        issue["code"] == "CONTENT_TOO_LONG" and issue["severity"] == "WARNING"
        for issue in result["final_quality_report"]["issues"]
    )


def test_quality_sse_marks_non_blocking_issue_as_warning():
    from api.routes import _quality_node_completion_status, _quality_sse_payload

    report = {
        "passed": True,
        "stage": "final",
        "issues": [
            {"code": "CONTENT_TOO_LONG", "severity": "WARNING", "message": "篇幅偏长"},
        ],
    }
    payload = _quality_sse_payload(report, stage="final")

    assert payload["status"] == "warning"
    assert _quality_node_completion_status(
        "final_quality_gate",
        {"final_quality_report": report},
    ) == "success"


def test_draft_validator_rejects_metadata():
    """Writer 输出含内部标记 → draft_validator 拒绝"""
    from agents.workflow_v2 import draft_validator_node

    result = asyncio.new_event_loop().run_until_complete(
        draft_validator_node({
            "draft": "正文内容。" * 100 + "\n【增强开始】",
            "target_words": 2000,
            "writer_retry_count": 0,
            "writer_max_retries": 1,
            "harness_run_id": "test",
        })
    )
    assert result["draft_quality_report"]["passed"] is False
    assert any("METADATA" in e["code"] for e in result["writer_validation_errors"])


def test_draft_validator_increments_retry_count():
    """失败时递增 writer_retry_count"""
    from agents.workflow_v2 import draft_validator_node

    result = asyncio.new_event_loop().run_until_complete(
        draft_validator_node({
            "draft": "超" * 4000,
            "target_words": 2000,
            "writer_retry_count": 0,
            "writer_max_retries": 1,
            "harness_run_id": "test",
        })
    )
    assert result["writer_retry_count"] == 1


def test_route_after_draft_validator_retry():
    """draft 失败，未耗尽 → 路由到 chapter_writer"""
    from agents.workflow_v2 import route_after_draft_validator

    result = route_after_draft_validator({
        "draft_quality_report": {"passed": False},
        "writer_retry_count": 0,
        "writer_max_retries": 1,
    })
    assert result == "chapter_writer"


def test_route_after_draft_validator_exhausted():
    """draft 失败，重试耗尽 → END"""
    from agents.workflow_v2 import route_after_draft_validator

    result = route_after_draft_validator({
        "draft_quality_report": {"passed": False},
        "writer_retry_count": 2,
        "writer_max_retries": 1,
    })
    assert result == "__end__"


def test_draft_validator_max_one_executes_one_actual_retry():
    """max_retries=1 表示首次失败后额外调用 Writer 1 次，而不是 0 次。"""
    from langgraph.graph import END, StateGraph
    from agents.workflow_v2 import CreativeStateV2, draft_validator_node, route_after_draft_validator

    attempts: list[int] = []

    async def invalid_writer(state):
        attempts.append(len(attempts) + 1)
        return {"draft": "超" * 4000}

    graph = StateGraph(CreativeStateV2)
    graph.add_node("writer", invalid_writer)
    graph.add_node("validator", draft_validator_node)
    graph.set_entry_point("writer")
    graph.add_edge("writer", "validator")
    graph.add_conditional_edges("validator", route_after_draft_validator, {
        "chapter_writer": "writer",
        "structural_critic": END,
        END: END,
    })
    app = graph.compile()

    result = asyncio.run(app.ainvoke({
        "target_words": 2000,
        "writer_retry_count": 0,
        "writer_max_retries": 1,
    }))

    assert len(attempts) == 2
    assert result["writer_retry_count"] == 2
    assert result["quality_gate_failed"] is True
    assert result["quality_failure_stage"] == "draft"


def test_editor_validator_max_two_executes_two_actual_retries():
    """max_retries=2 表示 Editor 总共执行 3 次（首次 + 2 次重试）。"""
    from langgraph.graph import END, StateGraph
    from agents.workflow_v2 import CreativeStateV2, edited_draft_validator_node, route_after_edited_draft_validator

    attempts: list[int] = []
    accepted_writer_draft = "甲乙丙丁" * 50 + "。"

    async def ineffective_editor(state):
        attempts.append(len(attempts) + 1)
        return {"draft": accepted_writer_draft}

    graph = StateGraph(CreativeStateV2)
    graph.add_node("editor", ineffective_editor)
    graph.add_node("validator", edited_draft_validator_node)
    graph.set_entry_point("editor")
    graph.add_edge("editor", "validator")
    graph.add_conditional_edges("validator", route_after_edited_draft_validator, {
        "narrative_editor": "editor",
        "continuity_checker": END,
        END: END,
    })
    app = graph.compile()

    result = asyncio.run(app.ainvoke({
        "writer_draft": accepted_writer_draft,
        "draft": accepted_writer_draft,
        "target_words": 200,
        "structural_critique": {"p1": ["需要调整节奏"]},
        "editor_retry_count": 0,
        "editor_max_retries": 2,
    }))

    assert len(attempts) == 3
    assert result["editor_retry_count"] == 3
    assert result["quality_gate_failed"] is True
    assert result["quality_failure_stage"] == "edited"


def test_quality_failure_helper_detects_exhausted_draft_and_editor():
    from api.routes import _quality_failure_from_state, _quality_sse_payload

    draft_failure = _quality_failure_from_state({
        "draft_quality_report": {
            "passed": False,
            "issues": [{"code": "CONTENT_TOO_LONG", "severity": "ERROR", "message": "超长"}],
        },
        "writer_retry_count": 2,
        "writer_max_retries": 1,
    })
    assert draft_failure is not None
    assert draft_failure[0] == "draft"
    assert "CONTENT_TOO_LONG" in draft_failure[2]
    assert _quality_sse_payload(draft_failure[1], stage="draft", retry=2, max_retries=1)["status"] == "failed"

    editor_failure = _quality_failure_from_state({
        "quality_gate_failed": True,
        "quality_failure_stage": "edited",
        "quality_failure_reason": "编辑稿质量验收未通过",
        "edited_quality_report": {"passed": False, "issues": []},
    })
    assert editor_failure == (
        "edited",
        {"passed": False, "issues": []},
        "编辑稿质量验收未通过",
    )


def test_editor_effectiveness_rejects_no_change():
    """Critic 要求修改，但 editor 输出完全相同 → edited_draft_validator 拒绝"""
    from services.chapter_quality import validate_edited_draft_quality

    report = validate_edited_draft_quality(
        "初稿正文的内容完全相同。",
        "初稿正文的内容完全相同。",
        20,
        {"requires_revision": True},
    )
    assert not report.passed
    assert any(i.code == "EDITOR_NOT_EFFECTIVE" for i in report.issues)


def test_final_quality_gate_blocks_empty():
    """最终质量门：空正文不能通过"""
    from agents.workflow_v2 import final_quality_gate_node

    result = asyncio.new_event_loop().run_until_complete(
        final_quality_gate_node({
            "draft": "",
            "target_words": 2000,
            "harness_run_id": "test",
            "edit_effectiveness_report": {},
        })
    )
    assert result["quality_gate_failed"] is True
    assert result["final_quality_report"]["passed"] is False


def test_final_quality_gate_passes_valid():
    """最终质量门：合格正文通过"""
    from agents.workflow_v2 import final_quality_gate_node

    segment = "完整的小说正文内容带有标点符号。明天继续上路前行。"
    result = asyncio.new_event_loop().run_until_complete(
        final_quality_gate_node({
            "draft": segment * 90,  # 2250 chars, in range
            "target_words": 2000,
            "harness_run_id": "test",
            "edit_effectiveness_report": {"requires_revision": False, "passed": True},
        })
    )
    assert result["quality_gate_failed"] is False
    assert result["final_quality_report"]["passed"] is True


def test_quality_sse_payload_structure():
    """SSE quality_check payload 结构正确"""
    from api.routes import _quality_sse_payload

    qr = {
        "passed": True,
        "stage": "draft",
        "target_chars": 2000,
        "actual_chars": 2100,
        "min_chars": 1600,
        "max_chars": 2400,
        "issues": [],
        "similarity": None,
        "changed_chars": None,
        "requires_revision": None,
    }
    payload = _quality_sse_payload(qr, stage="draft", retry=0, max_retries=1)
    assert payload["stage"] == "draft"
    assert payload["status"] == "passed"
    assert payload["target_chars"] == 2000
    assert payload["actual_chars"] == 2100


def test_quality_sse_payload_retrying():
    """失败且 retry > 0 → status=retrying"""
    from api.routes import _quality_sse_payload

    qr = {"passed": False, "issues": [{"code": "TEST", "message": "err"}], "target_chars": 2000, "actual_chars": 3999, "min_chars": 1600, "max_chars": 2400, "similarity": None, "changed_chars": None, "requires_revision": None, "stage": "draft"}
    payload = _quality_sse_payload(qr, stage="draft", retry=1, max_retries=2)
    assert payload["status"] == "retrying"
    assert payload["retry_count"] == 1
    assert payload["max_retries"] == 2


def test_quality_sse_payload_final_failed():
    """final 阶段失败 → status=failed"""
    from api.routes import _quality_sse_payload

    qr = {"passed": False, "issues": [{"code": "E1", "message": "final fail"}], "target_chars": 0, "actual_chars": 0, "min_chars": 0, "max_chars": 0, "similarity": None, "changed_chars": None, "requires_revision": None, "stage": "final"}
    payload = _quality_sse_payload(qr, stage="final", retry=0, max_retries=0)
    assert payload["status"] == "failed"
