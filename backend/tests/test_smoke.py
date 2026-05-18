"""最小冒烟测试 — 使用 SQLite 内存数据库，无需 PostgreSQL"""

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
from models import Project, Chapter, Expert, WorldEntry, Character, User  # noqa: F401
from db.session import get_db, set_engine
from main import app

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
    resp = client.post("/api/projects", json={"title": "测试小说", "description": "一部测试小说", "target_words": 50000}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "测试小说"
    assert data["status"] == "active"
    assert data["target_words"] == 50000
    project_id = data["id"]

    resp2 = client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp2.status_code == 200

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


def test_writer_generate_persists_content():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 200

    resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    experts = resp3.json()
    writer_expert_id = experts[0]["id"]

    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue", "expert_id": writer_expert_id,
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text

    resp5 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    chapters = resp5.json()
    assert len(chapters) == 1
    assert chapters[0]["content"] is not None
    assert len(chapters[0]["content"]) > 0
    assert chapters[0]["word_count"] > 0


def test_critic_generate_does_not_persist():
    headers = _auth_headers()
    resp = client.post("/api/projects", json={"title": "critic落库测试"}, headers=headers)
    project_id = resp.json()["id"]

    resp2 = client.post(f"/api/projects/{project_id}/chapters", json={
        "title": "第一章", "sequence_number": 1,
    }, headers=headers)
    assert resp2.status_code == 200

    resp3 = client.get(f"/api/projects/{project_id}/experts", headers=headers)
    experts = resp3.json()
    critic_expert_id = experts[1]["id"]

    resp4 = client.post(f"/api/projects/{project_id}/chapters/generate", json={
        "chapter_num": 1, "mode": "continue", "expert_id": critic_expert_id,
    }, headers=headers)
    assert resp4.status_code == 200
    assert "event: done" in resp4.text

    resp5 = client.get(f"/api/projects/{project_id}/chapters", headers=headers)
    chapters = resp5.json()
    assert len(chapters) == 1
    assert chapters[0]["content"] is None


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


def test_export_project_not_found():
    headers = _auth_headers()
    resp = client.get("/api/projects/00000000-0000-0000-0000-000000000000/export?format=txt", headers=headers)
    assert resp.status_code == 404