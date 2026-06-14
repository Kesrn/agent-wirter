"""最小冒烟测试 — 使用 SQLite 内存数据库，无需 PostgreSQL"""

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, JSON, String, Float
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Monkey-patch SQLite type compiler for PostgreSQL-specific types
# (must happen before importing models / main, which trigger type registration)
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"

from sqlalchemy.ext.compiler import compiles


@compiles(PG_UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
from models import Project, Chapter, Expert, WorldEntry, Character, User, ChapterVersion, Document, DocumentVersion, EvaluationDataset, EvaluationCase, EvaluationRun, EvaluationResult  # noqa: F401
from db.session import get_db, set_engine
from main import app
from services.diff_service import compute_diff
from services.version_service import create_version
from skills.runner import build_expert_system_prompt
from api.routes import _article_system_prompt, _article_brief
from config.settings import settings

# SQLite 内存数据库
TEST_DATABASE_URL = "sqlite+aiosqlite://"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)


test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with test_session_factory() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


@pytest.fixture(autouse=True, scope="session")
def setup_db():
    """在测试会话开始时注入 SQLite engine 并创建所有表"""
    import asyncio

    set_engine(test_engine)

    async def _create():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_create())
    loop.close()
    yield


_cached_auth: dict[str, str] = {}


def _auth_headers(username="testuser", password="testpass"):
    """注册+登录并返回 Authorization headers。Token is cached per username."""
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"login failed: {resp.text}"
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


# ==================== Health ====================

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["llm_provider"] == "mock"


# ==================== Auth ====================

def test_auth_login_success():
    resp = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert "access_token" in data
    assert data["user"]["username"] == "testuser"


def test_auth_login_with_email():
    client.post("/api/auth/register", json={"username": "demouser", "email": "demo@example.com", "password": "demopass"})
    resp = client.post("/api/auth/login", json={"email": "demo@example.com", "password": "demopass"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["username"] == "demouser"


def test_auth_login_no_identity():
    resp = client.post("/api/auth/login", json={"password": "testpass"})
    assert resp.status_code == 422

    resp2 = client.post("/api/auth/login", json={"username": "", "email": "", "password": "testpass"})
    assert resp2.status_code == 422


def test_auth_login_empty_fields():
    resp = client.post("/api/auth/login", json={"username": "user", "password": ""})
    assert resp.status_code == 422


def test_auth_me_with_token():
    headers = _auth_headers("meuser", "mepass")
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == "meuser"


def test_auth_me_no_token():
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_auth_me_invalid_token():
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid_token"})
    assert resp.status_code == 401


def test_auth_me_no_token_detail():
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "未登录"


def test_auth_me_invalid_token_detail():
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid_token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "登录已失效"


def test_auth_login_token_type_bearer():
    client.post("/api/auth/register", json={"username": "bearertest", "password": "bearpass"})
    resp = client.post("/api/auth/login", json={"username": "bearertest", "password": "bearpass"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["user"]["username"] == "bearertest"


# ==================== Projects ====================

def test_list_projects_empty():
    headers = _auth_headers()
    resp = client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_project():
    headers = _auth_headers()
    resp = client.post(
        "/api/projects",
        json={
            "title": "测试小说",
            "description": "一部测试小说",
            "genre": "科幻、悬疑",
            "style": "硬核、暗黑",
            "target_words": 50000,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "测试小说"
    assert data["status"] == "active"
    assert data["genre"] == "科幻、悬疑"
    assert data["style"] == "硬核、暗黑"
    assert data["target_words"] == 50000
    project_id = data["id"]

    resp2 = client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["genre"] == "科幻、悬疑"

    resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    assert resp3.status_code == 200
    experts = resp3.json()
    assert len(experts) == 6
    assert experts[0]["name"] == "创意大师"


def test_project_mode_default_novel():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "默认模式"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "novel"


def test_project_mode_article():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "文章模式", "mode": "article"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "article"
    project_id = resp.json()["id"]
    resp2 = client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp2.json()["mode"] == "article"


def test_project_mode_invalid():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "非法模式", "mode": "poetry"}, headers=headers)
    assert resp.status_code == 422


def test_delete_project_removes_project_and_children():
    headers = _auth_headers("deleteproject", "deletepass")
    project_resp = client.post("/api/projects", json={"title": "待删除项目"}, headers=headers)
    assert project_resp.status_code == 200
    project_id = project_resp.json()["id"]

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "第一章", "sequence_number": 1},
        headers=headers,
    )
    assert chapter_resp.status_code == 200
    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/1",
        json={"content": "需要一起删除的正文"},
        headers=headers,
    )
    assert patch_resp.status_code == 200

    world_resp = client.post(
        f"/api/projects/{project_id}/world-entries",
        json={"title": "设定", "content": "需要一起删除的世界观"},
        headers=headers,
    )
    assert world_resp.status_code == 200
    character_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "角色", "profile": "需要一起删除的角色"},
        headers=headers,
    )
    assert character_resp.status_code == 200

    other_headers = _auth_headers("otherdeleter", "deletepass")
    forbidden_resp = client.delete(f"/api/projects/{project_id}", headers=other_headers)
    assert forbidden_resp.status_code == 404

    delete_resp = client.delete(f"/api/projects/{project_id}", headers=headers)
    assert delete_resp.status_code == 204

    assert client.get(f"/api/projects/{project_id}", headers=headers).status_code == 404
    list_resp = client.get("/api/projects", headers=headers)
    assert all(item["id"] != project_id for item in list_resp.json())
    assert client.get(f"/api/projects/{project_id}/chapters", headers=headers).status_code == 404


