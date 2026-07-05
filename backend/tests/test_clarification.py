"""Phase J-1 测试 — Clarification Loop 基础层

验证：
1. parse_clarification_result 容错解析（纯 JSON / 散文中提取 / 失败 fallback）
2. 问题裁剪到 MAX_QUESTIONS=3
3. 问题缺 id 时自动生成稳定 id
4. needs_clarification=false 时不产生问题
5. 已回答问题不会重复问
6. build_clarification_summary 正确整合
7. clarification-planner skill 可扫描
8. workflow snapshot 可序列化且含 clarification 节点

J-3 验证：
9. GET /ai-runs/{run_id}/clarification
10. POST /ai-runs/{run_id}/clarification-answers (submit / skip)
11. 跨用户 404
12. 重复提交幂等
13. 非 clarification interrupt 不被误查
"""

import asyncio
import json
import uuid

import pytest

from test_smoke import setup_db, client, test_session_factory, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


# ── parse_clarification_result ─────────────────────────


class TestParseClarificationResult:
    def test_parse_pure_json(self):
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.72,
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
        })
        result = parse_clarification_result(raw)
        assert result["parse_error"] is False
        assert result["needs_clarification"] is True
        assert result["confidence"] == 0.72
        assert len(result["questions"]) == 1
        assert result["questions"][0]["id"] == "chapter_goal"
        assert result["questions"][0]["type"] == "single_choice"
        assert len(result["questions"][0]["options"]) == 2
        assert result["missing_fields"] == ["chapter_goal", "pov"]
        assert result["assumptions_if_skipped"] == ["默认采用第三人称有限视角"]

    def test_parse_from_prose(self):
        """LLM 可能把 JSON 嵌在散文中"""
        from agents.clarification import parse_clarification_result

        raw = '好的，我来分析一下。\n\n{"needs_clarification": true, "confidence": 0.5, "missing_fields": ["pov"], "questions": [{"id": "pov", "type": "single_choice", "question": "本章视角？", "options": [{"value": "first", "label": "第一人称"}], "required": true, "reason": "影响叙事口吻"}], "assumptions_if_skipped": [], "clarification_summary": ""}\n\n以上就是我的分析。'
        result = parse_clarification_result(raw)
        assert result["parse_error"] is False
        assert result["needs_clarification"] is True
        assert len(result["questions"]) == 1
        assert result["questions"][0]["id"] == "pov"

    def test_parse_failure_returns_safe_fallback(self):
        """解析失败时返回 needs_clarification=False，不阻断流程"""
        from agents.clarification import parse_clarification_result

        result = parse_clarification_result("这不是JSON")
        assert result["parse_error"] is True
        assert result["needs_clarification"] is False
        assert result["questions"] == []
        assert result["confidence"] == 0.0

    def test_parse_empty_string(self):
        from agents.clarification import parse_clarification_result

        result = parse_clarification_result("")
        assert result["parse_error"] is True
        assert result["needs_clarification"] is False


class TestQuestionCoercion:
    def test_questions_truncated_to_max_3(self):
        """问题超过 3 个时裁剪"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.3,
            "questions": [
                {"id": f"q{i}", "type": "free_text", "question": f"问题{i}"}
                for i in range(5)
            ],
        })
        result = parse_clarification_result(raw)
        assert len(result["questions"]) == 3

    def test_question_missing_id_gets_auto_id(self):
        """问题缺 id 时自动生成稳定 id"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.5,
            "questions": [
                {"type": "free_text", "question": "没有 id 的问题"},
            ],
        })
        result = parse_clarification_result(raw)
        assert len(result["questions"]) == 1
        assert result["questions"][0]["id"] == "question_1"

    def test_duplicate_ids_deduplicated(self):
        """同 id 的问题只保留第一个"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.5,
            "questions": [
                {"id": "dup", "type": "free_text", "question": "第一个"},
                {"id": "dup", "type": "free_text", "question": "第二个"},
            ],
        })
        result = parse_clarification_result(raw)
        assert len(result["questions"]) == 1
        assert result["questions"][0]["question"] == "第一个"

    def test_invalid_question_type_falls_back_to_free_text(self):
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.5,
            "questions": [
                {"id": "q1", "type": "invalid_type", "question": "测试"},
            ],
        })
        result = parse_clarification_result(raw)
        assert result["questions"][0]["type"] == "free_text"

    def test_question_without_text_is_dropped(self):
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.5,
            "questions": [
                {"id": "q1", "type": "free_text", "question": ""},
                {"id": "q2", "type": "free_text", "question": "有效问题"},
            ],
        })
        result = parse_clarification_result(raw)
        assert len(result["questions"]) == 1
        assert result["questions"][0]["id"] == "q2"

    def test_free_text_strips_options(self):
        """free_text / number 类型不需要 options"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": True,
            "confidence": 0.5,
            "questions": [
                {"id": "q1", "type": "free_text", "question": "自由输入", "options": [{"value": "a", "label": "A"}]},
            ],
        })
        result = parse_clarification_result(raw)
        assert result["questions"][0]["options"] == []


