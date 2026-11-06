"""Phase I-3 测试 — Workflow Definition + 规则总编

验证：
1. WORKFLOW_DEFINITIONS 结构完整
2. resolve_workflow 确定性映射
3. workflow 步骤 + checkpoint 完整性
4. expert_snapshot 只含 v2
5. AiRun 写入 workflow 快照
6. create_run 向后兼容
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


# ── WorkflowDefinition 结构 ────────────────────────────


class TestWorkflowDefinitions:
    def test_all_definitions_have_required_fields(self):
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        assert len(WORKFLOW_DEFINITIONS) >= 12
        for key, wf in WORKFLOW_DEFINITIONS.items():
            assert wf.workflow_key, f"{key} 缺少 workflow_key"
            assert wf.version, f"{key} 缺少 version"
            assert wf.task_type, f"{key} 缺少 task_type"
            assert wf.project_mode in ("novel", "article"), f"{key} project_mode 非法"
            # reject 可以是空 steps，其余至少 1 步
            if wf.workflow_key != "reject":
                assert len(wf.steps) >= 1, f"{key} steps 为空"

    def test_generate_chapter_standard_has_10_steps_and_3_checkpoints(self):
        """v2.1: 含 clarification_planner + human_clarification，共 10 步 3 checkpoint"""
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        assert wf.workflow_key == "generate_chapter_standard"
        assert len(wf.steps) == 10
        checkpoints = wf.checkpoints
        assert len(checkpoints) == 3
        checkpoint_names = {c.checkpoint_name for c in checkpoints}
        assert checkpoint_names == {"clarification_review", "planning_review", "final_review"}

    def test_step_expert_keys_exist_in_v2_templates(self):
        """workflow 步骤里的 expert_key 应在 BUILTIN_EXPERTS_V2 或 v2 skill 目录中存在。
        scene-enhancer / memory-curator 在 I-1 有 SKILL.md 但 I-2 未创建 Expert 记录（预留）。
        """
        from services.workflow_definitions import WORKFLOW_DEFINITIONS
        from agents.expert_templates import BUILTIN_EXPERTS_V2
        from skills.registry import scan_skills

        v2_keys = {tpl["expert_key"] for tpl in BUILTIN_EXPERTS_V2}
        v2_skill_dirs = set(scan_skills().keys())
        # I-2 创建的 6 个 + I-1 有 SKILL.md 但未创建 Expert 的 4 个预留
        all_known = v2_keys | v2_skill_dirs
        for key, wf in WORKFLOW_DEFINITIONS.items():
            if wf.project_mode != "novel":
                continue  # article 用 node_key，不走 v2 expert_key
            for step in wf.steps:
                if step.expert_key:
                    assert step.expert_key in all_known, (
                        f"{key} step {step.node_key} 的 expert_key '{step.expert_key}' 不在已知 v2 专家/skill 中"
                    )

    def test_to_dict_serializable(self):
        from services.workflow_definitions import WORKFLOW_DEFINITIONS
        import json

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        d = wf.to_dict()
        # 可 JSON 序列化
        json.dumps(d)
        assert d["workflow_key"] == "generate_chapter_standard"
        assert len(d["steps"]) == 10  # v2.1: 含 clarification 2 步


# ── resolve_workflow 确定性 ───────────────────────────


class TestResolveWorkflow:
    def test_novel_full_pipeline(self):
        from services.novel_orchestrator import resolve_workflow

        wf = resolve_workflow(project_mode="novel", mode="full_pipeline")
        assert wf is not None
        assert wf.workflow_key == "generate_chapter_standard"
        assert wf.version == "v2.1"  # J-1: 含 clarification 节点

    def test_novel_continue(self):
        from services.novel_orchestrator import resolve_workflow

        wf = resolve_workflow(project_mode="novel", mode="continue")
        assert wf is not None
        assert wf.workflow_key == "continue_fast"

    def test_article_enhance(self):
        from services.novel_orchestrator import resolve_workflow

        wf = resolve_workflow(project_mode="article", mode="enhance")
        assert wf is not None
        assert wf.workflow_key == "article_enhance"

    def test_resume_action_as_mode(self):
        """resume 的 action (approve/review/revise/reject) 直接作为 mode 查表"""
        from services.novel_orchestrator import resolve_workflow

        assert resolve_workflow(project_mode="novel", mode="full_pipeline", action="approve").workflow_key == "approve"
        assert resolve_workflow(project_mode="novel", mode="full_pipeline", action="review").workflow_key == "review"
        assert resolve_workflow(project_mode="novel", mode="full_pipeline", action="revise").workflow_key == "rewrite"
        assert resolve_workflow(project_mode="novel", mode="full_pipeline", action="reject").workflow_key == "reject"

    def test_deterministic(self):
        """同一输入始终返回同一结果"""
        from services.novel_orchestrator import resolve_workflow

        wf1 = resolve_workflow(project_mode="novel", mode="full_pipeline")
        wf2 = resolve_workflow(project_mode="novel", mode="full_pipeline")
        assert wf1 is not None
        assert wf1.workflow_key == wf2.workflow_key

    def test_unknown_returns_none(self):
        from services.novel_orchestrator import resolve_workflow

        assert resolve_workflow(project_mode="poetry", mode="full_pipeline") is None


# ── expert_snapshot / workflow_to_snapshot ─────────────


class TestSnapshotHelpers:
    def test_build_expert_snapshot_only_v2(self):
        """build_expert_snapshot 只含 expert_key 非空的 v2 专家"""
        from harness.run_manager import build_expert_snapshot
        from models.expert import Expert

        # 模拟：2 个 v2 + 1 个旧 deprecated
        experts = [
            Expert(
                project_id="00000000-0000-0000-0000-000000000000",
                name="正文写手", role_type="writer", workflow_position="replace_writer",
                expert_key="chapter-writer", version=1, deprecated=False, skill_dir="chapter-writer",
            ),
            Expert(
                project_id="00000000-0000-0000-0000-000000000000",
                name="残酷审稿人", role_type="critic", workflow_position="replace_critic",
                expert_key="structural-critic", version=1, deprecated=False, skill_dir="structural-critic",
            ),
            Expert(
                project_id="00000000-0000-0000-0000-000000000000",
                name="创意大师", role_type="writer", workflow_position="replace_writer",
                expert_key=None, version=1, deprecated=True, skill_dir="creative-master",
            ),
        ]
        snapshot = build_expert_snapshot(experts)
        assert len(snapshot) == 2  # 只含 v2，不含旧 deprecated
        keys = {e["expert_key"] for e in snapshot}
        assert keys == {"chapter-writer", "structural-critic"}
        for e in snapshot:
            assert e["version"] == 1
            assert e["skill_dir"]

    def test_workflow_to_snapshot_none(self):
        from harness.run_manager import workflow_to_snapshot

        assert workflow_to_snapshot(None) is None

    def test_workflow_to_snapshot_dict(self):
        from harness.run_manager import workflow_to_snapshot
        from services.workflow_definitions import WORKFLOW_DEFINITIONS

        wf = WORKFLOW_DEFINITIONS["novel:full_pipeline"]
        d = workflow_to_snapshot(wf)
        assert d["workflow_key"] == "generate_chapter_standard"
        assert "steps" in d


# ── AiRun 写入 workflow 快照（集成） ──────────────────


class TestAiRunWorkflowSnapshot:
    def test_full_pipeline_writes_workflow_to_airun(self):
        """novel full_pipeline generate 创建的 AiRun 应有 workflow 快照"""
        headers = _auth_headers("i3_airun", "i3pass")
        resp = client.post("/api/projects", json={"title": "I-3 AiRun 测试"}, headers=headers)
        project_id = resp.json()["id"]
        client.post(f"/api/projects/{project_id}/chapters", json={
            "title": "第一章", "sequence_number": 1,
        }, headers=headers)

        resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
            "chapter_num": 1, "mode": "full_pipeline",
        }, headers=headers)
        assert resp2.status_code == 200

        # 从 SSE 中提取 run_id
        import re
        match = re.search(r'"run_id":\s*"([^"]+)"', resp2.text)
        assert match, resp2.text
        run_id = match.group(1)

        # 查 AiRun
        async def _check():
            from models.ai_run import AiRun
            async with test_session_factory() as session:
                run = await session.get(AiRun, uuid.UUID(run_id))
                return run

        run = asyncio.new_event_loop().run_until_complete(_check())
        assert run is not None
        assert run.workflow_key == "generate_chapter_standard"
        assert run.workflow_version == "v2.1"
        assert run.workflow_snapshot is not None
        assert run.workflow_snapshot["workflow_key"] == "generate_chapter_standard"
        assert len(run.workflow_snapshot["steps"]) == 10  # v2.1: 含 clarification
        # expert_snapshot 只含 v2 专家
        assert run.expert_snapshot is not None
        assert len(run.expert_snapshot) == 6  # 6 个 v2 专家
        expert_keys = {e["expert_key"] for e in run.expert_snapshot}
        assert "chapter-writer" in expert_keys
        assert "chapter-architect" in expert_keys


# ── create_run 向后兼容 ───────────────────────────────


class TestCreateRunBackwardCompat:
    def test_create_run_without_workflow_params(self):
        """不传 workflow 参数时仍正常，v2 字段为 NULL"""
        async def _run():
            from harness.run_manager import create_run
            from models import Project

            async with test_session_factory() as session:
                # 创建一个项目
                headers = _auth_headers("i3_compat", "i3pass")
                resp = client.post("/api/projects", json={"title": "I-3 兼容测试"}, headers=headers)
                pid = resp.json()["id"]
                proj = await session.get(Project, uuid.UUID(pid))

                run = await create_run(
                    session,
                    project_id=proj.id,
                    run_type="CHAPTER_DRAFT",
                    mode="full_pipeline",
                )
                await session.commit()
                return run

        run = asyncio.new_event_loop().run_until_complete(_run())
        assert run.workflow_key is None
        assert run.workflow_version is None
        assert run.workflow_snapshot is None
        assert run.expert_snapshot is None