def test_evaluation_dataset_case_and_run():
    headers = _auth_headers("eval_user", "eval_pass")
    project_resp = client.post("/api/projects", json={"title": "评测项目"}, headers=headers)
    project_id = project_resp.json()["id"]

    dataset_resp = client.post(
        f"/api/projects/{project_id}/eval-datasets",
        json={"name": "小说回归评测", "description": "基础创作质量", "mode": "regression"},
        headers=headers,
    )
    assert dataset_resp.status_code == 200, dataset_resp.text
    dataset = dataset_resp.json()
    assert dataset["name"] == "小说回归评测"
    dataset_id = dataset["id"]

    case_resp = client.post(
        f"/api/projects/{project_id}/eval-datasets/{dataset_id}/cases",
        json={
            "name": "角色一致性",
            "task_type": "novel_chapter",
            "input_text": "主角林澈在旧城档案馆发现线索。",
            "actual_output": "林澈在旧城档案馆翻到缺页档案，并意识到线索被人故意藏起。",
            "expected_properties": ["必须出现林澈", "必须出现旧城档案馆"],
            "rubric": {"requirement_following": "是否遵守硬性要求", "prose_quality": "文字质量"},
        },
        headers=headers,
    )
    assert case_resp.status_code == 200, case_resp.text
    case = case_resp.json()
    assert case["rubric"]["requirement_following"]

    list_resp = client.get(f"/api/projects/{project_id}/eval-datasets", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["case_count"] == 1

    run_resp = client.post(
        f"/api/projects/{project_id}/eval-datasets/{dataset_id}/runs",
        json={"generation_mode": "judge_only"},
        headers=headers,
    )
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()
    assert run["status"] == "completed"
    assert run["total_cases"] == 1
    assert run["completed_cases"] == 1
    assert run["average_score"] >= 1
    assert len(run["results"]) == 1
    assert run["results"][0]["passed"] is True


def test_import_txt_project_creates_novel_chapters():
    headers = _auth_headers("txtimporter", "txtpass")
    content = "导入小说\n作者：佚名\n\n第1章 开端\n林澈来到旧城。\n\n第二章 转折\n档案突然出现。"
    resp = client.post(
        "/api/projects/import-txt",
        headers=headers,
        data={"title": "导入小说"},
        files={"file": ("demo.txt", content.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project"]["mode"] == "novel"
    assert data["project"]["title"] == "导入小说"
    assert data["import_meta"]["chapter_count"] == 2
    assert len(data["chapters"]) == 2
    assert data["chapters"][0]["title"] == "开端"
    assert "content" not in data["chapters"][0]

    project_id = data["project"]["id"]
    chapters_resp = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    assert chapters_resp.status_code == 200
    assert len(chapters_resp.json()) == 2
    assert chapters_resp.json()[0]["content"] == "林澈来到旧城。"


def test_import_txt_project_allows_many_chapters_with_txt_limit():
    old_upload_limit = settings.MAX_UPLOAD_BYTES
    old_txt_limit = settings.MAX_TXT_IMPORT_BYTES
    object.__setattr__(settings, "MAX_UPLOAD_BYTES", 32)
    object.__setattr__(settings, "MAX_TXT_IMPORT_BYTES", 128 * 1024)
    try:
        headers = _auth_headers("longtxtimporter", "txtpass")
        content = "\n".join(
            f"第{i}章 标题{i}\n这是第{i}章的正文，包含足够的导入内容。"
            for i in range(1, 81)
        )
        assert len(content.encode("utf-8")) > settings.MAX_UPLOAD_BYTES

        resp = client.post(
            "/api/projects/import-txt",
            headers=headers,
            data={"title": "长篇导入小说"},
            files={"file": ("long-demo.txt", content.encode("utf-8"), "text/plain")},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["import_meta"]["chapter_count"] == 80
        assert len(data["chapters"]) == 80
        assert data["chapters"][0]["title"] == "标题1"
        assert data["chapters"][-1]["sequence_number"] == 80
    finally:
        object.__setattr__(settings, "MAX_UPLOAD_BYTES", old_upload_limit)
        object.__setattr__(settings, "MAX_TXT_IMPORT_BYTES", old_txt_limit)


def test_project_not_found_detail():
    headers = _auth_headers()
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = client.get(f"/api/projects/{fake_id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "项目不存在"


# ==================== Chapters ====================

def test_chapters_crud():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "章节测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json() == []

    resp3 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章",
        "outline": "故事开始",
        "sequence_number": 1,
    }, headers=headers)
    assert resp3.status_code == 200
    chapter = resp3.json()
    assert chapter["title"] == "第一章"
    assert chapter["sequence_number"] == 1
    assert chapter["status"] == "draft"

    resp4 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    assert resp4.status_code == 200
    chapters = resp4.json()
    assert len(chapters) == 1
    assert chapters[0]["title"] == "第一章"


def test_duplicate_sequence_number_same_project():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "重复序号测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 200

    resp3 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "又一个第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp3.status_code == 400


def test_extract_chapter_structure_preview_and_apply():
    headers = _auth_headers("extractor", "extractpass")
    project_resp = client.post("/api/projects", json={"title": "提炼测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "第一章", "sequence_number": 1},
        headers=headers,
    )
    assert chapter_resp.status_code == 200

    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/1",
        json={"content": "林澈在旧城档案馆发现了失踪档案。"},
        headers=headers,
    )
    assert patch_resp.status_code == 200

    preview_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/extract-structure",
        json={"mode": "preview"},
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    extraction = preview_resp.json()["extraction"]
    assert extraction["outlines"]
    assert extraction["characters"]
    assert extraction["world_entries"]
    assert extraction["character_events"]

    apply_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/extract-structure",
        json={"mode": "apply", "extraction": extraction},
        headers=headers,
    )
    assert apply_resp.status_code == 200, apply_resp.text
    counts = apply_resp.json()["applied"]["counts"]
    assert counts["outlines"] >= 1
    assert counts["characters"] >= 1
    assert counts["character_events"] >= 1

    outlines_resp = client.get(f"/api/projects/{project_id}/outlines", headers=headers)
    chars_resp = client.get(f"/api/projects/{project_id}/characters", headers=headers)
    world_resp = client.get(f"/api/projects/{project_id}/world-entries", headers=headers)
    hidden_resp = client.get(f"/api/projects/{project_id}/hidden-threads", headers=headers)
    events_resp = client.get(f"/api/projects/{project_id}/character-events", headers=headers)
    assert outlines_resp.status_code == 200
    assert chars_resp.status_code == 200
    assert world_resp.status_code == 200
    assert hidden_resp.status_code == 200
    assert events_resp.status_code == 200
    assert len(outlines_resp.json()) >= 1
    assert len(chars_resp.json()) >= 1
    assert len(world_resp.json()) >= 1
    assert len(hidden_resp.json()) >= 1
    assert len(events_resp.json()) >= 1


def test_extract_chapter_structure_respects_targets():
    headers = _auth_headers("extract-targets", "extractpass")
    project_resp = client.post("/api/projects", json={"title": "提炼范围测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "第一章", "sequence_number": 1},
        headers=headers,
    )
    assert chapter_resp.status_code == 200

    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/1",
        json={"content": "林澈在旧城档案馆发现了失踪档案。"},
        headers=headers,
    )
    assert patch_resp.status_code == 200

    preview_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/extract-structure",
        json={"mode": "preview", "targets": ["characters"]},
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    extraction = preview_resp.json()["extraction"]
    assert extraction["characters"]
    assert extraction["outlines"] == []
    assert extraction["world_entries"] == []
    assert extraction["character_events"] == []

    apply_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/extract-structure",
        json={"mode": "apply", "targets": ["characters"], "extraction": extraction},
        headers=headers,
    )
    assert apply_resp.status_code == 200, apply_resp.text
    counts = apply_resp.json()["applied"]["counts"]
    assert counts["characters"] >= 1
    assert counts["world_entries"] == 0
    assert counts["character_events"] == 0

    world_resp = client.get(f"/api/projects/{project_id}/world-entries", headers=headers)
    assert world_resp.status_code == 200
    assert world_resp.json() == []


def test_character_chapter_event_can_be_updated():
    headers = _auth_headers("event-editor", "eventpass")
    project_resp = client.post("/api/projects", json={"title": "角色轨迹测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    char_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "林澈", "role_type": "protagonist"},
        headers=headers,
    )
    assert char_resp.status_code == 200
    character_id = char_resp.json()["id"]

    first_resp = client.put(
        f"/api/projects/{project_id}/characters/{character_id}/chapter-events/1",
        json={
            "appearance_type": "appeared",
            "event_summary": "林澈发现旧档案。",
            "actions": ["发现旧档案"],
            "state_change": "获得线索",
            "importance": 4,
        },
        headers=headers,
    )
    assert first_resp.status_code == 200, first_resp.text
    event_id = first_resp.json()["id"]

    update_resp = client.put(
        f"/api/projects/{project_id}/characters/{character_id}/chapter-events/1",
        json={
            "appearance_type": "mentioned",
            "event_summary": "林澈只在他人口中被提及。",
            "actions": ["被提及"],
            "importance": 2,
        },
        headers=headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    updated = update_resp.json()
    assert updated["id"] == event_id
    assert updated["appearance_type"] == "mentioned"
    assert updated["event_summary"] == "林澈只在他人口中被提及。"

    events_resp = client.get(
        f"/api/projects/{project_id}/character-events?character_id={character_id}",
        headers=headers,
    )
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) == 1
    assert events[0]["chapter_sequence_number"] == 1

    char_after_resp = client.get(f"/api/projects/{project_id}/characters", headers=headers)
    assert char_after_resp.status_code == 200
    assert char_after_resp.json()[0]["appearance_count"] == 1


def test_extract_chapter_structure_falls_back_when_model_returns_empty():
    headers = _auth_headers("extract-fallback", "extractpass")
    project_resp = client.post("/api/projects", json={"title": "兜底提炼测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    char_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "程璇", "role_type": "protagonist"},
        headers=headers,
    )
    assert char_resp.status_code == 200

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "硬刚穆卓云！", "sequence_number": 6},
        headers=headers,
    )
    assert chapter_resp.status_code == 200

    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/6",
        json={"content": "程璇站在训练场中央，抬头直视穆卓云。穆卓云沉声质问，程璇当场反驳。"},
        headers=headers,
    )
    assert patch_resp.status_code == 200

    preview_resp = client.post(
        f"/api/projects/{project_id}/chapters/6/extract-structure",
        json={"mode": "preview", "extraction": {}},
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    extraction = preview_resp.json()["extraction"]
    assert extraction["outlines"]
    assert extraction["characters"]
    assert extraction["character_events"]
    assert extraction["character_events"][0]["character_name"] == "程璇"


def test_extract_chapter_structure_fills_missing_groups_from_fallback():
    headers = _auth_headers("extract-partial-fallback", "extractpass")
    project_resp = client.post("/api/projects", json={"title": "分组兜底测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    char_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "程璇", "role_type": "protagonist"},
        headers=headers,
    )
    assert char_resp.status_code == 200

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "硬刚穆卓云！", "sequence_number": 6},
        headers=headers,
    )
    assert chapter_resp.status_code == 200

    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/6",
        json={"content": "程璇站在训练场中央，抬头直视穆卓云。穆卓云沉声质问，程璇当场反驳。"},
        headers=headers,
    )
    assert patch_resp.status_code == 200

    preview_resp = client.post(
        f"/api/projects/{project_id}/chapters/6/extract-structure",
        json={
            "mode": "preview",
            "extraction": {
                "outlines": [{"sequence_number": 6, "title": "硬刚穆卓云！", "summary": "模型只给了大纲"}],
                "characters": [],
                "character_events": [],
            },
        },
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    extraction = preview_resp.json()["extraction"]
    assert extraction["outlines"][0]["summary"] == "模型只给了大纲"
    assert extraction["characters"]
    assert extraction["character_events"]


def test_same_sequence_number_different_projects():
    headers = _auth_headers()
    resp_a = client.post("/api/projects", json={"title": "项目A"}, headers=headers)
    project_a_id = resp_a.json()["id"]
    resp_b = client.post("/api/projects", json={"title": "项目B"}, headers=headers)
    project_b_id = resp_b.json()["id"]

    resp2 = client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 200

    resp3 = client.post(f"/api/projects/{project_b_id}/chapters", json={
        "title": "B的第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp3.status_code == 200


def test_chapter_detail_and_update():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "章节详情测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp2 = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["title"] == "第一章"
    assert resp2.json()["content"] is None

    resp3 = client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "这是第一章的内容，一段测试文字。",
    }, headers=headers)
    assert resp3.status_code == 200
    data = resp3.json()
    assert data["content"] == "这是第一章的内容，一段测试文字。"
    assert data["word_count"] > 0

    resp4 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    chapters = resp4.json()
    assert chapters[0]["content"] is not None
    assert chapters[0]["word_count"] > 0

    resp6 = client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "title": "第一章（修改版）", "status": "approved",
    }, headers=headers)
    assert resp6.status_code == 200
    assert resp6.json()["title"] == "第一章（修改版）"
    assert resp6.json()["status"] == "approved"