class TestNeedsClarificationCoercion:
    def test_needs_true_but_no_questions_becomes_false(self):
        """needs_clarification=True 但没有问题 → 强制 False"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({"needs_clarification": True, "confidence": 0.9, "questions": []})
        result = parse_clarification_result(raw)
        assert result["needs_clarification"] is False

    def test_needs_false_but_has_questions_becomes_true(self):
        """有问题但 needs_clarification=False → 强制 True"""
        from agents.clarification import parse_clarification_result

        raw = json.dumps({
            "needs_clarification": False,
            "confidence": 0.9,
            "questions": [{"id": "q1", "type": "free_text", "question": "测试"}],
        })
        result = parse_clarification_result(raw)
        assert result["needs_clarification"] is True


class TestFilterAnsweredQuestions:
    def test_answered_questions_filtered(self):
        from agents.clarification import filter_answered_questions

        questions = [
            {"id": "q1", "question": "问题1"},
            {"id": "q2", "question": "问题2"},
            {"id": "q3", "question": "问题3"},
        ]
        result = filter_answered_questions(questions, {"q1"})
        assert len(result) == 2
        assert result[0]["id"] == "q2"

    def test_all_answered_returns_empty(self):
        from agents.clarification import filter_answered_questions

        questions = [{"id": "q1", "question": "问题1"}]
        result = filter_answered_questions(questions, {"q1"})
        assert result == []


class TestBuildClarificationSummary:
    def test_summary_with_answers(self):
        from agents.clarification import build_clarification_summary

        questions = [
            {"id": "pov", "question": "本章视角？"},
            {"id": "goal", "question": "本章目标？"},
        ]
        answers = {"pov": "第三人称有限", "goal": "触发冲突"}
        summary = build_clarification_summary(questions, answers)
        assert "本章视角？" in summary
        assert "第三人称有限" in summary
        assert "触发冲突" in summary

    def test_summary_empty_when_no_answers(self):
        from agents.clarification import build_clarification_summary

        questions = [{"id": "q1", "question": "问题1"}]
        summary = build_clarification_summary(questions, {})
        assert summary == ""


# ── Skill 扫描 ─────────────────────────────────────────


class TestClarificationSkill:
    def test_skill_scannable(self):
        from skills.registry import scan_skills

        skills = scan_skills()
        assert "clarification-planner" in skills
        info = skills["clarification-planner"]
        assert info.name
        assert info.description


# ── Workflow Snapshot ──────────────────────────────────


class TestWorkflowSnapshotWithClarification:
    def test_generate_chapter_standard_has_clarification_nodes(self):
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        node_keys = [s.node_key for s in wf.steps]
        assert "clarification_planner" in node_keys
        assert "human_clarification" in node_keys
        # 原有节点仍在
        assert "chapter_architect" in node_keys
        assert "chapter_writer" in node_keys
        assert "final_review" in node_keys

    def test_clarification_planner_is_conditional(self):
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        planner = next(s for s in wf.steps if s.node_key == "clarification_planner")
        assert planner.is_conditional is True
        assert planner.routes is not None
        assert planner.routes.get("needs_clarification") == "human_clarification"
        assert planner.routes.get("ready") == "chapter_architect"

    def test_human_clarification_is_checkpoint(self):
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        hc = next(s for s in wf.steps if s.node_key == "human_clarification")
        assert hc.is_checkpoint is True
        assert hc.checkpoint_name == "clarification_review"

    def test_workflow_snapshot_serializable_with_clarification(self):
        """workflow snapshot 可 JSON 序列化，含 conditional/routes 字段"""
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        snapshot = wf.to_dict()
        # 可序列化
        json.dumps(snapshot)
        assert snapshot["version"] == "v2.1"
        # 找到 clarification_planner step
        planner_step = next(s for s in snapshot["steps"] if s["node_key"] == "clarification_planner")
        assert planner_step["is_conditional"] is True
        assert planner_step["routes"] is not None
        # 找到 human_clarification step
        hc_step = next(s for s in snapshot["steps"] if s["node_key"] == "human_clarification")
        assert hc_step["is_checkpoint"] is True

    def test_step_count_updated(self):
        """v2.1 有 10 步（含 clarification 2 步）"""
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        assert len(wf.steps) == 10


# ── Phase J-2: Planner Agent ──────────────────────────


class TestBuildClarificationPrompt:
    def test_prompt_contains_context_and_chapter_info(self):
        from agents.clarification import build_clarification_prompt

        prompt = build_clarification_prompt(
            context="## 本章大纲\n第2章 天澜魔法高中",
            chapter_num=2,
            target_words=2000,
            user_note="",
            previous_answers={},
            round_num=1,
            max_rounds=3,
        )
        assert "## 上下文" in prompt
        assert "天澜魔法高中" in prompt
        assert "第2章" in prompt
        assert "2000" in prompt

    def test_prompt_contains_user_note(self):
        from agents.clarification import build_clarification_prompt

        prompt = build_clarification_prompt(
            context="上下文",
            chapter_num=1,
            target_words=1000,
            user_note="注意角色心理变化",
            previous_answers={},
            round_num=1,
            max_rounds=3,
        )
        assert "用户补充要求" in prompt
        assert "注意角色心理变化" in prompt

    def test_prompt_contains_previous_answers(self):
        from agents.clarification import build_clarification_prompt

        prompt = build_clarification_prompt(
            context="上下文",
            chapter_num=1,
            target_words=1000,
            user_note="",
            previous_answers={"pov": "第三人称有限", "goal": "触发冲突"},
            round_num=2,
            max_rounds=3,
        )
        assert "已有澄清回答" in prompt
        assert "pov: 第三人称有限" in prompt
        assert "goal: 触发冲突" in prompt
        assert "不要重复" in prompt

    def test_prompt_contains_round_info_first_round(self):
        from agents.clarification import build_clarification_prompt

        prompt = build_clarification_prompt(
            context="上下文",
            chapter_num=1,
            target_words=1000,
            user_note="",
            previous_answers={},
            round_num=1,
            max_rounds=3,
        )
        assert "第1轮" in prompt
        assert "最多3轮" in prompt


class TestRunClarificationPlanner:
    """run_clarification_planner 集成测试（使用 mock LLM）"""

    def test_llm_success_returns_clarification_result(self):
        """mock LLM 返回 ClarificationResult JSON，parser 正确解析"""
        import asyncio
        from agents.clarification import run_clarification_planner

        result = asyncio.new_event_loop().run_until_complete(
            run_clarification_planner(
                llm_config=None,
                context="## 本章大纲\n第1章 测试章节",
                chapter_num=1,
                target_words=2000,
            )
        )
        assert result["parse_error"] is False
        assert result["needs_clarification"] is True
        assert len(result["questions"]) == 1
        assert result["questions"][0]["id"] == "chapter_goal"
        assert result["confidence"] == 0.6

    def test_llm_no_harness_run_id_still_works(self):
        """不传 harness_run_id 时仍能正常工作（不记日志）"""
        import asyncio
        from agents.clarification import run_clarification_planner

        result = asyncio.new_event_loop().run_until_complete(
            run_clarification_planner(
                llm_config=None,
                context="上下文",
                chapter_num=1,
                target_words=1000,
                harness_run_id="",
            )
        )
        # mock provider 会返回 clarification result
        assert result["parse_error"] is False

    def test_answered_ids_filter_questions(self):
        """previous_answers 中的问题不会重复出现"""
        import asyncio
        from agents.clarification import run_clarification_planner

        result = asyncio.new_event_loop().run_until_complete(
            run_clarification_planner(
                llm_config=None,
                context="上下文",
                chapter_num=1,
                target_words=1000,
                previous_answers={"chapter_goal": "触发冲突"},
            )
        )
        # mock 返回的问题 id 是 chapter_goal，已被回答 → 过滤后无新问题
        assert result["needs_clarification"] is False
        assert len(result["questions"]) == 0

    def test_previous_answers_integrated_into_summary(self):
        """已有回答整合到 clarification_summary"""
        import asyncio
        from agents.clarification import run_clarification_planner

        result = asyncio.new_event_loop().run_until_complete(
            run_clarification_planner(
                llm_config=None,
                context="上下文",
                chapter_num=1,
                target_words=1000,
                previous_answers={"pov": "第三人称有限"},
            )
        )
        assert "pov" in result["clarification_summary"]
        assert "第三人称有限" in result["clarification_summary"]

    def test_llm_exception_fallback(self):
        """LLM 抛异常时 fallback needs_clarification=False"""
        import asyncio
        from agents.clarification import run_clarification_planner
        from agents.llm_provider import MockProvider

        # 用一个会抛异常的 mock provider
        class FailingProvider(MockProvider):
            async def generate(self, *args, **kwargs):
                raise RuntimeError("LLM 不可用")

        # monkey-patch get_llm_provider 返回 FailingProvider
        import agents.llm_provider as llm_mod
        original = llm_mod.get_llm_provider
        llm_mod.get_llm_provider = lambda config: FailingProvider()
        try:
            result = asyncio.new_event_loop().run_until_complete(
                run_clarification_planner(
                    llm_config=None,
                    context="上下文",
                    chapter_num=1,
                    target_words=1000,
                )
            )
        finally:
            llm_mod.get_llm_provider = original

        assert result["needs_clarification"] is False
        assert result["parse_error"] is True
        assert "LLM 不可用" in result.get("raw", "")

    def test_harness_run_id_wraps_logged_llm(self):
        """传 harness_run_id 时 LLM 被 LoggedLLMProvider 包装（不报错即正确）"""
        import asyncio
        from agents.clarification import run_clarification_planner

        result = asyncio.new_event_loop().run_until_complete(
            run_clarification_planner(
                llm_config=None,
                context="上下文",
                chapter_num=1,
                target_words=1000,
                harness_run_id="00000000-0000-0000-0000-000000000000",
                harness_step_id=None,
            )
        )
        # LoggedLLMProvider 会尝试写 LlmCallLog，但 mock 环境下可能静默失败
        # 只要 result 正确返回就说明包装没破坏调用
        assert result is not None


# ── Phase J-3: Clarification Interrupt API ─────────────


def _create_project_and_run(headers):
    """创建项目 + chapter + AiRun，返回 (project_id, run_id)"""
    resp = client.post("/api/projects", json={"title": "J-3 Clarification API 测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    # 通过 full_pipeline generate 创建 AiRun（暂停在 human_review）
    resp_gen = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "full_pipeline",
    }, headers=headers)
    import re
    match = re.search(r'"run_id":\s*"([^"]+)"', resp_gen.text)
    assert match, resp_gen.text
    run_id = match.group(1)
    return project_id, run_id


def _create_clarification_interrupt_directly(run_id, questions=None, round_num=1):
    """直接在 DB 创建 clarification interrupt（绕过 LangGraph，用于 API 测试）"""
    async def _create():
        from models.ai_run import AiRun
        from services.clarification_interrupt import create_clarification_interrupt
        async with test_session_factory() as session:
            run = await session.get(AiRun, uuid.UUID(run_id))
            interrupt = await create_clarification_interrupt(
                session,
                run=run,
                round_num=round_num,
                max_rounds=3,
                questions=questions or [
                    {
                        "id": "chapter_goal",
                        "type": "single_choice",
                        "question": "本章最重要的推进目标是什么？",
                        "options": [
                            {"value": "adapt", "label": "适应新环境"},
                            {"value": "conflict", "label": "触发冲突"},
                        ],
                        "required": True,
                        "reason": "章节目标决定 writer 的事件选择",
                    },
                ],
                assumptions_if_skipped=["默认采用第三人称有限视角"],
                chapter_sequence_number=1,
            )
            await session.commit()
            return str(interrupt.id)

    return asyncio.new_event_loop().run_until_complete(_create())


class TestGetClarification:
    def test_get_returns_questions(self):
        """GET 返回 clarification 问题列表"""
        headers = _auth_headers("j3_get", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        resp = client.get(f"/api/ai-runs/{run_id}/clarification", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "WAITING"
        assert len(data["questions"]) == 1
        assert data["questions"][0]["id"] == "chapter_goal"
        assert data["round"] == 1
        assert data["max_rounds"] == 3
        assert data["resolved"] is False

    def test_get_returns_none_when_no_clarification(self):
        """没有 clarification interrupt 时返回 status=none"""
        headers = _auth_headers("j3_none", "j3pass")
        project_id, run_id = _create_project_and_run(headers)

        resp = client.get(f"/api/ai-runs/{run_id}/clarification", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "none"
        assert data["resolved"] is False

    def test_get_cross_user_404(self):
        """跨用户访问返回 404"""
        headers = _auth_headers("j3_owner", "j3pass")
        other_headers = _auth_headers("j3_other", "j3pass")
        project_id, run_id = _create_project_and_run(headers)

        resp = client.get(f"/api/ai-runs/{run_id}/clarification", headers=other_headers)
        assert resp.status_code == 404

    def test_get_run_not_found(self):
        headers = _auth_headers("j3_404", "j3pass")
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = client.get(f"/api/ai-runs/{fake_id}/clarification", headers=headers)
        assert resp.status_code == 404


class TestSubmitClarificationAnswers:
    def test_submit_answers(self):
        """submit 把 answers 写入 payload 并 resolve"""
        headers = _auth_headers("j3_submit", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        resp = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "submit",
            "answers": {"chapter_goal": "触发冲突"},
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved"] is True
        assert data["status"] == "ANSWERED"
        assert data["existing_answers"]["chapter_goal"] == "触发冲突"

    def test_skip_clarification(self):
        """skip 标记跳过并 resolve"""
        headers = _auth_headers("j3_skip", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        resp = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "skip",
            "answers": {},
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved"] is True
        assert data["status"] == "SKIPPED"

    def test_submit_cross_user_404(self):
        """跨用户提交返回 404"""
        headers = _auth_headers("j3_submit_owner", "j3pass")
        other_headers = _auth_headers("j3_submit_other", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        resp = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "submit",
            "answers": {"chapter_goal": "adapt"},
        }, headers=other_headers)
        assert resp.status_code == 404

    def test_double_submit_idempotent(self):
        """重复提交返回当前状态（不报错）"""
        headers = _auth_headers("j3_idempotent", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        # 第一次提交
        resp1 = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "submit",
            "answers": {"chapter_goal": "触发冲突"},
        }, headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["resolved"] is True

        # 第二次提交（幂等）
        resp2 = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "submit",
            "answers": {"chapter_goal": "适应新环境"},
        }, headers=headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["resolved"] is True
        # answers 不被第二次覆盖（已 resolved 直接返回）
        assert data2["existing_answers"]["chapter_goal"] == "触发冲突"

    def test_submit_no_clarification_interrupt_404(self):
        """没有 clarification interrupt 时 POST 返回 404"""
        headers = _auth_headers("j3_no_interrupt", "j3pass")
        project_id, run_id = _create_project_and_run(headers)

        resp = client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "submit",
            "answers": {"chapter_goal": "adapt"},
        }, headers=headers)
        assert resp.status_code == 404

    def test_skip_then_get_returns_skipped(self):
        """skip 后 GET 返回 SKIPPED 状态"""
        headers = _auth_headers("j3_skip_get", "j3pass")
        project_id, run_id = _create_project_and_run(headers)
        _create_clarification_interrupt_directly(run_id)

        # skip
        client.post(f"/api/ai-runs/{run_id}/clarification-answers", json={
            "action": "skip", "answers": {},
        }, headers=headers)

        # GET
        resp = client.get(f"/api/ai-runs/{run_id}/clarification", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SKIPPED"
        assert data["resolved"] is True


class TestClarificationEnumValues:
    """枚举值正确性"""

    def test_submit_clarification_enum(self):
        from models.harness_enums import InterruptDecision, InterruptStatus

        assert InterruptDecision.SUBMIT_CLARIFICATION == "SUBMIT_CLARIFICATION"
        assert InterruptDecision.SKIP_CLARIFICATION == "SKIP_CLARIFICATION"
        assert InterruptStatus.ANSWERED == "ANSWERED"
        assert InterruptStatus.SKIPPED == "SKIPPED"
