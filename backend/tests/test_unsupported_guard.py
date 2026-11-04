"""P0 安全边界测试：intent_unsupported 不得生成可执行任务卡。

P0-1：分类器标记 intent_unsupported → 消息接口强制 task_card=null、
      requires_confirmation=false，不进入目标解析/任务卡构建；
      绕过前端直接调 confirm → 422 code=intent_unsupported（不得执行）。
P0-2：任何多步计划（含全 rewrite："重写第一章然后重写第二章"）一律拒绝，
      不得偷偷压成单章任务卡静默丢失步骤。

全部走真实 API + mock LLM provider（消息接口集成测试）。
"""

import asyncio
import json
import uuid

import pytest

import test_smoke as smoke
from db.session import set_engine
from models.base import Base

_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory
client = smoke.client

_cached_auth: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _ensure_db():
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _auth_headers(username="unsup_user", password="unsup_pass"):
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _project_with_chapters(n: int):
    """建项目 + n 个章节（带正文，确保有正式版本）。返回 (pid, sid)。"""
    headers = _auth_headers()
    proj = client.post("/api/projects", json={"title": "安全边界", "mode": "novel"},
                       headers=headers).json()
    pid = proj["id"]
    for i in range(1, n + 1):
        client.post(f"/api/projects/{pid}/chapters",
                    json={"title": f"第{i}章", "sequence_number": i},
                    headers=headers).json()
        client.patch(f"/api/projects/{pid}/chapters/{i}",
                     json={"content": f"第{i}章正文，需要被修改的文本。"},
                     headers=headers).json()
    resp = client.post("/api/chat/sessions", json={"project_id": pid}, headers=headers)
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["session_id"]


class FakeProvider:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[str] = []

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        self.calls.append(user_prompt)
        return self.raw


def _patch_llm(monkeypatch, raw: str):
    provider = FakeProvider(raw)

    async def _fake_config(user_id, db):
        return {"provider": "mock", "api_key": "test"}

    monkeypatch.setattr("agents.llm_provider.get_llm_provider", lambda config: provider)
    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _fake_config)
    return provider


def _rewrite_json(**overrides):
    payload = {
        "intent": "rewrite",
        "operation": "rewrite_chapter",
        "target_scope": "explicit_chapter",
        "target_chapter_no": 1,
        "mentioned_entities": [],
        "requirements": ["按要求重写"],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _compound_plan_json(steps, intent="rewrite"):
    """构造带 plan 的 LLM 输出。steps: [{intent, raw, target_chapter_no}, ...]"""
    return json.dumps({
        "intent": intent,
        "operation": "rewrite_chapter",
        "target_scope": "explicit_chapter",
        "target_chapter_no": None,
        "mentioned_entities": [],
        "requirements": [],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": {
            "steps": steps,
            "dependencies": [{"from_step": i, "to_step": i + 1}
                             for i in range(1, len(steps))],
        },
    }, ensure_ascii=False)


# ── P0-1：intent_unsupported 不得生成可执行任务卡（API 集成） ──


def test_api_compound_unsupported_never_builds_task_card(monkeypatch):
    """knowledge+rewrite 复合任务：消息接口必须返回 task_card=null。"""
    _patch_llm(monkeypatch, _compound_plan_json([
        {"intent": "knowledge_query", "raw": "查穆白最后状态", "target_chapter_no": 1},
        {"intent": "rewrite", "raw": "修改第一章", "target_chapter_no": 1},
    ]))
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "查穆白最后状态然后修改第一章",
                             "idempotency_key": "unsup-1"},
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["intent_unsupported"] is True
    assert data["task_card"] is None          # 强制 null
    assert data["requires_confirmation"] is False
    assert data["clarification"] is None      # 已有 unsupported_reason，不再反问
    assert data["unsupported_reason"]
    assert data["reply"] == data["unsupported_reason"]  # 提示语直接可见


def test_api_unsupported_confirm_rejected(monkeypatch):
    """绕过前端直接 confirm 复合任务消息 → 422 code=intent_unsupported，不得执行。"""
    _patch_llm(monkeypatch, _compound_plan_json([
        {"intent": "knowledge_query", "raw": "查穆白最后状态", "target_chapter_no": 1},
        {"intent": "rewrite", "raw": "修改第一章", "target_chapter_no": 1},
    ]))
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "查穆白最后状态然后修改第一章",
                             "idempotency_key": "unsup-2"},
                       headers=_auth_headers())
    data = resp.json()
    mid = data["message_id"]

    confirm = client.post(f"/api/chat/sessions/{sid}/messages/{mid}/confirm",
                          json={}, headers=_auth_headers())
    assert confirm.status_code == 422, confirm.text
    body = confirm.json()
    assert body["code"] == "intent_unsupported"


def test_api_finalize_unsupported_never_builds_task_card():
    """定稿（gate 直达 unsupported）：不调 LLM 也返回无任务卡 + 明确原因。"""
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "可以定稿了",
                             "idempotency_key": "unsup-3"},
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["intent_unsupported"] is True
    assert data["task_card"] is None
    assert data["requires_confirmation"] is False
    assert "定稿" in data["unsupported_reason"]


def test_api_finalize_confirm_rejected():
    """定稿消息 confirm → 422，不得执行。"""
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "可以定稿了",
                             "idempotency_key": "unsup-4"},
                       headers=_auth_headers())
    mid = resp.json()["message_id"]

    confirm = client.post(f"/api/chat/sessions/{sid}/messages/{mid}/confirm",
                          json={}, headers=_auth_headers())
    assert confirm.status_code == 422, confirm.text
    assert confirm.json()["code"] == "intent_unsupported"


# ── P0-2：多步计划一律 unsupported（含全 rewrite 多步） ──


def test_llm_multi_rewrite_plan_is_unsupported(monkeypatch):
    """'重写第一章然后重写第二章'：全 rewrite 多步也拒绝，不得压成单章任务卡。"""
    _patch_llm(monkeypatch, _compound_plan_json([
        {"intent": "rewrite", "raw": "重写第一章", "target_chapter_no": 1},
        {"intent": "rewrite", "raw": "重写第二章", "target_chapter_no": 2},
    ]))
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "重写第一章然后重写第二章",
                             "idempotency_key": "unsup-5"},
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["intent_unsupported"] is True
    assert data["task_card"] is None
    assert data["requires_confirmation"] is False
    # 完整计划保留给前端展示（识别但拒绝）
    assert data["plan"] is not None
    assert len(data["plan"]["steps"]) == 2

    # confirm 同样被拒绝
    confirm = client.post(f"/api/chat/sessions/{sid}/messages/{data['message_id']}/confirm",
                          json={}, headers=_auth_headers())
    assert confirm.status_code == 422, confirm.text
    assert confirm.json()["code"] == "intent_unsupported"


def test_single_step_rewrite_still_executable(monkeypatch):
    """单步 rewrite（无 plan）不受影响：任务卡正常、confirm 可执行。"""
    _patch_llm(monkeypatch, _rewrite_json())
    _, sid = _project_with_chapters(3)

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "重写第一章",
                             "idempotency_key": "unsup-6"},
                       headers=_auth_headers())
    data = resp.json()
    assert data.get("intent_unsupported") is None or data["intent_unsupported"] is False
    assert data["task_card"] is not None
    assert data["requires_confirmation"] is True

    confirm = client.post(f"/api/chat/sessions/{sid}/messages/{data['message_id']}/confirm",
                          json={}, headers=_auth_headers())
    assert confirm.status_code == 200, confirm.text