def test_chapter_detail_isolation():
    headers = _auth_headers()
    resp_a = client.post("/api/projects", json={"title": "项目A"}, headers=headers)
    project_a_id = resp_a.json()["id"]
    resp_b = client.post("/api/projects", json={"title": "项目B"}, headers=headers)
    project_b_id = resp_b.json()["id"]

    client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的章节", "sequence_number": 1,
    }, headers=headers)
    client.post(f"/api/projects/{project_b_id}/chapters", json={
        "title": "B的章节", "sequence_number": 1,
    }, headers=headers)

    resp = client.get(f"/api/projects/{project_a_id}/chapters/1", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["title"] == "A的章节"

    resp2 = client.get(f"/api/projects/{project_b_id}/chapters/1", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["title"] == "B的章节"

    client.patch(f"/api/projects/{project_b_id}/chapters/1", json={"content": "B的内容"}, headers=headers)
    resp3 = client.get(f"/api/projects/{project_a_id}/chapters/1", headers=headers)
    assert resp3.json()["content"] is None

    client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的第七章", "sequence_number": 7,
    }, headers=headers)

    resp4 = client.get(f"/api/projects/{project_b_id}/chapters/7", headers=headers)
    assert resp4.status_code == 404

    resp5 = client.patch(f"/api/projects/{project_b_id}/chapters/7", json={"title": "被篡改"}, headers=headers)
    assert resp5.status_code == 404

    resp6 = client.get(f"/api/projects/{project_a_id}/chapters/7", headers=headers)
    assert resp6.status_code == 200
    assert resp6.json()["title"] == "A的第七章"


def test_chapter_detail_not_found():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "404测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.get(f"/api/projects/{project_id}/chapters/99", headers=headers)
    assert resp2.status_code == 404

    resp3 = client.patch(f"/api/projects/{project_id}/chapters/99", json={"title": "不存在"}, headers=headers)
    assert resp3.status_code == 404


def test_chapter_patch_empty_content_zero_words():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "空内容测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp2 = client.patch(f"/api/projects/{project_id}/chapters/1", json={"content": ""}, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["content"] == ""
    assert resp2.json()["word_count"] == 0


def test_chapter_save_sanitizes_generation_artifacts():
    headers = _auth_headers("sanitize_user", "sanitize_pass")
    resp = client.post("/api/projects", json={"title": "清理正文测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    dirty_content = (
        "窗外很热，**【视觉】阳光把屋子烤得发闷**。\n\n"
        "他听见远处钟声。\n\n"
        "---\n"
        "### 📌 本章要点\n"
        "- 核心事件：不应进入正文\n\n"
        "### 💡 后续建议\n"
        "- 下章继续写"
    )
    resp2 = client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": dirty_content,
    }, headers=headers)
    assert resp2.status_code == 200
    saved = resp2.json()["content"]
    assert "【视觉】" not in saved
    assert "**" not in saved
    assert "阳光把屋子烤得发闷" in saved
    assert "本章要点" not in saved
    assert "后续建议" not in saved
    assert "核心事件" not in saved

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    version_id = resp_v.json()[0]["id"]
    resp_detail = client.get(f"/api/projects/{project_id}/chapters/1/versions/{version_id}", headers=headers)
    assert "本章要点" not in resp_detail.json()["content"]
    assert "【视觉】" not in resp_detail.json()["content"]


def test_skill_prompt_strips_output_templates():
    writer_prompt = build_expert_system_prompt("writer", "DEFAULT")
    renderer_prompt = build_expert_system_prompt("renderer", "DEFAULT")

    assert "## 输出格式" not in writer_prompt
    assert "本章要点" not in writer_prompt
    assert "后续建议" not in writer_prompt
    assert "【视觉】" not in renderer_prompt
    assert "【听觉】" not in renderer_prompt


def test_chapter_patch_empty_title_rejected():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "空标题测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp2 = client.patch(f"/api/projects/{project_id}/chapters/1", json={"title": ""}, headers=headers)
    assert resp2.status_code == 422


def test_chapter_patch_invalid_status_rejected():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "非法状态测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp2 = client.patch(f"/api/projects/{project_id}/chapters/1", json={"status": "bad"}, headers=headers)
    assert resp2.status_code == 422

    for valid_status in ("draft", "reviewing", "revision", "final", "approved"):
        resp3 = client.patch(f"/api/projects/{project_id}/chapters/1", json={"status": valid_status}, headers=headers)
        assert resp3.status_code == 200
        assert resp3.json()["status"] == valid_status


def test_duplicate_sequence_number_detail():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "序号detail测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp2 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "重复", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 400
    assert resp2.json()["detail"] == "章节序号已存在"


# ==================== Characters ====================

def test_character_update_persists_and_can_clear_fields():
    headers = _auth_headers("char_update_user", "char_update_pass")
    resp = client.post("/api/projects", json={"title": "角色更新测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/characters", json={
        "name": "旧角色",
        "role_type": "supporting",
        "profile": "旧简介",
        "faction": "旧阵营",
        "metadata": {"tag": "old"},
    }, headers=headers)
    assert resp2.status_code == 200
    character_id = resp2.json()["id"]

    resp3 = client.patch(f"/api/projects/{project_id}/characters/{character_id}", json={
        "name": "新角色",
        "role_type": "protagonist",
        "profile": "新简介",
        "faction": "新阵营",
        "metadata": {"tag": "new"},
    }, headers=headers)
    assert resp3.status_code == 200
    updated = resp3.json()
    assert updated["name"] == "新角色"
    assert updated["role_type"] == "protagonist"
    assert updated["profile"] == "新简介"
    assert updated["faction"] == "新阵营"
    assert updated["metadata"] == {"tag": "new"}

    resp4 = client.get(f"/api/projects/{project_id}/characters", headers=headers)
    assert resp4.status_code == 200
    listed = resp4.json()[0]
    assert listed["name"] == "新角色"
    assert listed["profile"] == "新简介"
    assert listed["faction"] == "新阵营"
    assert listed["metadata"] == {"tag": "new"}

    resp5 = client.patch(f"/api/projects/{project_id}/characters/{character_id}", json={
        "profile": "",
        "faction": "",
        "metadata": None,
    }, headers=headers)
    assert resp5.status_code == 200
    cleared = resp5.json()
    assert cleared["profile"] == ""
    assert cleared["faction"] == ""
    assert cleared["metadata"] is None

    resp6 = client.get(f"/api/projects/{project_id}/characters", headers=headers)
    assert resp6.status_code == 200
    listed_after_clear = resp6.json()[0]
    assert listed_after_clear["profile"] == ""
    assert listed_after_clear["faction"] == ""
    assert listed_after_clear["metadata"] is None


def test_character_merge_moves_events_and_deletes_duplicate():
    headers = _auth_headers("merge-character", "mergepass")
    project_resp = client.post("/api/projects", json={"title": "角色合并测试"}, headers=headers)
    project_id = project_resp.json()["id"]

    target_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "程璇", "role_type": "protagonist", "scope_type": "core", "profile": "主角"},
        headers=headers,
    )
    source_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "程旋", "role_type": "supporting", "scope_type": "recurring", "profile": "误提取的同名角色"},
        headers=headers,
    )
    assert target_resp.status_code == 200
    assert source_resp.status_code == 200
    target_id = target_resp.json()["id"]
    source_id = source_resp.json()["id"]

    event_resp = client.put(
        f"/api/projects/{project_id}/characters/{source_id}/chapter-events/1",
        json={"appearance_type": "appeared", "event_summary": "程旋在本章觉醒。", "actions": ["觉醒"], "importance": 4},
        headers=headers,
    )
    assert event_resp.status_code == 200

    merge_resp = client.post(
        f"/api/projects/{project_id}/characters/{source_id}/merge",
        json={"target_character_id": target_id},
        headers=headers,
    )
    assert merge_resp.status_code == 200, merge_resp.text
    assert merge_resp.json()["id"] == target_id
    assert "程旋" in merge_resp.json()["metadata"]["aliases"]

    chars_resp = client.get(f"/api/projects/{project_id}/characters", headers=headers)
    assert chars_resp.status_code == 200
    names = [item["name"] for item in chars_resp.json()]
    assert names == ["程璇"]

    events_resp = client.get(f"/api/projects/{project_id}/character-events?character_id={target_id}", headers=headers)
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) == 1
    assert events[0]["event_summary"] == "程旋在本章觉醒。"


