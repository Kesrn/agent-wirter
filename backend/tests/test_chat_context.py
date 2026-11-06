"""多轮上下文测试（补锁 3，P0-4）。

- _load_chat_history：最近 5 个 user-assistant 回合成对加载（先 5 条 user 再
  按 reply_to_id 补 assistant），跳过未完成占位消息，上限 10 条
- _load_confirmed_constraints：最近已确认任务卡的 resolved_instruction + requirements
- classify_with_llm：对话历史 / 工作对象（selection/current_paragraph）/
  已确认约束注入 LLM prompt（"按刚才第二种方案改"依赖前文解析）
- API 链路：schema 接受 selection/current_paragraph 并进入 prompt
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import test_smoke as smoke
from db.session import set_engine
from models import Project, User
from models.base import Base
from models.change_set import ChangeSet
from models.chat_conversation import ChatMessage, ChatSession
from services.conversation_service import (
    ChangeSetStatus,
    _load_chat_history,
    _load_confirmed_constraints,
)

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


def _auth_headers(username="ctx_user", password="ctx_pass"):
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _project_with_chapter():
    headers = _auth_headers()
    proj = client.post("/api/projects", json={"title": "多轮上下文", "mode": "novel"},
                       headers=headers).json()
    pid = proj["id"]
    client.post(f"/api/projects/{pid}/chapters",
                json={"title": "第一章", "sequence_number": 1}, headers=headers).json()
    client.patch(f"/api/projects/{pid}/chapters/1",
                 json={"content": "第一章正文。"}, headers=headers).json()
    resp = client.post("/api/chat/sessions", json={"project_id": pid}, headers=headers)
    return pid, resp.json()["session_id"]


class FakeProvider:
    def __init__(self, raw: str):
        self.raw = raw
        self.prompts: list[str] = []

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        self.prompts.append(user_prompt)
        return self.raw


def _rewrite_json():
    return json.dumps({
        "intent": "rewrite",
        "operation": "polish_chapter",
        "target_scope": "explicit_chapter",
        "target_chapter_no": 1,
        "mentioned_entities": ["穆白"],
        "requirements": ["加强穆白内心戏"],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": None,
    }, ensure_ascii=False)


def _patch_llm(monkeypatch, provider: FakeProvider):
    async def _fake_config(user_id, db):
        return {"provider": "mock", "api_key": "test"}

    monkeypatch.setattr("agents.llm_provider.get_llm_provider", lambda config: provider)
    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _fake_config)
    return provider


# ── _load_chat_history：5 回合成对加载 ──


def test_chat_history_pairs_and_skips_placeholders():
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"hist_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="历史", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)

            # 7 个回合：u1..u7 各带 assistant 回复；u7 的 assistant 是占位（空）。
            # created_at 显式递增（SQLite server_default 秒级精度，同秒排序不稳定）。
            base = asyncio.get_event_loop().time()
            for i in range(1, 8):
                ts = datetime.now(timezone.utc) + timedelta(seconds=i)
                um = ChatMessage(session_id=sid, role="user",
                                 content=f"第{i}条用户消息", idempotency_key=f"h{i}",
                                 created_at=ts)
                session.add(um)
                await session.flush()
                am = ChatMessage(session_id=sid, role="assistant",
                                 content="" if i == 7 else f"第{i}条回复",
                                 reply_to_id=str(um.id), created_at=ts,
                                 response_json=None if i == 7 else {"reply": f"第{i}条回复"})
                session.add(am)
                await session.flush()

            history = await _load_chat_history(session, sid)
            # 只取最近 5 回合（u3..u7），u7 占位 assistant 跳过
            contents = [t["content"] for t in history]
            assert contents == [
                "第3条用户消息", "第3条回复",
                "第4条用户消息", "第4条回复",
                "第5条用户消息", "第5条回复",
                "第6条用户消息", "第6条回复",
                "第7条用户消息",  # 占位 assistant 被跳过
            ]
            assert len(history) <= 10

    asyncio.run(_go())


def test_chat_history_empty():
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"histe_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="空历史", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            assert await _load_chat_history(session, str(chat.id)) == []

    asyncio.run(_go())


# ── _load_confirmed_constraints：已确认任务卡约束 ──


def test_confirmed_constraints_from_recent_execution():
    """P0-2：按真实消息链路——user 消息回填 execution_id=change_set.id，
    分类助手消息带 task_card 无 execution_id。"""
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"conf_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="约束", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)
            # 真实结构：原始 user 消息（confirm 后回填 execution_id=change_set.id）
            um = ChatMessage(session_id=sid, role="user", content="润色第一章")
            session.add(um)
            await session.flush()
            # 真实结构：分类助手消息（task_card，无 execution_id，reply_to=user 消息）
            am = ChatMessage(
                session_id=sid, role="assistant", content="收到修改指令",
                reply_to_id=str(um.id), execution_id=None,
                task_card={
                    "operation_type": "chapter_rewrite",
                    "resolved_instruction": "执行润色章节；重点处理：穆白",
                    "requirements": ["加强穆白内心戏"],
                },
            )
            session.add(am)
            # 真实结构：change_set（id 即消息上的 execution_id；execution_id 列恒 None）
            cs = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.PENDING,
            )
            session.add(cs)
            await session.flush()
            um.execution_id = str(cs.id)  # confirm 成功后的回填语义
            # 更早一条已回滚的执行不得覆盖
            cs0 = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.REVERTED,
            )
            session.add(cs0)
            await session.flush()
            um0 = ChatMessage(session_id=sid, role="user", content="旧消息",
                              idempotency_key="old")
            session.add(um0)
            await session.flush()
            um0.execution_id = str(cs0.id)
            await session.flush()

            constraints = await _load_confirmed_constraints(session, sid)
            assert "执行润色章节；重点处理：穆白" in constraints
            assert "加强穆白内心戏" in constraints

    asyncio.run(_go())


def test_confirmed_constraints_none():
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"confn_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="无约束", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            assert await _load_confirmed_constraints(session, str(chat.id)) == []

    asyncio.run(_go())


def test_confirmed_constraints_pending_retained():
    """验证 ChangeSet 处于 PENDING 状态时，任务卡约束仍能被正常加载。"""
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"conf_pend_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="Pending约束", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)

            um = ChatMessage(session_id=sid, role="user", content="重写第三章")
            session.add(um)
            await session.flush()

            am = ChatMessage(
                session_id=sid, role="assistant", content="已生成任务卡",
                reply_to_id=str(um.id), execution_id=None,
                task_card={
                    "operation_type": "chapter_rewrite",
                    "resolved_instruction": "修改第3章氛围",
                    "requirements": ["突出暴风雨压迫感"],
                },
            )
            session.add(am)

            cs = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.PENDING,
            )
            session.add(cs)
            await session.flush()
            um.execution_id = str(cs.id)
            await session.flush()

            constraints = await _load_confirmed_constraints(session, sid)
            assert "修改第3章氛围" in constraints
            assert "突出暴风雨压迫感" in constraints

    asyncio.run(_go())


def test_confirmed_constraints_applied_retained():
    """验证 ChangeSet 处于 APPLIED 状态时，任务卡约束能被正常加载。"""
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"conf_appl_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="Applied约束", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)

            um = ChatMessage(session_id=sid, role="user", content="定稿润色")
            session.add(um)
            await session.flush()

            am = ChatMessage(
                session_id=sid, role="assistant", content="已采纳变更",
                reply_to_id=str(um.id), execution_id=None,
                task_card={
                    "operation_type": "chapter_rewrite",
                    "resolved_instruction": "确认采纳新版对话",
                    "requirements": ["保持林风冷静性格"],
                },
            )
            session.add(am)

            cs = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.APPLIED,
            )
            session.add(cs)
            await session.flush()
            um.execution_id = str(cs.id)
            await session.flush()

            constraints = await _load_confirmed_constraints(session, sid)
            assert "确认采纳新版对话" in constraints
            assert "保持林风冷静性格" in constraints

    asyncio.run(_go())


def test_confirmed_constraints_reverted_and_failed_excluded():
    """验证 ChangeSet 处于 REVERTED 或 FAILED 时，即使有完整任务卡也不加载。"""
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"conf_excl_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="排除约束", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)

            # 1. 构造一个 REVERTED 的任务卡
            um_rev = ChatMessage(session_id=sid, role="user", content="被回滚的修改")
            session.add(um_rev)
            await session.flush()
            am_rev = ChatMessage(
                session_id=sid, role="assistant", content="回滚前任务卡",
                reply_to_id=str(um_rev.id), execution_id=None,
                task_card={
                    "operation_type": "chapter_rewrite",
                    "resolved_instruction": "幽灵指令：必须被排除",
                    "requirements": ["幽灵要求：不得生效"],
                },
            )
            session.add(am_rev)
            cs_rev = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.REVERTED,
            )
            session.add(cs_rev)
            await session.flush()
            um_rev.execution_id = str(cs_rev.id)

            # 2. 构造一个 FAILED 的任务卡
            um_fail = ChatMessage(session_id=sid, role="user", content="执行失败的修改")
            session.add(um_fail)
            await session.flush()
            am_fail = ChatMessage(
                session_id=sid, role="assistant", content="失败前任务卡",
                reply_to_id=str(um_fail.id), execution_id=None,
                task_card={
                    "operation_type": "chapter_rewrite",
                    "resolved_instruction": "失败指令：必须被排除",
                    "requirements": ["失败要求：不得生效"],
                },
            )
            session.add(am_fail)
            cs_fail = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.FAILED,
            )
            session.add(cs_fail)
            await session.flush()
            um_fail.execution_id = str(cs_fail.id)
            await session.flush()

            # 断言：二者均被过滤，结果必须为空列表
            constraints = await _load_confirmed_constraints(session, sid)
            assert constraints == []

    asyncio.run(_go())


# ── LLM prompt 注入 ──


def test_llm_prompt_injects_history_workspace_and_constraints(monkeypatch):
    provider = FakeProvider(_rewrite_json())
    _patch_llm(monkeypatch, provider)

    from services.intent_classifier import classify_with_llm

    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}
    result = asyncio.run(classify_with_llm(
        "u1", "按刚才第二种方案改", None, rule_result,
        chat_history=[
            {"role": "user", "content": "润色第三章，加强穆白内心戏"},
            {"role": "assistant", "content": "收到修改指令。我准备了一张任务卡"},
        ],
        workspace_context={
            "chapter_no": 3, "chapter_title": "第三章 觉醒",
            "current_version_id": "ver-9",
            "selection": "穆白攥紧拳头", "current_paragraph": "穆白攥紧拳头，望向窗外。",
        },
        confirmed_constraints=["执行润色章节；加强穆白内心戏"],
    ))

    assert result["llm_used"] is True
    prompt = provider.prompts[0]
    # 对话历史注入
    assert "## 对话历史（最近 5 个回合）" in prompt
    assert "用户：润色第三章，加强穆白内心戏" in prompt
    assert "助手：收到修改指令" in prompt
    # 工作对象注入（含选中文本/当前段落）
    assert "## 当前工作对象" in prompt
    assert "第3章" in prompt
    assert "用户选中的文本：穆白攥紧拳头" in prompt
    assert "光标所在段落" in prompt
    # 已确认约束注入
    assert "## 已确认的修改约束" in prompt
    assert "执行润色章节；加强穆白内心戏" in prompt
    # 当前消息
    assert "用户消息：按刚才第二种方案改" in prompt


def test_llm_prompt_empty_context_omits_sections(monkeypatch):
    provider = FakeProvider(_rewrite_json())
    _patch_llm(monkeypatch, provider)

    from services.intent_classifier import classify_with_llm

    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}
    asyncio.run(classify_with_llm("u1", "重写第三章", None, rule_result))

    prompt = provider.prompts[0]
    assert "## 对话历史" not in prompt
    assert "## 当前工作对象" not in prompt
    assert "## 已确认的修改约束" not in prompt
    assert "用户消息：重写第三章" in prompt


# ── API 链路：schema 接受 selection/current_paragraph ──


def test_api_context_accepts_selection_and_paragraph(monkeypatch):
    provider = FakeProvider(_rewrite_json())
    _patch_llm(monkeypatch, provider)
    _, sid = _project_with_chapter()

    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={
                           "content": "改一下这段",
                           "idempotency_key": "ctx-1",
                           "context": {
                               "chapter_no": 1,
                               "chapter_title": "第一章",
                               "selection": "穆白攥紧拳头",
                               "current_paragraph": "穆白攥紧拳头，望向窗外。",
                           },
                       },
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["intent"] == "rewrite"
    assert data["task_card"] is not None

    prompt = provider.prompts[0]
    assert "用户选中的文本：穆白攥紧拳头" in prompt
    assert "光标所在段落" in prompt


# ── P0-2：真实闭环——发送 → 确认 → 读已确认约束 ──


def test_confirmed_constraints_real_flow_roundtrip(monkeypatch):
    """真实 API 闭环：发消息（任务卡）→ confirm（执行）→ _load_confirmed_constraints
    必须取到 resolved_instruction + requirements（不得返回空数组）。"""
    provider = FakeProvider(_rewrite_json())
    _patch_llm(monkeypatch, provider)
    _, sid = _project_with_chapter()

    # 1) 发送消息 → 任务卡（LLM 解析字段：resolved_instruction + requirements）
    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "润色一下第一章，重点写穆白",
                             "idempotency_key": "rt-1"},
                       headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    card = data["task_card"]
    assert card is not None
    assert card.get("resolved_instruction")
    assert card.get("requirements")

    # 2) 确认任务卡 → 执行（change_set 落库）
    confirm = client.post(f"/api/chat/sessions/{sid}/messages/{data['message_id']}/confirm",
                          json={}, headers=_auth_headers())
    assert confirm.status_code == 200, confirm.text
    execution_id = confirm.json()["execution_id"]

    # 3) 再读已确认约束：必须取到任务卡里的 resolved_instruction + requirements
    def _fetch():
        async def _go():
            async with _test_session_factory() as session:
                return await _load_confirmed_constraints(session, sid)
        return asyncio.run(_go())

    constraints = _fetch()
    assert constraints, "真实闭环必须取到已确认约束"
    assert card["resolved_instruction"] in constraints
    assert card["requirements"][0] in constraints
    assert execution_id  # 执行确实发生
