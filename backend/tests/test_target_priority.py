"""目标章节四层优先级测试（方案决策 1，S3）。

显式章节号 > "本章/这章"指示词+当前章节 > LLM 推断 > 澄清。

关键边界（防 LLM 幻觉改错章）：
- LLM 返回不存在的目标 → 必须澄清，绝不偷偷回退当前章节
- "这章"指示词但无当前章节上下文 → 澄清（指示词优先，不采纳 LLM 目标）
- 无指示词时 context.chapter_no 不再无条件回退（旧行为）→ 澄清

全部走真实 API（POST /api/chat/sessions/{id}/messages）+ mock LLM provider，
验证四层在完整链路上生效。
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


def _auth_headers(username="target_user", password="target_pass"):
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _project_with_chapters(n: int):
    """建项目 + n 个章节（sequence_number 1..n）。返回 (pid, sid)。"""
    headers = _auth_headers()
    proj = client.post("/api/projects", json={"title": "目标优先级", "mode": "novel"},
                       headers=headers).json()
    pid = proj["id"]
    for i in range(1, n + 1):
        client.post(f"/api/projects/{pid}/chapters",
                    json={"title": f"第{i}章", "sequence_number": i},
                    headers=headers).json()
    resp = client.post("/api/chat/sessions", json={"project_id": pid}, headers=headers)
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["session_id"]


# ── LLM mock ──────────────────────────────────────────────


class FakeProvider:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[str] = []

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        self.calls.append(user_prompt)
        return self.raw


def _patch_llm(monkeypatch, target_no: int | None, intent="rewrite"):
    """mock LLM 主路径：返回带指定 target_chapter_no 的 rewrite 输出。"""
    provider = FakeProvider(json.dumps({
        "intent": intent,
        "operation": "rewrite_chapter",
        "target_scope": "explicit_chapter" if target_no is not None else "none",
        "target_chapter_no": target_no,
        "mentioned_entities": [],
        "requirements": ["按要求重写"],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": None,
    }, ensure_ascii=False))

    async def _fake_config(user_id, db):
        return {"provider": "mock", "api_key": "test"}

    monkeypatch.setattr("agents.llm_provider.get_llm_provider", lambda config: provider)
    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _fake_config)
    return provider


def _send(sid, content, context=None, key=None):
    body = {"content": content, "idempotency_key": key or f"k-{uuid.uuid4().hex[:8]}"}
    if context is not None:
        body["context"] = context
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json=body,
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 1. 显式章节号 > LLM 推断 ───────────────────────────────


def test_explicit_chapter_beats_llm_target(monkeypatch):
    _, sid = _project_with_chapters(3)
    _patch_llm(monkeypatch, target_no=5)  # LLM 幻觉说第 5 章

    data = _send(sid, "重写第三章")
    card = data["task_card"]
    assert card is not None
    assert card["target"]["chapter_no"] == 3  # 显式号优先，LLM 不得覆盖


# ── 2. "本章/这章"指示词 > LLM 推断 ─────────────────────────


def test_this_chapter_beats_llm_target(monkeypatch):
    _, sid = _project_with_chapters(5)
    _patch_llm(monkeypatch, target_no=5)

    data = _send(sid, "重写这章", context={"chapter_no": 2})
    card = data["task_card"]
    assert card is not None
    assert card["target"]["chapter_no"] == 2


def test_this_chapter_without_context_asks_clarification(monkeypatch):
    # 指示词优先：有指示词但无当前章节 → 澄清，不采纳 LLM 目标
    _, sid = _project_with_chapters(5)
    _patch_llm(monkeypatch, target_no=3)

    data = _send(sid, "重写这章")
    assert data["clarification"] is not None
    assert data["task_card"] is None


# ── 3. LLM 推断（无显式号、无指示词） ───────────────────────


def test_llm_target_used_when_no_explicit_or_deictic(monkeypatch):
    _, sid = _project_with_chapters(5)
    _patch_llm(monkeypatch, target_no=3)

    data = _send(sid, "帮我重写一下")
    card = data["task_card"]
    assert card is not None
    assert card["target"]["chapter_no"] == 3


# ── 4. LLM 幻觉目标 → 澄清，绝不回退当前章节 ───────────────


def test_llm_phantom_target_asks_clarification_not_fallback(monkeypatch):
    _, sid = _project_with_chapters(5)
    _patch_llm(monkeypatch, target_no=99)  # 不存在

    data = _send(sid, "帮我重写一下", context={"chapter_no": 2})
    assert data["clarification"] is not None
    assert data["task_card"] is None
    # 澄清文案指向目标章节
    assert data["clarification"]["needs_clarification"] is True


# ── 5. 显式章节号不存在 → 澄清（不采纳 LLM 目标） ───────────


def test_explicit_phantom_chapter_asks_clarification(monkeypatch):
    _, sid = _project_with_chapters(3)
    _patch_llm(monkeypatch, target_no=2)

    data = _send(sid, "重写第 99 章")
    assert data["clarification"] is not None
    assert data["task_card"] is None


# ── 6. 全部缺失 → 澄清 ─────────────────────────────────────


def test_no_target_at_all_asks_clarification(monkeypatch):
    _, sid = _project_with_chapters(3)
    _patch_llm(monkeypatch, target_no=None)

    data = _send(sid, "帮我重写一下吧")
    assert data["clarification"] is not None
    assert data["task_card"] is None


# ── 7. 规则降级路径（无 LLM） ───────────────────────────────


def test_rule_path_this_chapter_with_context(monkeypatch):
    """无 Key 降级：'重写这章' + 当前章节 → 用当前章节。"""
    def _boom(user_id, db):
        raise RuntimeError("未配置 LLM Key")

    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _boom)
    _, sid = _project_with_chapters(5)

    data = _send(sid, "重写这章", context={"chapter_no": 4})
    card = data["task_card"]
    assert card is not None
    assert card["target"]["chapter_no"] == 4


def test_rule_path_no_deictic_no_fallback_to_context(monkeypatch):
    """无 Key 降级：无显式号、无指示词时，即使有当前章节也不得偷偷回退（旧行为）→ 澄清。"""
    def _boom(user_id, db):
        raise RuntimeError("未配置 LLM Key")

    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _boom)
    _, sid = _project_with_chapters(5)

    data = _send(sid, "帮我重写一下吧", context={"chapter_no": 4})
    assert data["clarification"] is not None
    assert data["task_card"] is None