def test_story_layer_fields_roundtrip():
    headers = _auth_headers("story_layers_user", "layer_pass")
    project_resp = client.post(
        "/api/projects",
        json={"title": "分层小说", "overall_outline": "全书主线：主角团从觉醒走向决战。"},
        headers=headers,
    )
    assert project_resp.status_code == 200
    project_id = project_resp.json()["id"]
    assert project_resp.json()["overall_outline"] == "全书主线：主角团从觉醒走向决战。"

    patch_resp = client.patch(
        f"/api/projects/{project_id}",
        json={"overall_outline": "更新后的全书大纲"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["overall_outline"] == "更新后的全书大纲"

    char_resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": "程璇", "role_type": "protagonist", "scope_type": "core"},
        headers=headers,
    )
    assert char_resp.status_code == 200
    assert char_resp.json()["scope_type"] == "core"

    world_resp = client.post(
        f"/api/projects/{project_id}/world-entries",
        json={"title": "魔法体系", "category": "规则", "scope_type": "global", "content": "全书通用规则"},
        headers=headers,
    )
    assert world_resp.status_code == 200
    assert world_resp.json()["scope_type"] == "global"


# ==================== Experts ====================

def test_create_custom_expert():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "专家测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/experts", json={
        "name": "悬疑大师",
        "description": "擅长悬疑推理",
        "role_type": "writer",
        "system_prompt": "你是一位悬疑推理大师。",
        "temperature": 0.7,
        "max_tokens": 4096,
        "workflow_position": "replace_writer",
        "context_scope": {"include_world": True, "include_characters": True, "include_previous_chapters": 2, "include_outline": True},
        "trigger": "manual",
        "color": "cyan",
    }, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "悬疑大师"
    assert resp2.json()["is_builtin"] is False


def test_safety_validation():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "安全测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/experts", json={
        "name": "恶意Agent", "role_type": "custom",
        "system_prompt": "x" * 2001, "workflow_position": "standalone",
    }, headers=headers)
    assert resp2.status_code in (400, 422)

    resp3 = client.post(f"/api/projects/{project_id}/experts", json={
        "name": "超限Agent", "role_type": "custom",
        "system_prompt": "正常","max_tokens": 99999, "workflow_position": "standalone",
    }, headers=headers)
    assert resp3.status_code in (400, 422)

    resp4 = client.post(f"/api/projects/{project_id}/experts", json={
        "name": "替换检查器", "role_type": "custom",
        "system_prompt": "正常", "workflow_position": "consistency_checker",
    }, headers=headers)
    assert resp4.status_code in (400, 422)


# ==================== Generate ====================

def test_generate_chapter_sse():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "生成测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 200

    resp3 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue",
    }, headers=headers)
    assert resp3.status_code == 200
    text = resp3.text
    assert "event: progress" in text
    assert "event: done" in text


def test_full_pipeline_approve_persists_after_human_review():
    """Full pipeline approval should find the paused workflow state by thread_id."""
    headers = _auth_headers("pipeline_approve", "pipeline_approve")
    resp = client.post("/api/projects", json={"title": "审批落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp_generate = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1,
        "mode": "full_pipeline",
    }, headers=headers)
    assert resp_generate.status_code == 200
    generate_text = resp_generate.text
    assert "等待人工审核" in generate_text
    match = re.search(r'"thread_id":\s*"([^"]+)"', generate_text)
    assert match, generate_text
    thread_id = match.group(1)

    # Generate only pauses for approval; it must not persist before approve.
    resp_before = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp_before.status_code == 200
    assert resp_before.json()["content"] is None

    resp_resume = client.post(
        f"/api/projects/{project_id}/chapters/resume?thread_id={thread_id}&action=approve",
        headers=headers,
    )
    assert resp_resume.status_code == 200
    resume_text = resp_resume.text
    assert "工作流状态不存在或已过期" not in resume_text
    assert "event: done" in resume_text

    resp_after = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp_after.status_code == 200
    assert resp_after.json()["content"]

    resp_versions = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    assert resp_versions.status_code == 200
    versions = resp_versions.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "ai_approve"


def test_full_pipeline_review_and_revise_cycle():
    """修改后采纳 should first return directions, then revise without losing workflow state."""
    headers = _auth_headers("pipeline_revise", "pipeline_revise")
    resp = client.post("/api/projects", json={"title": "修订流程测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp_generate = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1,
        "mode": "full_pipeline",
    }, headers=headers)
    assert resp_generate.status_code == 200
    match = re.search(r'"thread_id":\s*"([^"]+)"', resp_generate.text)
    assert match, resp_generate.text
    thread_id = match.group(1)

    resp_review = client.post(
        f"/api/projects/{project_id}/chapters/resume?thread_id={thread_id}&action=review",
        headers=headers,
    )
    assert resp_review.status_code == 200
    assert "event: revision_suggestions" in resp_review.text
    assert '"max_revisions": 3' in resp_review.text

    resp_revise = client.post(
        f"/api/projects/{project_id}/chapters/resume",
        params={"thread_id": thread_id, "action": "revise", "feedback": "加强冲突和人物动机"},
        headers=headers,
    )
    assert resp_revise.status_code == 200
    assert "工作流状态不存在或已过期" not in resp_revise.text
    assert "等待人工审核" in resp_revise.text


def test_revision_writer_rewrites_candidate_instead_of_continuing():
    """Revision writer must rewrite the candidate draft, not continue after it."""
    import asyncio
    from agents.workflow import writer_node

    async def _run():
        result = await writer_node({
            "project_id": "00000000-0000-0000-0000-000000000000",
            "chapter_id": "00000000-0000-0000-0000-000000000001",
            "mode": "full_pipeline",
            "context": "无",
            "draft": "已有候选稿到此结束。",
            "original_text": "",
            "critiques": ["[用户选择的修改方向] 加强心理描写"],
            "consistency_report": "",
            "edited_draft": "",
            "revision_count": 1,
            "writer_prompt": "",
            "critic_prompt": "",
            "editor_prompt": "",
            "consistency_prompt": "",
            "llm_config": None,
            "selected_outline_ids": [],
            "selected_character_ids": [],
            "selected_world_entry_ids": [],
            "selected_hidden_thread_ids": [],
            "target_words": 0,
        })
        return result["draft"]

    text = asyncio.new_event_loop().run_until_complete(_run())
    assert "已有候选稿" in text
    assert "（生成内容）" not in text
    assert "你来了" not in text


def test_expert_test_sse():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "专家SSE测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    experts = resp2.json()
    expert_id = experts[0]["id"]

    resp3 = client.post(f"/api/projects/{project_id}/experts/{expert_id}/test", json={
        "test_text": "一段测试文本",
    }, headers=headers)
    assert resp3.status_code == 200
    text = resp3.text
    assert "event: agent_start" in text
    assert "event: done" in text


def test_generate_chapter_isolation():
    headers = _auth_headers()
    resp_a = client.post("/api/projects", json={"title": "项目A"}, headers=headers)
    project_a_id = resp_a.json()["id"]
    resp_b = client.post("/api/projects", json={"title": "项目B"}, headers=headers)
    project_b_id = resp_b.json()["id"]

    resp_ch = client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的章节", "sequence_number": 1,
    }, headers=headers)
    chapter_a_id = resp_ch.json()["id"]

    resp_cross = client.post(f"/api/projects/{project_b_id}/chapters/generate", json={
        "chapter_id": chapter_a_id, "mode": "continue",
    }, headers=headers)
    assert resp_cross.status_code == 404


def test_generate_chapter_num_not_found():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "章节号测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 99, "mode": "continue",
    }, headers=headers)
    assert resp2.status_code == 404


