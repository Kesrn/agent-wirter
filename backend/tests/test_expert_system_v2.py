"""Expert System v2 — Phase I-1 / I-2 测试

I-1 验证：
1. Expert / AiRun / AiRunStep 模型新增 v2 字段且默认值正确
2. scan_skills() 能扫到 10 个 v2 skill，旧 skill 仍在
3. 新 skill 的 SKILL.md frontmatter 可解析

I-2 验证：
4. 新项目默认创建 v2 + 旧(deprecated) 内置专家
5. sync_v2_experts 幂等补齐
6. ExpertUpdate 可切换 deprecated
"""

import asyncio

import pytest
from sqlalchemy import select

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    """确保 test_smoke 的 setup_db fixture 在当前文件中生效"""
    yield


# ── 模型字段 ──────────────────────────────────────────


class TestExpertV2ModelFields:
    """Expert v2 新增字段与默认值"""

    def test_expert_has_v2_fields_with_defaults(self):
        from models.expert import Expert

        e = Expert(
            project_id="00000000-0000-0000-0000-000000000000",
            name="正文写手",
            role_type="writer",
            workflow_position="replace_writer",
        )
        # 新字段存在（默认值在 flush/INSERT 时由 default 与 server_default 注入，
        # 未落库的内存对象为 None，默认值正确性由 roundtrip 测试验证）
        assert hasattr(e, "expert_key")
        assert hasattr(e, "version")
        assert hasattr(e, "deprecated")
        assert hasattr(e, "input_schema")
        assert hasattr(e, "output_schema")

    def test_expert_v2_fields_roundtrip_via_api(self):
        """通过 API 创建专家时不传 v2 字段，应使用默认值；PATCH 可设置 expert_key"""
        headers = _auth_headers("expert_v2_user", "expert_v2_pass")
        resp = client.post("/api/projects", json={"title": "v2专家测试"}, headers=headers)
        project_id = resp.json()["id"]

        resp2 = client.post(
            f"/api/projects/{project_id}/experts",
            json={
                "name": "正文写手",
                "role_type": "writer",
                "skill_dir": "chapter-writer",
                "system_prompt": "你是正文写手。",
                "workflow_position": "replace_writer",
            },
            headers=headers,
        )
        assert resp2.status_code == 200, resp2.text
        expert = resp2.json()
        assert expert["version"] == 1
        assert expert["deprecated"] is False


class TestAiRunV2Fields:
    def test_ai_run_has_workflow_snapshot_fields(self):
        from models.ai_run import AiRun

        # 字段存在性（不实例化，避免 FK 约束）
        assert hasattr(AiRun, "workflow_key")
        assert hasattr(AiRun, "workflow_version")
        assert hasattr(AiRun, "workflow_snapshot")
        assert hasattr(AiRun, "expert_snapshot")

    def test_ai_run_step_has_expert_snapshot_fields(self):
        from models.ai_run_step import AiRunStep

        assert hasattr(AiRunStep, "node_key")
        assert hasattr(AiRunStep, "expert_key")
        assert hasattr(AiRunStep, "expert_version")
        assert hasattr(AiRunStep, "skill_dir")


class TestAiRunV2FieldsPersist:
    """AiRun v2 字段可写入可读取"""

    def test_ai_run_workflow_snapshot_persists(self):
        from models.ai_run import AiRun
        from models import Project

        headers = _auth_headers("airun_v2_user", "airun_v2_pass")
        resp = client.post("/api/projects", json={"title": "AiRun v2 测试"}, headers=headers)
        project_id = resp.json()["id"]

        async def _run():
            async with test_session_factory() as session:
                proj = (
                    await session.get(Project, __import__("uuid").UUID(project_id))
                )
                run = AiRun(
                    project_id=proj.id,
                    run_type="chapter_generate",
                    mode="full_pipeline",
                    status="CREATED",
                    workflow_key="generate_chapter_standard",
                    workflow_version="v2.0",
                    workflow_snapshot={"steps": [{"node": "chapter_writer"}]},
                    expert_snapshot=[{"expert_key": "chapter-writer", "version": 1}],
                )
                session.add(run)
                await session.commit()
                await session.refresh(run)
                return run

        run = asyncio.new_event_loop().run_until_complete(_run())
        assert run.workflow_key == "generate_chapter_standard"
        assert run.workflow_version == "v2.0"
        assert run.workflow_snapshot["steps"][0]["node"] == "chapter_writer"
        assert run.expert_snapshot[0]["expert_key"] == "chapter-writer"


# ── Skill Registry ────────────────────────────────────


V2_SKILL_DIRS = [
    "novel-orchestrator",
    "chapter-architect",
    "chapter-writer",
    "structural-critic",
    "narrative-editor",
    "continuity-checker",
    "story-recorder",
    "memory-curator",
    "canon-researcher",
    "scene-enhancer",
]

LEGACY_SKILL_DIRS = ["creative-master", "brutal-critic", "professional-editor"]


