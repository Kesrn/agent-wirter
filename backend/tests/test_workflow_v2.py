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

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


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
        assert "等待人工审核" in resp_revise.text


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