def test_generate_does_not_persist_content():
    """Generate endpoint must NOT write Chapter.content or create versions."""
    headers = _auth_headers("gen_nopersist", "gen_nopersist")
    resp = client.post("/api/projects", json={"title": "不落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    # Set some pre-existing content so we can verify it stays unchanged
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "原始内容，不应被覆盖。",
    }, headers=headers)

    resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    writer_expert_id = resp3.json()[0]["id"]

    # Generate with writer expert
    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue", "expert_id": writer_expert_id,
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text

    # Content must remain the original, not the generated candidate
    resp5 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    chapters = resp5.json()
    assert len(chapters) == 1
    assert chapters[0]["content"] == "原始内容，不应被覆盖。"

    # No new version should have been created by generate
    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_v.json()
    # Only the manual version from the PATCH above
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"


def test_critic_generate_does_not_persist():
    headers = _auth_headers("critic_nopersist", "critic_nopersist")
    resp = client.post("/api/projects", json={"title": "critic不落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    critic_expert_id = resp3.json()[1]["id"]

    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue", "expert_id": critic_expert_id,
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text

    resp5 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    chapters = resp5.json()
    assert len(chapters) == 1
    assert chapters[0]["content"] is None

    # No version created by critic generate
    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    assert resp_v.json() == []


def test_enhance_does_not_persist():
    """Enhance mode must stream candidate but not save to chapter."""
    headers = _auth_headers("enhance_nopersist", "enhance_nopersist")
    resp = client.post("/api/projects", json={"title": "enhance不落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "待润色的原始文本。",
    }, headers=headers)

    # Step 2: enhance with a direction
    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "enhance", "enhance_direction": "增强画面感",
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text
    assert "待润色" in resp4.text
    assert "命运的齿轮" not in resp4.text

    # Content must stay original
    resp5 = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp5.json()["content"] == "待润色的原始文本。"

    # No new version from enhance
    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_v.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"


def test_enhance_rejects_oversized_target_words():
    """Enhance should reject unsafe word budgets instead of returning truncated text."""
    headers = _auth_headers("enhance_limit", "enhance_limit")
    resp = client.post("/api/projects", json={"title": "enhance字数限制测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "这是一段需要润色的正文。",
    }, headers=headers)

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1,
        "mode": "enhance",
        "enhance_direction": "增强画面感",
        "target_words": 999999,
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: error" in resp2.text
    assert "单次润色最多支持" in resp2.text


def test_continue_does_not_persist():
    """Continue mode must stream candidate but not append to chapter."""
    headers = _auth_headers("continue_nopersist", "continue_nopersist")
    resp = client.post("/api/projects", json={"title": "continue不落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "已有的章节正文。",
    }, headers=headers)

    # Continue with a direction
    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue", "turn_direction": "加入冲突",
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text

    # Content must stay original (no append)
    resp5 = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp5.json()["content"] == "已有的章节正文。"

    # No new version from continue
    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_v.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"


def test_generation_history_created_without_content_persist():
    """Generate should create AI generation history without changing chapter content or versions."""
    headers = _auth_headers("gen_history", "gen_history")
    resp = client.post("/api/projects", json={"title": "生成历史测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "当前章节原文。",
    }, headers=headers)

    resp_generate = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1,
        "mode": "continue",
        "turn_direction": "加入冲突",
    }, headers=headers)
    assert resp_generate.status_code == 200
    assert "event: generation_record" in resp_generate.text

    resp_chapter = client.get(f"/api/projects/{project_id}/chapters/1", headers=headers)
    assert resp_chapter.json()["content"] == "当前章节原文。"

    resp_versions = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_versions.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"

    resp_history = client.get(f"/api/projects/{project_id}/chapters/1/generations", headers=headers)
    assert resp_history.status_code == 200
    records = resp_history.json()
    assert len(records) == 1
    assert records[0]["status"] == "candidate"
    assert records[0]["mode"] == "continue"
    assert records[0]["direction"] == "加入冲突"
    assert "content" not in records[0]

    record_id = records[0]["id"]
    resp_detail = client.get(f"/api/projects/{project_id}/generations/{record_id}", headers=headers)
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["content"]
    assert detail["word_count"] > 0

    resp_update = client.patch(f"/api/projects/{project_id}/generations/{record_id}", json={
        "status": "applied",
    }, headers=headers)
    assert resp_update.status_code == 200
    assert resp_update.json()["status"] == "applied"

    resp_diff = client.post(f"/api/projects/{project_id}/generations/{record_id}/diff", json={
        "current_content": "当前章节原文。",
    }, headers=headers)
    assert resp_diff.status_code == 200
    assert resp_diff.json()["generation_id"] == record_id
    assert isinstance(resp_diff.json()["diff"], list)


def test_article_generate_accepts_brief_and_does_not_persist():
    """Article projects should generate copy/article content without saving it immediately."""
    headers = _auth_headers("article_generate", "article_generate")
    resp = client.post("/api/projects", json={"title": "文章生成测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "第一篇", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_id": doc_id,
        "chapter_num": 1,
        "mode": "full_pipeline",
        "content_type": "公众号文章",
        "platform": "微信公众号",
        "audience": "准备做内容运营的新手",
        "content_goal": "解释方法并引导咨询",
        "tone": "专业但不端着",
        "key_points": "选题、结构、行动引导",
        "target_words": 1200,
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: content_output" in resp2.text
    assert "文章" in resp2.text or "文案" in resp2.text
    assert "你来了" not in resp2.text
    assert "夜色渐深" not in resp2.text

    resp3 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp3.json()["content"] is None

    resp_v = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers)
    assert resp_v.json() == []


def test_article_enhance_rewrites_copy_without_persisting():
    """Article enhance should behave as rewrite/optimization, not fiction continuation."""
    headers = _auth_headers("article_enhance", "article_enhance")
    resp = client.post("/api/projects", json={"title": "文章改写测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "产品介绍", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "我们的工具可以帮助团队更快完成内容协作。",
    }, headers=headers)

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_id": doc_id,
        "chapter_num": 1,
        "mode": "enhance",
        "enhance_direction": "强化卖点并补足行动引导",
        "content_type": "产品文案",
        "audience": "内容团队负责人",
        "content_goal": "引导预约演示",
        "tone": "清晰克制",
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: done" in resp2.text
    assert "行动引导" in resp2.text
    assert "你来了" not in resp2.text
    assert "剧情" not in resp2.text

    resp3 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp3.json()["content"] == "我们的工具可以帮助团队更快完成内容协作。"

    resp_v = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers)
    versions = resp_v.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"


def test_article_suggestions_are_not_story_turns():
    """Article suggestion mode should return content angles, not story turn suggestions."""
    headers = _auth_headers("article_suggest", "article_suggest")
    resp = client.post("/api/projects", json={"title": "文章建议测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "标题备选", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_id": doc_id,
        "chapter_num": 1,
        "mode": "continue",
        "content_type": "小红书文案",
        "platform": "小红书",
        "audience": "自由职业者",
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: content_suggestions" in resp2.text
    assert "受众" in resp2.text or "卖点" in resp2.text or "行动引导" in resp2.text
    assert "主角发现" not in resp2.text
    assert "新角色登场" not in resp2.text


# ==================== Export ====================

def test_export_txt():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "导出测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "这是第一章的内容。",
    }, headers=headers)
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第二章", "sequence_number": 2,
    }, headers=headers)

    resp2 = client.get(f"/api/projects/{project_id}/export?format=txt", headers=headers)
    assert resp2.status_code == 200
    text = resp2.text
    assert "导出测试" in text
    assert "第1章 第一章" in text
    assert "这是第一章的内容。" in text
    assert "第2章 第二章" in text


def test_export_md():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "MD导出测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "这是第一章的内容。",
    }, headers=headers)

    resp2 = client.get(f"/api/projects/{project_id}/export?format=md", headers=headers)
    assert resp2.status_code == 200
    text = resp2.text
    assert "# MD导出测试" in text
    assert "## 第1章 第一章" in text
    assert "这是第一章的内容。" in text


def test_export_article_documents_txt():
    headers = _auth_headers("export_article", "export_article")
    resp = client.post("/api/projects", json={"title": "文章导出测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "第一篇", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "这是第一篇文章。",
    }, headers=headers)

    resp2 = client.get(f"/api/projects/{project_id}/export?format=txt", headers=headers)
    assert resp2.status_code == 200
    text = resp2.text
    assert "文章导出测试" in text
    assert "1. 第一篇" in text
    assert "这是第一篇文章。" in text
    assert "第1章" not in text


def test_export_project_not_found():
    headers = _auth_headers()
    resp = client.get("/api/projects/00000000-0000-0000-0000-000000000000/export?format=txt", headers=headers)
    assert resp.status_code == 404


# ==================== Project Assets ====================