class TestSkillRegistryV2:
    def test_scan_skills_finds_all_v2_skills(self):
        from skills.registry import scan_skills

        skills = scan_skills()
        for d in V2_SKILL_DIRS:
            assert d in skills, f"v2 skill 缺失: {d}"
            info = skills[d]
            assert info.name, f"{d} 的 frontmatter name 为空"
            assert info.description, f"{d} 的 description 为空"

    def test_legacy_skills_still_present(self):
        """旧 skill 不应被 I-1 影响"""
        from skills.registry import scan_skills

        skills = scan_skills()
        for d in LEGACY_SKILL_DIRS:
            assert d in skills, f"旧 skill 丢失: {d}"

    def test_v2_skill_content_loads_without_frontmatter(self):
        """SKILL.md 正文可加载，且去掉 frontmatter 后非空"""
        from skills.registry import scan_skills, get_skill_prompt_for_info

        skills = scan_skills()
        for d in V2_SKILL_DIRS:
            content = get_skill_prompt_for_info(skills[d])
            assert content, f"{d} 的正文为空"
            assert "## 核心职责" in content, f"{d} 缺少核心职责段"
            assert "## 禁止事项" in content, f"{d} 缺少禁止事项段"
            assert "## 权限边界" in content, f"{d} 缺少权限边界段"


# ── 迁移列存在性（建表后断言） ────────────────────────


class TestMigrationColumns:
    """通过 SQLite 建表后断言 v2 列存在（与 smoke 测试同一建表路径）"""

    def test_v2_columns_exist_after_create_all(self):
        from sqlalchemy import inspect as sa_inspect
        from test_smoke import test_engine

        def _get_cols_sync(conn):
            insp = sa_inspect(conn)

            def cols(table):
                return {c["name"] for c in insp.get_columns(table)}

            return {t: cols(t) for t in ("experts", "ai_runs", "ai_run_steps")}

        async def _inspect():
            async with test_engine.connect() as conn:
                return await conn.run_sync(_get_cols_sync)

        all_cols = asyncio.new_event_loop().run_until_complete(_inspect())

        # experts
        ec = all_cols["experts"]
        assert "expert_key" in ec
        assert "version" in ec
        assert "deprecated" in ec
        assert "input_schema" in ec
        assert "output_schema" in ec

        # ai_runs
        ac = all_cols["ai_runs"]
        assert "workflow_key" in ac
        assert "workflow_version" in ac
        assert "workflow_snapshot" in ac
        assert "expert_snapshot" in ac

        # ai_run_steps
        sc = all_cols["ai_run_steps"]
        assert "node_key" in sc
        assert "expert_key" in sc
        assert "expert_version" in sc
        assert "skill_dir" in sc


# ── Phase I-2: 内置专家模板替换 ────────────────────────


class TestBuiltinExpertTemplates:
    """BUILTIN_EXPERTS_V2 / BUILTIN_EXPERTS / ALL_BUILTIN_EXPERTS 结构"""

    def test_v2_templates_have_expert_key_and_not_deprecated(self):
        from agents.expert_templates import BUILTIN_EXPERTS_V2

        assert len(BUILTIN_EXPERTS_V2) == 6
        v2_keys = set()
        for tpl in BUILTIN_EXPERTS_V2:
            assert tpl["expert_key"], f"v2 模板缺少 expert_key: {tpl['name']}"
            assert tpl["deprecated"] is False
            assert tpl["version"] == 1
            assert tpl["skill_dir"], f"v2 模板缺少 skill_dir: {tpl['name']}"
            v2_keys.add(tpl["expert_key"])
        # 确认 6 个 key 都在
        assert v2_keys == {
            "chapter-architect", "chapter-writer", "structural-critic",
            "narrative-editor", "continuity-checker", "story-recorder",
        }

    def test_legacy_templates_are_deprecated(self):
        from agents.expert_templates import BUILTIN_EXPERTS

        assert len(BUILTIN_EXPERTS) == 6
        for tpl in BUILTIN_EXPERTS:
            assert tpl["deprecated"] is True, f"旧模板未标记 deprecated: {tpl['name']}"
            assert "expert_key" not in tpl or not tpl.get("expert_key")

    def test_all_builtin_experts_is_union(self):
        from agents.expert_templates import ALL_BUILTIN_EXPERTS, BUILTIN_EXPERTS, BUILTIN_EXPERTS_V2

        assert len(ALL_BUILTIN_EXPERTS) == len(BUILTIN_EXPERTS_V2) + len(BUILTIN_EXPERTS) == 12


