"""resolved_instruction 传导测试（方案补锁 2，S4+S5）。

S4：任务卡携带 LLM 解析字段——resolved_instruction / operation /
     mentioned_entities / requirements / router_source / prompt_version，
     原始 user_instruction 保留便于追溯。
S5：生成候选稿时 prompt 指令优先级 resolved_instruction > 原始消息
     （验证 GenerationRecord.user_note + request_params 溯源）。
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

import test_smoke as smoke
from db.session import set_engine
from models.base import Base
from models.generation_record import GenerationRecord

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


def _auth_headers(username="ins_user", password="ins_pass"):
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
    proj = client.post("/api/projects", json={"title": "指令传导", "mode": "novel"},
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


def _send(sid, content, key=None):
    body = {"content": content, "idempotency_key": key or f"k-{uuid.uuid4().hex[:8]}"}
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json=body, headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rewrite_json(**overrides):
    payload = {
        "intent": "rewrite",
        "operation": "polish_chapter",
        "target_scope": "explicit_chapter",
        "target_chapter_no": 1,
        "mentioned_entities": ["穆白"],
        "requirements": ["加强穆白内心戏", "保持人物关系不变"],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── S4：resolved_instruction 进入任务卡 ─────────────────────


def test_task_card_carries_llm_parsed_fields(monkeypatch):
    provider = _patch_llm(monkeypatch, _rewrite_json())
    _, sid = _project_with_chapters(3)

    data = _send(sid, "润色一下第三章，重点写穆白")
    card = data["task_card"]
    assert card is not None

    # LLM 解析字段
    assert card["router_source"] == "llm"
    assert card["prompt_version"] == "intent-router-v2"
    assert card["operation"] == "polish_chapter"
    assert card["mentioned_entities"] == ["穆白"]
    assert card["requirements"] == ["加强穆白内心戏", "保持人物关系不变"]
    # 方案 A 模板组装
    assert card["resolved_instruction"] is not None
    assert "执行润色章节" in card["resolved_instruction"]
    assert "穆白" in card["resolved_instruction"]
    assert "加强穆白内心戏" in card["resolved_instruction"]
    assert "保持人物关系不变" in card["resolved_instruction"]
    # 原始消息保留，便于追溯
    assert card["constraints"]["user_instruction"] == "润色一下第三章，重点写穆白"
    assert provider.calls  # LLM 主路径确实被调用


def test_task_card_rule_path_marks_router_source(monkeypatch):
    """无 Key 规则降级：router_source=rule，无 LLM 解析字段。"""
    async def _no_key(user_id, db):
        raise RuntimeError("未配置 LLM Key")

    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _no_key)
    _, sid = _project_with_chapters(3)

    data = _send(sid, "重写第三章")
    card = data["task_card"]
    assert card is not None
    assert card["router_source"] == "rule"
    assert card["prompt_version"] == "intent-router-v2"
    assert card.get("resolved_instruction") is None
    assert card["constraints"]["user_instruction"] == "重写第三章"


def test_task_card_llm_without_operation_no_resolved(monkeypatch):
    """LLM 无 operation（如纯 discussion）不生成任务卡；rewrite 但无 operation →
    resolved_instruction=None，生成时回退原始消息。"""
    provider = _patch_llm(monkeypatch, _rewrite_json(operation=None, requirements=[]))
    _, sid = _project_with_chapters(3)

    data = _send(sid, "重写第三章")
    card = data["task_card"]
    assert card is not None
    assert card.get("resolved_instruction") is None  # 无 operation → 模板无法组装
    assert card["constraints"]["user_instruction"] == "重写第三章"


def test_task_card_context_snapshot(monkeypatch):
    """P1-2：context_snapshot 记录 AI 理解时的工作对象上下文（含选中文本/当前段落）。"""
    provider = _patch_llm(monkeypatch, _rewrite_json())
    _, sid = _project_with_chapters(3)

    body = {
        "content": "润色一下第三章，重点写穆白",
        "idempotency_key": f"k-{uuid.uuid4().hex[:8]}",
        "context": {
            "chapter_no": 3,
            "selection": "穆白攥紧拳头",
            "current_paragraph": "穆白攥紧拳头，望向窗外。",
        },
    }
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json=body,
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    card = resp.json()["task_card"]

    snap = card["context_snapshot"]
    assert snap["chapter_no"] == 3
    assert snap["selected_text"] == "穆白攥紧拳头"
    assert snap["current_paragraph"] == "穆白攥紧拳头，望向窗外。"
    assert provider.calls


def _fetch_record(execution_id: str):
    def _go():
        async def _inner():
            async with _test_session_factory() as session:
                result = await session.execute(
                    select(GenerationRecord).where(
                        GenerationRecord.task_card_id == execution_id
                    )
                )
                return result.scalars().first()
        return asyncio.run(_inner())
    return _go()


# ── S5：resolved_instruction 进入最终生成 Prompt ────────────


def test_candidate_uses_resolved_instruction(monkeypatch):
    """confirm 后生成候选稿：指令取 resolved_instruction（非原始消息）。"""
    _patch_llm(monkeypatch, _rewrite_json())
    _, sid = _project_with_chapters(3)

    data = _send(sid, "润色一下第三章，重点写穆白")
    card = data["task_card"]
    resolved = card["resolved_instruction"]
    assert resolved and "执行润色章节" in resolved

    mid = data["message_id"]
    resp = client.post(f"/api/chat/sessions/{sid}/messages/{mid}/confirm",
                       json={}, headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    execution_id = resp.json()["execution_id"]

    record = _fetch_record(execution_id)
    assert record is not None
    # user_note = 解析后的具体指令（resolved_instruction 优先）
    assert record.user_note == resolved
    # 存根溯源：request_params 携带完整任务卡（router_source/prompt_version/resolved_instruction）
    params = record.request_params or {}
    stored_card = params.get("task_card") or {}
    assert stored_card.get("prompt_version") == "intent-router-v2"
    assert stored_card.get("router_source") == "llm"
    assert stored_card.get("resolved_instruction") == resolved


def test_candidate_falls_back_to_raw_message(monkeypatch):
    """无 resolved_instruction（规则降级路径）：生成指令回退原始消息。"""
    # LLM 输出非法 → 严格模型拒绝 → 规则降级；provider 本身可用（confirm 能生成）
    _patch_llm(monkeypatch, "这不是合法 JSON，走规则降级")
    _, sid = _project_with_chapters(3)

    data = _send(sid, "重写第三章，加强冲突")
    card = data["task_card"]
    assert card.get("resolved_instruction") is None
    assert card["router_source"] == "rule"

    mid = data["message_id"]
    resp = client.post(f"/api/chat/sessions/{sid}/messages/{mid}/confirm",
                       json={}, headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    execution_id = resp.json()["execution_id"]

    record = _fetch_record(execution_id)
    assert record is not None
    assert record.user_note == "重写第三章，加强冲突"  # 回退原始消息
    assert (record.request_params or {}).get("task_card", {}).get("router_source") == "rule"