def test_upload_project_image(tmp_path):
    old_upload_dir = settings.UPLOAD_DIR
    object.__setattr__(settings, "UPLOAD_DIR", str(tmp_path))
    try:
        headers = _auth_headers("asset_user", "asset_pass")
        resp = client.post("/api/projects", json={"title": "图片资源测试"}, headers=headers)
        project_id = resp.json()["id"]

        png_1x1 = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = client.post(
            f"/api/projects/{project_id}/assets/images",
            content=png_1x1,
            headers={**headers, "Content-Type": "image/png"},
        )

        assert upload.status_code == 200
        data = upload.json()
        assert data["url"].startswith(f"/media/projects/{project_id}/images/")
        assert data["filename"].endswith(".png")
        assert data["content_type"] == "image/png"
        assert data["size"] == len(png_1x1)

        saved_path = os.path.join(tmp_path, "projects", project_id, "images", data["filename"])
        assert os.path.exists(saved_path)
        with open(saved_path, "rb") as f:
            assert f.read() == png_1x1
    finally:
        object.__setattr__(settings, "UPLOAD_DIR", old_upload_dir)


def test_upload_project_image_rejects_non_image():
    headers = _auth_headers("asset_reject_user", "asset_reject_pass")
    resp = client.post("/api/projects", json={"title": "图片拒绝测试"}, headers=headers)
    project_id = resp.json()["id"]

    upload = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=b"not an image",
        headers={**headers, "Content-Type": "text/plain"},
    )

    assert upload.status_code == 415


# ==================== Chapter Version History ====================

def test_version_created_on_manual_content_update():
    """Patching chapter content should create a version with source='manual'."""
    headers = _auth_headers("veruser1", "verpass1")
    resp = client.post("/api/projects", json={"title": "版本测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    # First content set creates the first saved snapshot.
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "第一版内容。",
    }, headers=headers)

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    assert resp_v.status_code == 200
    versions = resp_v.json()
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["source"] == "manual"
    assert versions[0]["word_count"] > 0
    assert "content" not in versions[0]

    # Second content update creates version 2.
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "第二版内容。",
    }, headers=headers)

    resp_v2 = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    assert resp_v2.status_code == 200
    versions = resp_v2.json()
    assert len(versions) == 2
    assert versions[0]["version_number"] == 2
    assert versions[0]["source"] == "manual"
    assert versions[0]["word_count"] > 0
    # List response should NOT contain content
    assert "content" not in versions[0]


def test_version_detail_contains_content():
    """GET single version should include content."""
    headers = _auth_headers("veruser2", "verpass2")
    resp = client.post("/api/projects", json={"title": "版本详情测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "旧内容。",
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "新内容。",
    }, headers=headers)

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    version_id = resp_v.json()[0]["id"]

    resp_detail = client.get(f"/api/projects/{project_id}/chapters/1/versions/{version_id}", headers=headers)
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["content"] == "新内容。"
    assert detail["version_number"] == 2
    assert detail["source"] == "manual"


def test_version_not_found():
    headers = _auth_headers("veruser3", "verpass3")
    resp = client.post("/api/projects", json={"title": "版本404测试"}, headers=headers)
    project_id = resp.json()["id"]
    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)

    fake_vid = "00000000-0000-0000-0000-000000000000"
    resp = client.get(f"/api/projects/{project_id}/chapters/1/versions/{fake_vid}", headers=headers)
    assert resp.status_code == 404


def test_version_diff_endpoint():
    """POST diff should return structured diff between two versions."""
    headers = _auth_headers("veruser4", "verpass4")
    resp = client.post("/api/projects", json={"title": "diff测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "第一行\n第二行\n第三行",
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "第一行\n修改的第二行\n第三行\n新增第四行",
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "完全不同的内容",
    }, headers=headers)

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_v.json()
    assert len(versions) == 3

    v2_id = next(v["id"] for v in versions if v["version_number"] == 2)
    v1_id = next(v["id"] for v in versions if v["version_number"] == 1)

    resp_diff = client.post(f"/api/projects/{project_id}/chapters/1/versions/diff", json={
        "version_id_a": v1_id,
        "version_id_b": v2_id,
    }, headers=headers)
    assert resp_diff.status_code == 200
    diff_data = resp_diff.json()
    assert diff_data["version_a"] == 1
    assert diff_data["version_b"] == 2
    assert isinstance(diff_data["diff"], list)
    assert len(diff_data["diff"]) > 0
    # Each hunk has a tag
    for hunk in diff_data["diff"]:
        assert hunk["tag"] in ("equal", "insert", "delete", "replace")


def test_version_diff_against_current_content():
    """POST diff can compare a stored version with unsaved current editor content."""
    headers = _auth_headers("veruser_current", "verpass_current")
    resp = client.post("/api/projects", json={"title": "当前正文diff测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "第一行\n第二行",
    }, headers=headers)

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    version_id = resp_v.json()[0]["id"]

    resp_diff = client.post(f"/api/projects/{project_id}/chapters/1/versions/diff", json={
        "version_id_a": version_id,
        "current_content": "第一行\n当前正在编辑的第二行\n新增第三行",
    }, headers=headers)
    assert resp_diff.status_code == 200
    diff_data = resp_diff.json()
    assert diff_data["version_a"] == 1
    assert diff_data["version_b"] == 0
    assert any(h["tag"] in ("insert", "replace") for h in diff_data["diff"])


def test_version_owner_isolation():
    """Versions from one project should not be accessible via another project's path."""
    headers = _auth_headers("veruser5", "verpass5")
    resp_a = client.post("/api/projects", json={"title": "项目A"}, headers=headers)
    project_a_id = resp_a.json()["id"]
    resp_b = client.post("/api/projects", json={"title": "项目B"}, headers=headers)
    project_b_id = resp_b.json()["id"]

    client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的章节", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_a_id}/chapters/1", json={"content": "A内容1"}, headers=headers)
    client.patch(f"/api/projects/{project_a_id}/chapters/1", json={"content": "A内容2"}, headers=headers)

    resp_v = client.get(f"/api/projects/{project_a_id}/chapters/1/versions", headers=headers)
    version_id = resp_v.json()[0]["id"]

    # Try to access A's version via B's project path — should 404
    resp_cross = client.get(f"/api/projects/{project_b_id}/chapters/1/versions/{version_id}", headers=headers)
    assert resp_cross.status_code == 404

    # Try to list versions for B's non-existent chapter
    client.post(f"/api/projects/{project_b_id}/chapters", json={
        "title": "B的章节", "sequence_number": 1,
    }, headers=headers)
    resp_b_versions = client.get(f"/api/projects/{project_b_id}/chapters/1/versions", headers=headers)
    assert resp_b_versions.status_code == 200
    assert resp_b_versions.json() == []


def test_version_source_validation():
    """create_version should reject invalid source values."""
    import asyncio
    from db.session import async_session

    async def _test():
        async with async_session() as db:
            v = await create_version(db, "some-id", "content", source="invalid_source")
            # Should have raised ValueError
            assert False, "Should have raised ValueError"
    try:
        asyncio.new_event_loop().run_until_complete(_test())
        assert False, "Should have raised"
    except ValueError as e:
        assert "invalid_source" in str(e)


def test_version_diff_backward_compat_two_versions():
    """Two-version diff (version_id_a + version_id_b) still works after current_content extension."""
    headers = _auth_headers("veruser_compat", "verpass_compat")
    resp = client.post("/api/projects", json={"title": "兼容性diff测试"}, headers=headers)
    project_id = resp.json()["id"]

    client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "旧内容第一行\n旧内容第二行",
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "新内容第一行\n新内容第二行\n新内容第三行",
    }, headers=headers)

    resp_v = client.get(f"/api/projects/{project_id}/chapters/1/versions", headers=headers)
    versions = resp_v.json()
    v1_id = next(v["id"] for v in versions if v["version_number"] == 1)
    v2_id = next(v["id"] for v in versions if v["version_number"] == 2)

    resp_diff = client.post(f"/api/projects/{project_id}/chapters/1/versions/diff", json={
        "version_id_a": v1_id,
        "version_id_b": v2_id,
    }, headers=headers)
    assert resp_diff.status_code == 200
    diff_data = resp_diff.json()
    assert diff_data["version_a"] == 1
    assert diff_data["version_b"] == 2
    assert isinstance(diff_data["diff"], list)
    assert len(diff_data["diff"]) > 0