class TestNewProjectCreatesV2AndLegacyExperts:
    """新项目创建后应有 6 v2 + 6 旧(deprecated) 内置专家"""

    def test_new_project_has_v2_and_legacy_experts(self):
        headers = _auth_headers("i2_newproject", "i2pass")
        resp = client.post("/api/projects", json={"title": "I-2 新项目"}, headers=headers)
        project_id = resp.json()["id"]

        resp2 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
        assert resp2.status_code == 200
        experts = resp2.json()

        builtin = [e for e in experts if e["is_builtin"]]
        assert len(builtin) == 12

        v2 = [e for e in builtin if e["expert_key"]]
        legacy = [e for e in builtin if not e["expert_key"]]
        assert len(v2) == 6
        assert len(legacy) == 6

        # v2 专家 deprecated=false, version=1
        for e in v2:
            assert e["deprecated"] is False
            assert e["version"] == 1

        # 旧专家 deprecated=true
        for e in legacy:
            assert e["deprecated"] is True

        # 旧专家 role_type / skill_dir 保持不变（workflow.py 不断）
        legacy_skill_dirs = {e["skill_dir"] for e in legacy}
        assert "creative-master" in legacy_skill_dirs
        assert "brutal-critic" in legacy_skill_dirs

    def test_import_txt_project_also_creates_v2_and_legacy(self):
        headers = _auth_headers("i2_txtimport", "i2pass")
        content = "导入小说\n\n第1章 开端\n林澈来到旧城。"
        resp = client.post(
            "/api/projects/import-txt",
            headers=headers,
            data={"title": "I-2 导入小说"},
            files={"file": ("demo.txt", content.encode("utf-8"), "text/plain")},
        )
        assert resp.status_code == 200
        project_id = resp.json()["project"]["id"]

        resp2 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
        experts = resp2.json()
        builtin = [e for e in experts if e["is_builtin"]]
        assert len(builtin) == 12
        assert len([e for e in builtin if e["expert_key"]]) == 6


class TestSyncV2Experts:
    """sync_v2_experts 服务幂等性 + 补齐"""

    def test_sync_idempotent_on_new_project(self):
        """新项目已有 v2 专家，sync 应 created=0"""
        headers = _auth_headers("i2_sync_new", "i2pass")
        resp = client.post("/api/projects", json={"title": "I-2 sync 新项目"}, headers=headers)
        project_id = resp.json()["id"]

        resp2 = client.post(f"/api/projects/{project_id}/experts/sync-v2", headers=headers)
        assert resp2.status_code == 200
        result = resp2.json()
        assert result["created"] == 0  # v2 已存在
        assert result["deprecated_marked"] == 0  # 旧专家已 deprecated

    def test_sync_backfills_v2_for_legacy_project(self):
        """模拟老项目（只有旧专家，无 v2），sync 后补齐 v2"""
        import uuid

        headers = _auth_headers("i2_sync_old", "i2pass")
        resp = client.post("/api/projects", json={"title": "I-2 sync 老项目"}, headers=headers)
        project_id = resp.json()["id"]

        # 删除所有 v2 专家，模拟老项目
        async def _delete_v2():
            from models.expert import Expert
            async with test_session_factory() as session:
                experts = (
                    await session.execute(
                        select(Expert).where(
                            Expert.project_id == uuid.UUID(project_id),
                            Expert.expert_key.isnot(None),
                        )
                    )
                ).scalars().all()
                for e in experts:
                    await session.delete(e)
                await session.commit()

        asyncio.new_event_loop().run_until_complete(_delete_v2())

        # 同时把旧专家 deprecated 重置为 False，模拟真正的老项目
        async def _unDeprecate():
            from sqlalchemy import update
            from models.expert import Expert
            async with test_session_factory() as session:
                await session.execute(
                    update(Expert)
                    .where(
                        Expert.project_id == uuid.UUID(project_id),
                        Expert.expert_key.is_(None),
                        Expert.is_builtin == True,  # noqa: E712
                    )
                    .values(deprecated=False)
                )
                await session.commit()

        asyncio.new_event_loop().run_until_complete(_unDeprecate())

        # sync
        resp2 = client.post(f"/api/projects/{project_id}/experts/sync-v2", headers=headers)
        assert resp2.status_code == 200
        result = resp2.json()
        assert result["created"] == 6  # 补齐 6 个 v2
        assert result["deprecated_marked"] == 6  # 旧专家标记 deprecated

        # 验证最终有 12 个 builtin
        resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
        builtin = [e for e in resp3.json() if e["is_builtin"]]
        assert len(builtin) == 12
        assert len([e for e in builtin if e["expert_key"]]) == 6

        # 再 sync 一次，幂等
        resp4 = client.post(f"/api/projects/{project_id}/experts/sync-v2", headers=headers)
        result4 = resp4.json()
        assert result4["created"] == 0
        assert result4["deprecated_marked"] == 0


class TestExpertUpdateDeprecated:
    """ExpertUpdate 可切换 deprecated"""

    def test_patch_deprecated(self):
        headers = _auth_headers("i2_patch_dep", "i2pass")
        resp = client.post("/api/projects", json={"title": "I-2 patch 测试"}, headers=headers)
        project_id = resp.json()["id"]

        resp2 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
        experts = resp2.json()
        v2_expert = next(e for e in experts if e["expert_key"] == "chapter-writer")
        assert v2_expert["deprecated"] is False

        # PATCH 标记 deprecated
        resp3 = client.patch(
            f"/api/projects/{project_id}/experts/{v2_expert['id']}",
            json={"deprecated": True},
            headers=headers,
        )
        assert resp3.status_code == 200
        assert resp3.json()["deprecated"] is True