def test_version_diff_cross_project_inaccessible():
    """Diff should reject version_id from another project/chapter."""
    headers = _auth_headers("veruser_cross", "verpass_cross")
    resp_a = client.post("/api/projects", json={"title": "项目A"}, headers=headers)
    project_a_id = resp_a.json()["id"]
    resp_b = client.post("/api/projects", json={"title": "项目B"}, headers=headers)
    project_b_id = resp_b.json()["id"]

    client.post(f"/api/projects/{project_a_id}/chapters", json={
        "title": "A的章节", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_a_id}/chapters/1", json={"content": "A内容1"}, headers=headers)
    client.patch(f"/api/projects/{project_a_id}/chapters/1", json={"content": "A内容2"}, headers=headers)

    client.post(f"/api/projects/{project_b_id}/chapters", json={
        "title": "B的章节", "sequence_number": 1,
    }, headers=headers)
    client.patch(f"/api/projects/{project_b_id}/chapters/1", json={"content": "B内容1"}, headers=headers)

    resp_va = client.get(f"/api/projects/{project_a_id}/chapters/1/versions", headers=headers)
    resp_vb = client.get(f"/api/projects/{project_b_id}/chapters/1/versions", headers=headers)
    va_id = resp_va.json()[0]["id"]
    vb_id = resp_vb.json()[0]["id"]

    # Use A's version_id_a with B's version_id_b via B's project path — should 404
    resp_diff = client.post(f"/api/projects/{project_b_id}/chapters/1/versions/diff", json={
        "version_id_a": va_id,
        "version_id_b": vb_id,
    }, headers=headers)
    assert resp_diff.status_code == 404

    # Use A's version_id_a with current_content via B's project path — should 404
    resp_diff2 = client.post(f"/api/projects/{project_b_id}/chapters/1/versions/diff", json={
        "version_id_a": va_id,
        "current_content": "任意内容",
    }, headers=headers)
    assert resp_diff2.status_code == 404


def test_diff_service_pure():
    """compute_diff should produce correct hunks for known inputs."""
    diff = compute_diff("第一行\n第二行\n第三行\n", "第一行\n修改行\n第三行\n新增行\n")
    tags = [h["tag"] for h in diff]
    assert "equal" in tags
    assert "replace" in tags or "delete" in tags or "insert" in tags


# ==================== Article Mode 补充测试 ====================

def test_article_request_params_accepted():
    """GenerateRequest should accept all article-specific fields"""
    from schemas.api import GenerateRequest
    req = GenerateRequest(
        mode="enhance",
        document_id="00000000-0000-0000-0000-000000000001",
        content_type="copywriting",
        platform="小红书",
        audience="年轻女性",
        content_goal="种草推荐",
        tone="活泼亲切",
        key_points="成分、效果、价格",
    )
    assert req.content_type == "copywriting"
    assert req.document_id == "00000000-0000-0000-0000-000000000001"
    assert req.platform == "小红书"
    assert req.audience == "年轻女性"
    assert req.content_goal == "种草推荐"
    assert req.tone == "活泼亲切"
    assert req.key_points == "成分、效果、价格"


def test_article_system_prompt_anti_novel():
    """_article_system_prompt must contain anti-novel directives"""
    prompt = _article_system_prompt("生成完整文章/文案")
    # 禁止小说要素 — 不应包含小说写作特有指令
    forbidden = ["角色一致", "大纲优先", "暗线融入", "世界观遵守"]
    for elem in forbidden:
        assert elem not in prompt, f"Article prompt contains novel directive: {elem}"
    # 必须包含文章/文案相关指导
    assert "文章" in prompt or "文案" in prompt
    # 禁止续写
    assert "禁止" in prompt or "不得" in prompt


def test_article_brief_includes_fields():
    """_article_brief should include article-specific fields"""
    from schemas.api import GenerateRequest
    req = GenerateRequest(
        mode="enhance",
        content_type="article",
        platform="微信公众号",
        audience="25-35岁职场新人",
        content_goal="品牌价值传播",
        tone="专业亲和",
        key_points="品牌故事、用户案例",
    )
    brief = _article_brief(req)
    assert "微信公众号" in brief
    assert "25-35岁职场新人" in brief
    assert "品牌价值传播" in brief
    assert "专业亲和" in brief


def test_article_summarize_does_not_persist():
    """Article summarize should not persist content and should use audience perspective"""
    headers = _auth_headers("article_summ", "article_summ")
    resp = client.post("/api/projects", json={"title": "文章反馈测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "产品文章", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "这是一篇关于产品创新的深度文章。",
    }, headers=headers)

    resp2 = client.post(f"/api/projects/{project_id}/documents/generate", json={
        "document_id": doc_id,
        "chapter_num": 1,
        "mode": "summarize",
        "content_type": "article",
        "audience": "产品经理",
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: done" in resp2.text
    assert "event: content_output" in resp2.text

    # summarize 不落库
    resp3 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp3.json()["content"] == "这是一篇关于产品创新的深度文章。"

    # 只有之前手动保存的版本，不应有 summarize 新增的版本
    resp_v = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers)
    versions = resp_v.json()
    assert len(versions) == 1
    assert versions[0]["source"] == "manual"


def test_article_continue_with_direction_no_story():
    """Article continue with direction should not produce story continuation"""
    headers = _auth_headers("article_cont", "article_cont")
    resp = client.post("/api/projects", json={"title": "文章续写测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "产品文案", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "产品创新的三个核心维度。",
    }, headers=headers)

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_id": doc_id,
        "chapter_num": 1,
        "mode": "continue",
        "turn_direction": "补充行业案例",
        "content_type": "article",
        "platform": "微信公众号",
        "audience": "产品经理",
    }, headers=headers)
    assert resp2.status_code == 200
    assert "event: done" in resp2.text

    # 不落库
    resp3 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp3.json()["content"] == "产品创新的三个核心维度。"


def test_document_generate_alias_rejects_novel_project():
    """Document generation alias should be reserved for article projects."""
    headers = _auth_headers("doc_gen_novel", "doc_gen_novel")
    resp = client.post("/api/projects", json={"title": "小说项目", "mode": "novel"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/documents/generate", json={
        "mode": "continue",
        "chapter_num": 1,
    }, headers=headers)

    assert resp2.status_code == 400
    assert "Document generation only available" in resp2.text


def test_document_resume_alias_rejects_novel_project():
    """Document resume alias should be reserved for article projects."""
    headers = _auth_headers("doc_resume_novel", "doc_resume_novel")
    resp = client.post("/api/projects", json={"title": "小说项目", "mode": "novel"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(
        f"/api/projects/{project_id}/documents/resume?thread_id=missing-thread&action=reject",
        headers=headers,
    )

    assert resp2.status_code == 400
    assert "Document generation only available" in resp2.text


# ==================== Article Review Pipeline 测试 ====================

def test_article_full_pipeline_emits_review_events():
    """Article full_pipeline should emit article_review SSE events after content generation."""
    headers = _auth_headers("article_pipe", "article_pipe")
    resp = client.post("/api/projects", json={"title": "文章流水线测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    resp_doc = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "测试文案", "position": 1,
    }, headers=headers)
    doc_id = resp_doc.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_id": doc_id,
        "chapter_num": 1,
        "mode": "full_pipeline",
        "content_type": "article",
        "platform": "微信公众号",
        "audience": "内容运营",
    }, headers=headers)
    assert resp2.status_code == 200
    # 必须包含审校事件
    assert "event: article_review" in resp2.text
    # 必须包含内容输出
    assert "event: content_output" in resp2.text
    # 不落库
    resp3 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp3.json()["content"] is None

    resp_history = client.get(f"/api/projects/{project_id}/documents/{doc_id}/generations", headers=headers)
    assert resp_history.status_code == 200
    records = resp_history.json()
    assert len(records) == 1
    assert records[0]["mode"] == "full_pipeline"
    record_id = records[0]["id"]

    resp_detail = client.get(f"/api/projects/{project_id}/generations/{record_id}", headers=headers)
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["document_id"] == doc_id
    assert detail["review_results"]
    assert "structure" in detail["review_results"]


def test_article_context_remap_removes_novel_terms():
    """Article context remapping should replace novel terms with article equivalents."""
    from services.article_context import remap_context_for_article
    novel_context = (
        "## 大纲\n### 第1章 开端\n故事开始\n\n"
        "## 角色资料\n- 张三(主角): 勇敢\n\n"
        "## 世界观设定\n- [体系] 魔法: 高阶魔法\n\n"
        "## 暗线设定\n- 伏笔A: 未来揭晓\n\n"
        "## 前文内容\n### 前章\n剧情发展"
    )
    article_ctx = remap_context_for_article(novel_context)
    # 小说术语应被替换
    assert "大纲" not in article_ctx
    assert "内容结构" in article_ctx
    assert "角色资料" not in article_ctx
    assert "受众画像" in article_ctx
    assert "世界观设定" not in article_ctx
    assert "品牌" in article_ctx
    assert "暗线设定" not in article_ctx
    assert "内容策略" in article_ctx
    # 行内术语替换
    assert "主角" not in article_ctx
    assert "剧情" not in article_ctx


def test_article_context_remap_empty_input():
    """Empty input should return empty output."""
    from services.article_context import remap_context_for_article
    assert remap_context_for_article("") == ""
    assert remap_context_for_article(None) == ""


# ==================== Document API Alias 测试 ====================

def test_document_list_only_for_article_projects():
    """Document list API should only work for article projects."""
    headers = _auth_headers("doc_alias", "doc_alias")
    # Novel project — should 400
    resp = client.post("/api/projects", json={"title": "小说项目"}, headers=headers)
    novel_id = resp.json()["id"]
    resp2 = client.get(f"/api/projects/{novel_id}/documents", headers=headers)
    assert resp2.status_code == 400

    # Article project — should work
    resp3 = client.post("/api/projects", json={"title": "文章项目", "mode": "article"}, headers=headers)
    article_id = resp3.json()["id"]
    resp4 = client.get(f"/api/projects/{article_id}/documents", headers=headers)
    assert resp4.status_code == 200
    assert resp4.json() == []


def test_document_crud():
    """Document CRUD should use Document table, not Chapter table."""
    headers = _auth_headers("doc_crud", "doc_crud")
    resp = client.post("/api/projects", json={"title": "文档测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    # Create via documents API
    resp_create = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "第一篇", "position": 1,
    }, headers=headers)
    assert resp_create.status_code == 200
    doc = resp_create.json()
    doc_id = doc["id"]
    assert doc["position"] == 1
    assert doc["title"] == "第一篇"

    # Update document content via documents API
    resp_patch = client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "这是文档内容。",
    }, headers=headers)
    assert resp_patch.status_code == 200
    assert resp_patch.json()["content"] == "这是文档内容。"

    # Read via documents API by document_id
    resp2 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["title"] == "第一篇"
    assert data["content"] == "这是文档内容。"
    assert data["position"] == 1
    assert "word_count" in data
    assert "updated_at" in data

    # List documents
    resp3 = client.get(f"/api/projects/{project_id}/documents", headers=headers)
    assert resp3.status_code == 200
    docs = resp3.json()
    assert len(docs) == 1
    assert docs[0]["id"] == doc_id

    # Update document title via documents API
    resp4 = client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "title": "第一篇（修订）",
        "content": "修订后的文档内容。",
    }, headers=headers)
    assert resp4.status_code == 200
    assert resp4.json()["title"] == "第一篇（修订）"
    assert resp4.json()["content"] == "修订后的文档内容。"

    # Document should NOT appear in chapters API (separate table)
    resp5 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    assert resp5.status_code == 200
    assert len(resp5.json()) == 0

    # Delete via documents API
    resp6 = client.delete(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp6.status_code == 200
    resp7 = client.get(f"/api/projects/{project_id}/documents/{doc_id}", headers=headers)
    assert resp7.status_code == 404


def test_document_not_found():
    """Document API should return 404 for non-existent document_id."""
    headers = _auth_headers("doc_404", "doc_404")
    resp = client.post("/api/projects", json={"title": "文档404", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp2 = client.get(f"/api/projects/{project_id}/documents/{fake_id}", headers=headers)
    assert resp2.status_code == 404


def test_document_save_creates_version():
    """Saving document content should create a document version."""
    headers = _auth_headers("doc_ver", "doc_ver")
    resp = client.post("/api/projects", json={"title": "文档版本测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    # Create document
    resp_create = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "版本测试", "position": 1,
    }, headers=headers)
    doc_id = resp_create.json()["id"]

    # Save content — should create version
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "第一版内容。",
    }, headers=headers)

    # Save again — should create another version
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "第二版内容。",
    }, headers=headers)

    # List versions
    resp_versions = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers)
    assert resp_versions.status_code == 200
    versions = resp_versions.json()
    assert len(versions) == 2
    assert versions[0]["version_number"] == 2
    assert versions[1]["version_number"] == 1

    # Get specific version
    v1_id = versions[1]["id"]
    resp_v1 = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions/{v1_id}", headers=headers)
    assert resp_v1.status_code == 200
    assert resp_v1.json()["content"] == "第一版内容。"

    # Restore specific version
    resp_restore = client.post(f"/api/projects/{project_id}/documents/{doc_id}/versions/{v1_id}/restore", headers=headers)
    assert resp_restore.status_code == 200
    assert resp_restore.json()["content"] == "第一版内容。"


def test_document_patch_explicit_null_content_clears_content():
    """Explicit content=null should clear content; omitted content should leave it unchanged."""
    headers = _auth_headers("doc_null_content", "doc_null_content")
    resp = client.post("/api/projects", json={"title": "文档清空测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]
    resp_create = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "清空测试", "position": 1, "content": "原始内容",
    }, headers=headers)
    doc_id = resp_create.json()["id"]

    status_only = client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "status": "reviewing",
    }, headers=headers)
    assert status_only.status_code == 200
    assert status_only.json()["content"] == "原始内容"

    clear = client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": None,
    }, headers=headers)
    assert clear.status_code == 200
    assert clear.json()["content"] == ""


def test_document_version_word_count_ignores_whitespace():
    """Document and document version word_count should use the same counting logic."""
    headers = _auth_headers("doc_version_wc", "doc_version_wc")
    resp = client.post("/api/projects", json={"title": "文档字数测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]
    resp_create = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "字数测试", "position": 1,
    }, headers=headers)
    doc_id = resp_create.json()["id"]

    save = client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "A B C",
    }, headers=headers)
    assert save.status_code == 200
    assert save.json()["word_count"] == 3

    versions = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers).json()
    assert versions[0]["word_count"] == 3


def test_document_version_diff():
    """Document version diff should work."""
    headers = _auth_headers("doc_diff", "doc_diff")
    resp = client.post("/api/projects", json={"title": "文档Diff测试", "mode": "article"}, headers=headers)
    project_id = resp.json()["id"]

    # Create document and two versions
    resp_create = client.post(f"/api/projects/{project_id}/documents", json={
        "title": "Diff测试", "position": 1,
    }, headers=headers)
    doc_id = resp_create.json()["id"]

    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "旧内容。",
    }, headers=headers)
    client.patch(f"/api/projects/{project_id}/documents/{doc_id}", json={
        "content": "新内容。",
    }, headers=headers)

    # Get versions
    resp_versions = client.get(f"/api/projects/{project_id}/documents/{doc_id}/versions", headers=headers)
    versions = resp_versions.json()
    v1_id = versions[1]["id"]
    v2_id = versions[0]["id"]

    # Diff between two versions
    resp_diff = client.post(f"/api/projects/{project_id}/documents/{doc_id}/versions/diff", json={
        "version_id_a": v1_id,
        "version_id_b": v2_id,
    }, headers=headers)
    assert resp_diff.status_code == 200
    diff_data = resp_diff.json()
    assert diff_data["version_a"] == 1
    assert diff_data["version_b"] == 2
    assert len(diff_data["diff"]) > 0

    # Diff against current content
    resp_diff2 = client.post(f"/api/projects/{project_id}/documents/{doc_id}/versions/diff", json={
        "version_id_a": v1_id,
        "current_content": "当前内容。",
    }, headers=headers)
    assert resp_diff2.status_code == 200
    assert resp_diff2.json()["version_b"] == 0


def test_document_api_rejects_novel_project():
    """All Document API endpoints should reject novel projects with 400."""
    headers = _auth_headers("doc_novel", "doc_novel")
    resp = client.post("/api/projects", json={"title": "小说项目"}, headers=headers)
    project_id = resp.json()["id"]

    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/projects/{project_id}/documents", headers=headers).status_code == 400
    assert client.post(f"/api/projects/{project_id}/documents", json={"title": "x"}, headers=headers).status_code == 400
    assert client.get(f"/api/projects/{project_id}/documents/{fake_id}", headers=headers).status_code == 400
    assert client.patch(f"/api/projects/{project_id}/documents/{fake_id}", json={}, headers=headers).status_code == 400
    assert client.delete(f"/api/projects/{project_id}/documents/{fake_id}", headers=headers).status_code == 400


def test_chapter_api_still_works_for_novel():
    """Chapter API should still work normally for novel projects."""
    headers = _auth_headers("novel_chk", "novel_chk")
    resp = client.post("/api/projects", json={"title": "小说"}, headers=headers)
    project_id = resp.json()["id"]

    # Create chapter
    resp = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["sequence_number"] == 1

    # Update chapter
    resp2 = client.patch(f"/api/projects/{project_id}/chapters/1", json={
        "content": "小说内容。",
    }, headers=headers)
    assert resp2.status_code == 200

    # List chapters
    resp3 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    assert resp3.status_code == 200
    assert len(resp3.json()) == 1
