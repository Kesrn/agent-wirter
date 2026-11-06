"""对话式创作（chat）后端契约测试 —— SQLite 内存 + TestClient。

覆盖方案 §12 契约（全部幂等）：
1. POST /api/chat/sessions                       get-or-create 会话
2. POST /api/chat/sessions/{id}/messages         先回意图不执行（knowledge_query/discussion/rewrite/flow_control/finalize）
3. POST /api/chat/sessions/{id}/messages/{mid}/confirm   确认任务卡 → 生成候选稿（不覆盖正文）
4. POST /api/chat/executions/{id}/candidate/accept       版本冲突 409 version_conflict + 同事务写版本与 change_set
5. POST /api/chat/executions/{id}/candidate/discard      丢弃（不写正式数据）
6. POST /api/chat/executions/{id}/candidate/revise       继续修改（重新生成候选稿）
7. POST /api/chat/change-sets/{id}/revert                撤销
8. POST /api/chat/change-sets/revert-session             按时间逆序回滚

§14 强制约束断言：base_version_id 指向正式版本；idempotency_key 幂等；
confirm/accept/discard/revert 幂等；change_set 与版本写入同一事务。
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.base import Base
from models.change_set import ChangeSet
from models.chat_conversation import ChatMessage, ChatSession
from models.chapter_version import ChapterVersion
from models.generation_record import GenerationRecord

# 顶层导入（pytest 无 tests/__init__.py，按顶层模块收集）——若用
# `import tests.test_smoke` 会生成第二个模块副本（新内存引擎 + 重新覆盖 app
# 的 get_db override），同进程里先跑 smoke 会全部 "no such table" 污染。
import test_smoke as smoke
from db.session import set_engine
from services.intent_classifier import classify_rule

_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory
client = smoke.client


@pytest.fixture(autouse=True)
def _ensure_db():
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_cached_auth: dict[str, str] = {}


def _auth_headers(username="chatuser", password="chatpass"):
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _create_project_with_chapter(headers, content=None):
    """建项目 + 章节 + 可选正文。返回 (project_id, chapter_id, chapter_no)。"""
    proj = client.post("/api/projects", json={"title": "对话测试", "mode": "novel"},
                       headers=headers).json()
    pid = proj["id"]
    ch = client.post(f"/api/projects/{pid}/chapters",
                     json={"title": "第一章", "sequence_number": 1},
                     headers=headers).json()
    cid = ch["id"]
    if content:
        patched = client.patch(f"/api/projects/{pid}/chapters/1",
                               json={"content": content}, headers=headers).json()
        assert patched["content"] == content
    return pid, cid


def _chat_session(headers, pid):
    resp = client.post("/api/chat/sessions", json={"project_id": pid}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _latest_version_id(pid, cid):
    async def _go():
        async with _test_session_factory() as session:
            result = await session.execute(
                select(ChapterVersion.id)
                .where(ChapterVersion.chapter_id == str(cid))
                .order_by(ChapterVersion.version_number.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
    return asyncio.run(_go())


# ==================== 意图分流（规则骨架） ====================

def test_classify_rule_knowledge_query():
    result = classify_rule("穆白最后出现在哪里？")
    assert result["intent"] == "knowledge_query"
    assert result["plan"] is None
    assert result["requires_confirmation"] is False


def test_classify_rule_discussion():
    result = classify_rule("你觉得这一章节奏是不是太慢了？")
    assert result["intent"] == "discussion"


def test_classify_rule_rewrite():
    result = classify_rule("压缩第十章前半段")
    assert result["intent"] == "rewrite"
    assert result["plan"] is None


def test_classify_rule_rewrite_common_phrases():
    # 修复：日常口语改写指令此前误判 discussion（"写/开头/结尾/段"不在关键词表）
    for text in ("帮我写个开头", "写个开头", "帮我写一段", "写个结尾", "续写一段"):
        assert classify_rule(text)["intent"] == "rewrite", text


def test_classify_rule_rewrite_beats_weak_knowledge():
    # 修复：knowledge 弱词（最后/什么/谁）不得劫持 rewrite 指令——
    # "删掉最后一段"是修改，不是查询
    for text in ("删掉最后一段", "修改最后一段", "最后一段删掉"):
        assert classify_rule(text)["intent"] == "rewrite", text


def test_classify_rule_strong_knowledge_beats_rewrite():
    # 强知识词（查/搜索）在无修改词时仍判查询；"搜一下世界观"此前漏判 discussion
    for text in ("搜一下世界观", "查一下人物设定", "资料在哪"):
        assert classify_rule(text)["intent"] == "knowledge_query", text


def test_classify_rule_compound_task():
    result = classify_rule("查一下穆白最后的状态，然后修改第十章")
    assert result["intent"] == "rewrite"
    assert result["plan"] is not None
    steps = result["plan"]["steps"]
    assert [s["intent"] for s in steps] == ["knowledge_query", "rewrite"]
    assert result["plan"]["dependencies"] == [{"step": 2, "depends_on": [1]}]


def test_classify_rule_flow_control_finalize_unsupported():
    result = classify_rule("定稿")
    assert result["intent"] == "flow_control"
    assert result.get("intent_unsupported") is True


def test_classify_rule_flow_control_revert():
    result = classify_rule("撤销刚才的修改")
    assert result["intent"] == "flow_control"


# ==================== 会话 ====================

def test_chat_session_get_or_create():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers, content="第一章正文内容")
    sid1 = _chat_session(headers, pid)
    resp = client.post("/api/chat/sessions", json={"project_id": pid}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid1  # get-or-create：同一会话


def test_chat_session_requires_owner():
    headers = _auth_headers("owner1", "owner1pass")
    pid, _ = _create_project_with_chapter(headers)
    sid = _chat_session(headers, pid)
    other = _auth_headers("otherowner", "otherown")
    resp = client.get(f"/api/chat/sessions/{sid}", headers=other)
    assert resp.status_code == 404  # 跨用户不可见


# ==================== 消息分类（先回意图，不执行） ====================

def test_message_knowledge_query_returns_intent():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers)
    sid = _chat_session(headers, pid)
    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "穆白最后出现在哪里？", "idempotency_key": "kq-1"},
                       headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "knowledge_query"
    assert data["plan"] is None
    assert data["requires_confirmation"] is False
    assert data["task_card"] is None


def test_message_rewrite_returns_task_card_and_requires_confirmation():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers, content="第一章正文，需要被修改的文本。")
    sid = _chat_session(headers, pid)
    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "重写第一章", "idempotency_key": "rw-1"},
                       headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "rewrite"
    assert data["requires_confirmation"] is True
    card = data["task_card"]
    assert card is not None
    assert card["operation_type"] == "chapter_rewrite"
    assert card["target"]["chapter_no"] == 1
    assert card["risk_level"] == "L2"


def test_message_rewrite_without_target_asks_clarification():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers)
    sid = _chat_session(headers, pid)
    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "帮我重写一下吧", "idempotency_key": "rw-2"},
                       headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "rewrite"
    assert data["task_card"] is None
    assert data["clarification"] is not None  # 必须反问，不得猜


def test_message_finalize_returns_intent_unsupported():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers)
    sid = _chat_session(headers, pid)
    resp = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "定稿", "idempotency_key": "fin-1"},
                       headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "flow_control"
    assert data.get("intent_unsupported") is True


def test_message_idempotent_by_key():
    headers = _auth_headers()
    pid, _ = _create_project_with_chapter(headers, content="第一章正文")
    sid = _chat_session(headers, pid)
    first = client.post(f"/api/chat/sessions/{sid}/messages",
                        json={"content": "重写第一章", "idempotency_key": "idem-1"},
                        headers=headers).json()
    second = client.post(f"/api/chat/sessions/{sid}/messages",
                         json={"content": "重写第一章", "idempotency_key": "idem-1"},
                         headers=headers).json()
    assert second == first  # 保存首次响应结果，重复请求直接返回
    assert second["message_id"] == first["message_id"]


# ==================== 确认 → 生成候选稿 ====================

def _confirm_task(headers, sid, message_id, **kwargs):
    body = {"confirmed": True}
    body.update(kwargs)
    return client.post(f"/api/chat/sessions/{sid}/messages/{message_id}/confirm",
                       json=body, headers=headers)


def _full_rewrite_flow(headers, content="第一章原始正文。需要重写的内容。"):
    """发起 rewrite → 确认 → 返回 (pid, cid, sid, message_id, confirm_resp)。"""
    pid, cid = _create_project_with_chapter(headers, content=content)
    sid = _chat_session(headers, pid)
    msg = client.post(f"/api/chat/sessions/{sid}/messages",
                      json={"content": "重写第一章", "idempotency_key": str(uuid.uuid4())},
                      headers=headers).json()
    assert msg["task_card"] is not None
    confirm = _confirm_task(headers, sid, msg["message_id"])
    assert confirm.status_code == 200, confirm.text
    return pid, cid, sid, msg["message_id"], confirm.json()


def test_confirm_generates_candidate_without_overwriting_chapter():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    assert confirm["status"] == "pending"
    candidate = confirm["candidate"]
    assert candidate is not None
    assert candidate["content"]
    execution_id = confirm["execution_id"]

    # 候选稿落 generation_records（status=candidate），带 conversation/base/task_card 列
    async def _check():
        async with _test_session_factory() as session:
            rec = (await session.execute(
                select(GenerationRecord).where(GenerationRecord.id == candidate["record_id"])
            )).scalar_one()
            assert rec.status == "candidate"
            assert str(rec.conversation_id) == sid
            assert str(rec.base_version_id) == candidate["base_version_id"]
            assert str(rec.task_card_id) == execution_id
            assert str(rec.chapter_id) == cid
            # base_version_id 必须指向正式版本（ChapterVersion），不能指向候选稿
            ver = (await session.execute(
                select(ChapterVersion).where(ChapterVersion.id == candidate["base_version_id"])
            )).scalar_one()
            assert ver.content == "第一章原始正文。需要重写的内容。"
    asyncio.run(_check())

    # 正式正文未被覆盖
    chapter = client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()
    assert chapter["content"] == "第一章原始正文。需要重写的内容。"


def test_confirm_idempotent_with_key():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    key = str(uuid.uuid4())
    resp1 = _confirm_task(headers, sid, mid, idempotency_key=key)
    assert resp1.status_code == 200
    data1 = resp1.json()
    # 同幂等号重复确认 → 返回首次执行响应，且不重复生成候选稿
    resp2 = _confirm_task(headers, sid, mid, idempotency_key=key)
    assert resp2.status_code == 200
    assert resp2.json() == data1

    async def _count():
        async with _test_session_factory() as session:
            recs = (await session.execute(
                select(GenerationRecord).where(GenerationRecord.task_card_id == data1["execution_id"])
            )).scalars().all()
            return len(recs)
    assert asyncio.run(_count()) == 1


# ==================== 采用（版本冲突 + 同事务） ====================

def test_accept_writes_version_and_change_set_in_same_tx():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    candidate_content = confirm["candidate"]["content"]

    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["change_set_id"] == execution_id
    assert data["new_version_id"]

    # 版本已写入，change_set applied，generation record applied，三者一致
    async def _check():
        async with _test_session_factory() as session:
            cs = (await session.execute(
                select(ChangeSet).where(ChangeSet.id == execution_id)
            )).scalar_one()
            assert cs.status == "applied"
            assert cs.objects[0]["after_ref"] == data["new_version_id"]
            assert cs.objects[0]["before_ref"] == confirm["candidate"]["base_version_id"]
            ver = (await session.execute(
                select(ChapterVersion).where(ChapterVersion.id == data["new_version_id"])
            )).scalar_one()
            assert ver.content == candidate_content
            rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == confirm["candidate"]["record_id"])
            )).scalar_one()
            assert rec.status == "applied"
            assert str(rec.accepted_version_id) == data["new_version_id"]
    asyncio.run(_check())

    # 正式正文已更新
    chapter = client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()
    assert chapter["content"] == candidate_content


def test_accept_version_conflict_returns_409():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]

    # 手动修改章节 → 当前版本变化，不等于 base_version_id
    client.patch(f"/api/projects/{pid}/chapters/1",
                 json={"content": "用户手动修改后的最新正文。"}, headers=headers)

    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    assert resp.status_code == 409
    data = resp.json()
    assert data["code"] == "version_conflict"
    assert data["latest_version_id"] is not None

    # 冲突时不得写任何东西
    async def _check():
        async with _test_session_factory() as session:
            cs = (await session.execute(
                select(ChangeSet).where(ChangeSet.id == execution_id)
            )).scalar_one()
            assert cs.status == "pending"  # 未被标记 applied
            rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == confirm["candidate"]["record_id"])
            )).scalar_one()
            assert rec.status == "candidate"
    asyncio.run(_check())


def test_accept_idempotent_returns_current_state():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    first = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    assert first.status_code == 200
    second = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    assert second.status_code == 200
    assert second.json()["change_set_id"] == first.json()["change_set_id"]
    assert second.json()["new_version_id"] == first.json()["new_version_id"]

    async def _count_versions():
        async with _test_session_factory() as session:
            versions = (await session.execute(
                select(ChapterVersion).where(ChapterVersion.chapter_id == str(cid))
            )).scalars().all()
            return len(versions)
    # 只写了 1 个 manual（PATCH）+ 1 个 ai_enhance（accept）= 2，不重复写
    assert asyncio.run(_count_versions()) == 2


# ==================== 丢弃 ====================

def test_discard_does_not_write_formal_data():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]

    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/discard", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "discarded"

    # 正式正文未变；generation record 标 discarded
    chapter = client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()
    assert chapter["content"] == "第一章原始正文。需要重写的内容。"

    async def _check():
        async with _test_session_factory() as session:
            rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == confirm["candidate"]["record_id"])
            )).scalar_one()
            assert rec.status == "discarded"
    asyncio.run(_check())


def test_discard_idempotent():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    first = client.post(f"/api/chat/executions/{execution_id}/candidate/discard", headers=headers)
    second = client.post(f"/api/chat/executions/{execution_id}/candidate/discard", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "discarded"


def test_accept_after_discard_returns_409():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    client.post(f"/api/chat/executions/{execution_id}/candidate/discard", headers=headers)
    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "already_discarded"


# ==================== 继续修改（revise） ====================

def test_revise_regenerates_candidate():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    old_record_id = confirm["candidate"]["record_id"]

    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/revise",
                       json={"revise_instruction": "把结尾改得更紧张"}, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["candidate"]["record_id"] != old_record_id

    # 正式正文仍未变
    chapter = client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()
    assert chapter["content"] == "第一章原始正文。需要重写的内容。"


# ==================== change_set 撤销 ====================

def test_revert_applied_restores_base_version():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    base_version_id = confirm["candidate"]["base_version_id"]
    client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)

    resp = client.post(f"/api/chat/change-sets/{execution_id}/revert", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "reverted"

    # 正文恢复到 base 版本快照
    chapter = client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()
    assert chapter["content"] == "第一章原始正文。需要重写的内容。"

    async def _check():
        async with _test_session_factory() as session:
            base = (await session.execute(
                select(ChapterVersion).where(ChapterVersion.id == base_version_id)
            )).scalar_one()
            assert base.content == chapter["content"]
            cs = (await session.execute(
                select(ChangeSet).where(ChangeSet.id == execution_id)
            )).scalar_one()
            assert cs.status == "reverted"
            assert cs.reverted_at is not None
    asyncio.run(_check())


def test_revert_idempotent():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=headers)
    first = client.post(f"/api/chat/change-sets/{execution_id}/revert", headers=headers)
    second = client.post(f"/api/chat/change-sets/{execution_id}/revert", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "reverted"


def test_revert_session_rolls_back_all_in_reverse():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    exec1 = confirm["execution_id"]
    client.post(f"/api/chat/executions/{exec1}/candidate/accept", headers=headers)

    # 同一会话再来一轮修改
    msg2 = client.post(f"/api/chat/sessions/{sid}/messages",
                       json={"content": "重写第一章", "idempotency_key": str(uuid.uuid4())},
                       headers=headers).json()
    confirm2 = _confirm_task(headers, sid, msg2["message_id"])
    exec2 = confirm2.json()["execution_id"]
    client.post(f"/api/chat/executions/{exec2}/candidate/accept", headers=headers)
    assert chapter_content(headers, pid) == confirm2.json()["candidate"]["content"]

    resp = client.post("/api/chat/change-sets/revert-session",
                       params={"session_id": sid}, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reverted_count"] == 2

    # 正文恢复到最早 base 版本
    assert chapter_content(headers, pid) == "第一章原始正文。需要重写的内容。"


def chapter_content(headers, pid):
    return client.get(f"/api/projects/{pid}/chapters/1", headers=headers).json()["content"]


# ==================== 归属校验 ====================

def test_execution_cross_user_forbidden():
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    execution_id = confirm["execution_id"]
    other = _auth_headers("chatother", "chatotherpass")
    resp = client.post(f"/api/chat/executions/{execution_id}/candidate/accept", headers=other)
    assert resp.status_code == 404  # 不泄露存在性


def test_confirm_regenerate_with_new_base_version():
    """修复 B：同 message 带新 base_version_id 重新确认 → 真正基于新版本重新生成。

    验证：accept 生成新正式版本后，再次 confirm 同 message + 新 base_version_id
    + 新幂等键 → 不得返回首次执行响应（幂等短路被跳过），返回新候选且
    base_version_id 指向新版本。
    """
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    first_execution = confirm["execution_id"]
    first_base = confirm["candidate"]["base_version_id"]

    # 采用 → 生成新正式版本
    accept = client.post(
        f"/api/chat/executions/{first_execution}/candidate/accept",
        headers=headers)
    assert accept.status_code == 200, accept.text
    new_version_id = accept.json()["new_version_id"]
    assert new_version_id != first_base

    # 同 message 重新确认，带新 base_version_id + 新幂等键
    resp = _confirm_task(
        headers, sid, mid,
        idempotency_key=str(uuid.uuid4()),
        base_version_id=new_version_id,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 不是首次执行响应：新 execution + 新候选
    assert data["execution_id"] != first_execution
    assert data["candidate"] is not None
    assert data["candidate"]["base_version_id"] == new_version_id

    # 新候选基于新版本（版本内容 = 首次候选内容）
    async def _check():
        async with _test_session_factory() as session:
            ver = (await session.execute(
                select(ChapterVersion).where(ChapterVersion.id == new_version_id)
            )).scalar_one()
            rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == data["candidate"]["record_id"])
            )).scalar_one()
            assert str(rec.base_version_id) == new_version_id
            # 首次 record 已被 accept 采用（applied=正式历史），不得被 superseded 误改
            old_rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == confirm["candidate"]["record_id"])
            )).scalar_one()
            assert old_rec.status == "applied"
            assert old_rec.superseded_by_execution_id is None
    asyncio.run(_check())


def test_confirm_failed_returns_error_message(monkeypatch):
    """修复 A：生成失败（provider 不可用）→ status=failed + error_message，不得静默。"""
    import services.conversation_service as cs

    async def _fail(*args, **kwargs):
        return None

    monkeypatch.setattr(cs, "_generate_candidate", _fail)
    headers = _auth_headers()
    pid, cid, sid, mid, _ = _full_rewrite_flow(headers)

    confirm = _confirm_task(headers, sid, mid)
    assert confirm.status_code == 200, confirm.text
    data = confirm.json()
    assert data["status"] == "failed"
    assert data["candidate"] is None
    assert data["error_message"], "失败响应必须带 error_message"
    assert "API Key" in data["error_message"]


def test_confirm_regenerate_idempotent_replay():
    """修复 1：同一重新生成请求（相同新版本 + 相同新幂等键）重试两次 →
    命中幂等返回首次 regenerate 响应，只生成一条候选记录（不重复消耗 Token）。"""
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    first_execution = confirm["execution_id"]
    first_base = confirm["candidate"]["base_version_id"]

    # 采用 → 新正式版本
    accept = client.post(
        f"/api/chat/executions/{first_execution}/candidate/accept",
        headers=headers)
    assert accept.status_code == 200, accept.text
    new_version_id = accept.json()["new_version_id"]

    regen_key = str(uuid.uuid4())
    resp1 = _confirm_task(headers, sid, mid,
                          idempotency_key=regen_key,
                          base_version_id=new_version_id)
    assert resp1.status_code == 200, resp1.text
    data1 = resp1.json()
    assert data1["execution_id"] != first_execution
    assert data1["candidate"]["base_version_id"] == new_version_id

    # 同新版本 + 同新幂等键重试 → 返回首次 regenerate 响应（幂等）
    resp2 = _confirm_task(headers, sid, mid,
                          idempotency_key=regen_key,
                          base_version_id=new_version_id)
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["execution_id"] == data1["execution_id"]
    assert data2["candidate"]["record_id"] == data1["candidate"]["record_id"]

    # 只生成一条 regenerate 候选记录
    async def _count():
        async with _test_session_factory() as session:
            recs = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.base_version_id == new_version_id)
            )).scalars().all()
            return len(recs)
    assert asyncio.run(_count()) == 1


def test_confirm_regenerate_rejects_base_not_latest():
    """一致性 2：regenerate 传的 base_version_id 已不是最新版本 → 409 拒绝。"""
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    first_base = confirm["candidate"]["base_version_id"]

    # 第一次采用 → v1
    accept1 = client.post(
        f"/api/chat/executions/{confirm['execution_id']}/candidate/accept",
        headers=headers)
    assert accept1.status_code == 200, accept1.text
    v1 = accept1.json()["new_version_id"]

    # 基于 v1 重新生成（v1 != 任务卡旧 base → regenerate）→ 采用 → v2
    regen1 = _confirm_task(headers, sid, mid,
                           idempotency_key=str(uuid.uuid4()),
                           base_version_id=v1)
    assert regen1.status_code == 200, regen1.text
    accept2 = client.post(
        f"/api/chat/executions/{regen1.json()['execution_id']}/candidate/accept",
        headers=headers)
    assert accept2.status_code == 200, accept2.text
    v2 = accept2.json()["new_version_id"]
    assert v2 != v1

    # 再次传 v1（与任务卡旧 base 不同 → regenerate；但 v1 已不是最新 v2）→ 409
    resp = _confirm_task(headers, sid, mid,
                         idempotency_key=str(uuid.uuid4()),
                         base_version_id=v1)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "version_conflict"
    assert body["latest_version_id"] == v2


def test_confirm_concurrent_idempotent_placeholder():
    """并发幂等闭环：同一 idempotency_key 并发调用 confirm_task 两次 →
    只允许一个生成任务；两次返回同一 execution_id；只产生一条 GenerationRecord。

    使用临时文件库（WAL）双连接真并发，覆盖"占位先行"的竞争路径
    （后到者 IntegrityError → 轮询重放首次结果）。
    """
    import tempfile
    from pathlib import Path
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from models.chapter import Chapter
    from models.chapter_version import ChapterVersion
    from models.project import Project
    from models.user import User
    from services.conversation_service import confirm_task, get_or_create_session

    tmp_db = Path(tempfile.mkdtemp()) / "conc.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_db}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            user = User(username=f"conc_{uuid.uuid4().hex[:8]}", hashed_password="x")
            s.add(user)
            await s.flush()
            project = Project(owner_id=str(user.id), title="并发测试", mode="novel")
            s.add(project)
            await s.flush()
            chapter = Chapter(project_id=project.id, title="第一章",
                              sequence_number=1, content="第一章原始正文。并发测试内容。",
                              status="draft")
            s.add(chapter)
            await s.flush()
            base_ver = ChapterVersion(chapter_id=str(chapter.id),
                                     content="第一章原始正文。并发测试内容。",
                                     version_number=1)
            s.add(base_ver)
            await s.flush()
            chat = ChatSession(project_id=project.id, user_id=user.id)
            s.add(chat)
            await s.flush()
            user_msg = ChatMessage(session_id=str(chat.id), role="user",
                                   content="重写第一章", intent="rewrite")
            s.add(user_msg)
            await s.flush()
            task_card = {
                "schema_version": 1,
                "operation_type": "chapter_rewrite",
                "target": {"chapter_id": str(chapter.id), "chapter_no": 1,
                           "chapter_title": "第一章", "version_id": str(base_ver.id)},
                "base_version_id": str(base_ver.id),
                "constraints": {"must_preserve": [], "must_not_change": ["人物设定"],
                                "target_word_count": None, "user_instruction": "重写第一章"},
                "risk_level": "L2",
                "confirmation_required": True,
                "idempotency_key": None,
            }
            assistant_msg = ChatMessage(session_id=str(chat.id), role="assistant",
                                        content="", intent="rewrite", task_card=task_card,
                                        reply_to_id=str(user_msg.id))
            s.add(assistant_msg)
            await s.commit()
            return (str(user.id), str(project.id), str(chapter.id), str(chat.id),
                    str(assistant_msg.id), str(base_ver.id))
    uid, pid, cid, sid, mid, base = asyncio.run(_setup())
    key = f"conc-{uuid.uuid4().hex}"

    async def _confirm_once():
        async with factory() as session:
            chat = await session.get(ChatSession, uuid.UUID(sid))
            return await confirm_task(
                session, chat, mid, user_id=uid,
                confirmed=True, idempotency_key=key,
                base_version_id=base)

    async def _run_concurrent():
        r1, r2 = await asyncio.gather(_confirm_once(), _confirm_once())
        return r1, r2

    r1, r2 = asyncio.run(_run_concurrent())

    # 两次返回同一 execution_id（只有一次生成）
    assert r1["execution_id"] == r2["execution_id"], \
        f"并发应返回同一 execution：{r1['execution_id']} vs {r2['execution_id']}"
    assert r1["status"] != "failed"
    assert r2["status"] != "failed"

    # 只产生一条 GenerationRecord
    async def _count():
        async with factory() as session:
            recs = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.task_card_id == r1["execution_id"])
            )).scalars().all()
            return len(recs)
    assert asyncio.run(_count()) == 1, "并发只允许一个生成任务、一条候选记录"

    async def _cleanup():
        await engine.dispose()
    asyncio.run(_cleanup())
    tmp_db.unlink(missing_ok=True)


def test_regenerate_supersedes_previous_candidate():
    """重新生成成功后：未采用的旧候选稿（candidate 残留）标 superseded 并记录替代执行。"""
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    first_record_id = confirm["candidate"]["record_id"]

    # 手动保存正文 → 新正式版本（不经过 accept，旧候选仍是 candidate）
    patched = client.patch(f"/api/projects/{pid}/chapters/1",
                           json={"content": "手动修改后的正文，作为新基线。"},
                           headers=headers)
    assert patched.status_code == 200, patched.text
    latest = str(_latest_version_id(pid, cid))

    # 基于手动版本重新生成（≠ 任务卡旧 base → regenerate）
    resp = _confirm_task(headers, sid, mid,
                         idempotency_key=str(uuid.uuid4()),
                         base_version_id=latest)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["candidate"]["base_version_id"] == latest

    # 旧候选稿（未采用）→ superseded + 记录被哪个新执行替代
    async def _check():
        async with _test_session_factory() as session:
            old_rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == first_record_id)
            )).scalar_one()
            assert old_rec.status == "superseded"
            assert str(old_rec.superseded_by_execution_id) == data["execution_id"]
            # 新候选仍是 candidate（未被误标）
            new_rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.id == data["candidate"]["record_id"])
            )).scalar_one()
            assert new_rec.status == "candidate"
            assert new_rec.superseded_by_execution_id is None
    asyncio.run(_check())


def test_stale_placeholder_closes_change_set_and_blocks_late_result(monkeypatch):
    """宕机恢复闭环：
    1. 占位超过 stale 阈值 → CAS 接管标 failed **且 ChangeSet 同步 FAILED**；
    2. 原 owner 晚到回填被拒绝（CAS rowcount=0），其候选记录被丢弃（discarded）；
    3. 同幂等重试拿到 failed（含真实 execution_id），不再无限等待。
    """
    from datetime import timedelta
    import services.conversation_service as cs
    from sqlalchemy import text as _text

    # 1) 缩短 stale 阈值 + 2) 让生成流程在占位提交后挂起（模拟进程卡死）
    monkeypatch.setattr(cs, "PLACEHOLDER_STALE_SECONDS", 0.01)

    original_wait = cs._wait_for_placeholder

    async def _slow_wait(*args, **kwargs):
        # 让"占位命中"的轮询走完 stale 接管（阈值 0.01s，立即超龄）
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(cs, "_wait_for_placeholder", _slow_wait)

    # 直接调服务层：先完成一次正常 confirm（产生占位 + 候选），再把占位时间改旧
    headers = _auth_headers()
    pid, cid, sid, mid, confirm = _full_rewrite_flow(headers)
    key = str(uuid.uuid4())
    # 首次确认（带 key）→ 正常完成，占位被回填
    first = _confirm_task(headers, sid, mid, idempotency_key=key)
    assert first.status_code == 200
    exec_id = first.json()["execution_id"]

    # 模拟"进程在占位提交后、回填前崩溃"：把占位消息改回未回填状态（response_json='null'）
    # 且 created_at 改到 stale 阈值之前（-200 秒）
    async def _age_placeholder():
        async with _test_session_factory() as session:
            await session.execute(
                _text("UPDATE chat_messages SET response_json = 'null', "
                      "created_at = datetime('now', '-200 seconds') "
                      "WHERE idempotency_key = :k AND response_json IS NOT NULL"),
                {"k": f"confirm:{key}"},
            )
            await session.commit()
    asyncio.run(_age_placeholder())

    # 同幂等键再次确认 → 占位 stale → 接管标 failed + ChangeSet FAILED
    resp = _confirm_task(headers, sid, mid, idempotency_key=key)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["execution_id"] == exec_id  # 失败响应带真实 execution_id
    assert "超时" in data["error_message"]

    # ChangeSet 已闭合为 failed（前端看到失败时执行状态一致）
    async def _check():
        async with _test_session_factory() as session:
            cs_row = await session.get(ChangeSet, uuid.UUID(exec_id))
            assert cs_row is not None
            assert cs_row.status == "failed"

            # 注意：本场景无"晚到回填者"（首次确认已回填），record 保持 candidate；
            # 晚到回填被拒 → record discarded 由回填 CAS 路径覆盖（并发测试主路径）。
            rec = (await session.execute(
                select(GenerationRecord).where(
                    GenerationRecord.task_card_id == exec_id)
            )).scalar_one()
            assert rec is not None

            # 占位消息失败已落库（后续同幂等请求直接拿到 failed）
            msg = (await session.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(sid),
                    ChatMessage.idempotency_key == f"confirm:{key}",
                )
            )).scalar_one()
            assert msg.response_json is not None
            assert msg.response_json.get("status") == "failed"
    asyncio.run(_check())

    # 同幂等键第三次确认 → 直接命中 failed（不再轮询/不再 running）
    resp3 = _confirm_task(headers, sid, mid, idempotency_key=key)
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "failed"


def test_late_owner_cas_failure_discards_everything(monkeypatch):
    """P0 迟到 owner 竞态（真实双协程 + event gate）：

    时序：owner A 占位提交后阻塞在模型调用（gate.wait）→ 占位 stale →
    owner B 接管（占位标 failed + ChangeSet FAILED）→ 释放 gate →
    A 恢复生成候选并尝试 CAS 回填 → 必须**失败**：
      - A 接口返回 failed（不是它构造好的成功 response）
      - 候选生成事务整体回滚（不残留任何 GenerationRecord）
      - task-card 消息与占位消息都写 failed
      - ChangeSet 保持 failed
      - 同幂等键重复请求不得拿到成功候选（必须 failed）
    """
    import tempfile
    from pathlib import Path
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from models.chapter import Chapter
    from models.chapter_version import ChapterVersion
    from models.project import Project
    from models.user import User
    from services.conversation_service import confirm_task, get_or_create_session

    import services.conversation_service as cs

    # 缩短 stale 阈值：占位提交后 ~0.05s 即可被判定超龄
    monkeypatch.setattr(cs, "PLACEHOLDER_STALE_SECONDS", 0.01)

    tmp_db = Path(tempfile.mkdtemp()) / "late_owner.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_db}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.execute(_text("PRAGMA journal_mode=WAL"))
            await conn.execute(_text("PRAGMA busy_timeout=30000"))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            user = User(username=f"late_{uuid.uuid4().hex[:8]}", hashed_password="x")
            s.add(user)
            await s.flush()
            project = Project(owner_id=str(user.id), title="迟到 owner 测试", mode="novel")
            s.add(project)
            await s.flush()
            chapter = Chapter(project_id=project.id, title="第一章",
                              sequence_number=1, content="第一章原始正文。迟到 owner 测试内容。",
                              status="draft")
            s.add(chapter)
            await s.flush()
            base_ver = ChapterVersion(chapter_id=str(chapter.id),
                                     content="第一章原始正文。迟到 owner 测试内容。",
                                     version_number=1)
            s.add(base_ver)
            await s.flush()
            chat = ChatSession(project_id=project.id, user_id=user.id)
            s.add(chat)
            await s.flush()
            user_msg = ChatMessage(session_id=str(chat.id), role="user",
                                   content="重写第一章", intent="rewrite")
            s.add(user_msg)
            await s.flush()
            task_card = {
                "schema_version": 1,
                "operation_type": "chapter_rewrite",
                "target": {"chapter_id": str(chapter.id), "chapter_no": 1,
                           "chapter_title": "第一章", "version_id": str(base_ver.id)},
                "base_version_id": str(base_ver.id),
                "constraints": {"must_preserve": [], "must_not_change": ["人物设定"],
                                "target_word_count": None, "user_instruction": "重写第一章"},
                "risk_level": "L2",
                "confirmation_required": True,
                "idempotency_key": None,
            }
            assistant_msg = ChatMessage(session_id=str(chat.id), role="assistant",
                                        content="", intent="rewrite", task_card=task_card,
                                        reply_to_id=str(user_msg.id))
            s.add(assistant_msg)
            await s.commit()
            return (str(user.id), str(project.id), str(chapter.id), str(chat.id),
                    str(assistant_msg.id), str(base_ver.id))
    uid, pid, cid, sid, mid, base = asyncio.run(_setup())
    key = f"late-{uuid.uuid4().hex}"

    # ── event gate：owner A 在模型调用处阻塞，直到测试释放 ──
    gate = asyncio.Event()
    original_generate = cs._generate_candidate

    async def _gated_generate(*args, **kwargs):
        await gate.wait()
        return await original_generate(*args, **kwargs)

    monkeypatch.setattr(cs, "_generate_candidate", _gated_generate)

    async def _confirm_once():
        async with factory() as session:
            chat = await session.get(ChatSession, uuid.UUID(sid))
            return await confirm_task(
                session, chat, mid, user_id=uid,
                confirmed=True, idempotency_key=key,
                base_version_id=base)

    async def _run():
        # 1) owner A 启动：提交占位后阻塞在 gate
        task_a = asyncio.create_task(_confirm_once())

        # 2) 等 A 的占位落库（A 已提交占位、正阻塞在模型调用）
        for _ in range(200):
            async with factory() as s:
                row = (await s.execute(
                    select(ChatMessage).where(
                        ChatMessage.session_id == str(sid),
                        ChatMessage.idempotency_key == f"confirm:{key}",
                    )
                )).scalars().first()
                if row is not None:
                    break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("owner A 的占位从未提交")

        # 3) 确保占位已超龄（stale 阈值 0.01s）
        await asyncio.sleep(0.05)

        # 4) owner B 同幂等键确认 → 命中占位 → stale 接管 → 返回 failed
        r_b = await _confirm_once()
        assert r_b["status"] == "failed", f"接管方必须返回 failed: {r_b}"
        assert "超时" in r_b["error_message"]
        exec_id = r_b["execution_id"]
        assert exec_id, "接管响应必须带真实 execution_id"

        # 5) 释放 gate → A 恢复生成并尝试 CAS 回填 → 必须整体失败
        gate.set()
        r_a = await task_a
        assert r_a["status"] == "failed", f"迟到 owner 必须返回 failed: {r_a}"
        assert r_a["candidate"] is None
        assert "超时" in r_a["error_message"]
        assert r_a["execution_id"] == exec_id
        # 不得出现"接口失败但 change_set 仍 pending"的矛盾状态
        assert r_a["change_set"] is not None
        assert r_a["change_set"]["status"] == "failed"

        # 6) 同幂等键第三次确认 → 直接命中 failed，不得拿到成功候选
        r_c = await _confirm_once()
        assert r_c["status"] == "failed"
        assert r_c.get("candidate") is None
        assert r_c["execution_id"] == exec_id

        # 7) 落库状态闭环：ChangeSet failed + 候选整体回滚（不残留任何记录）+
        #    两条消息均 failed
        async with factory() as s:
            cs_row = await s.get(ChangeSet, uuid.UUID(exec_id))
            assert cs_row is not None
            assert cs_row.status == "failed"

            # 迟到 owner 的生成事务已整体回滚：候选记录不得落库（比 discarded
            # 更彻底——不留下半成品/孤儿候选，采用接口不会误读到）
            recs = (await s.execute(
                select(GenerationRecord).where(
                    GenerationRecord.task_card_id == exec_id)
            )).scalars().all()
            assert len(recs) == 0, \
                f"迟到 owner 的候选必须整体回滚，不得残留任何记录: {[(str(r.id), r.status) for r in recs]}"

            # 占位消息（幂等键）失败已落库
            ph = (await s.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(sid),
                    ChatMessage.idempotency_key == f"confirm:{key}",
                )
            )).scalar_one()
            assert ph.response_json is not None
            assert ph.response_json.get("status") == "failed"

            # task-card 消息（mid）也必须 failed，不得残留成功响应
            tc = await s.get(ChatMessage, uuid.UUID(mid))
            assert tc.response_json is not None
            assert tc.response_json.get("status") == "failed"
        return True

    assert asyncio.run(_run())

    async def _cleanup():
        await engine.dispose()
    asyncio.run(_cleanup())


def test_crash_between_cas_and_final_commit_leaks_nothing(monkeypatch):
    """故障注入：进程在第 2 次 commit 成功后宕机（模拟"CAS/最终提交之后立即死亡"）。

    单事务成功路径下第 2 次 commit 就是最终提交（CAS + objects + superseded +
    msg 一体）→ 提交成功后宕机，状态完整一致，不得留下：
      - "占位宣告成功但 ChangeSet.objects 为空"的半成品（采用接口误报无候选）
      - "占位成功但候选缺失 / 任务消息未成功"的不一致
      - "占位未成功但孤儿候选落库"的残留
    同幂等键重试必须与占位状态一致（成功候选或 failed），绝不矛盾。
    """
    import tempfile
    from pathlib import Path
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession as _AS
    from models.chapter import Chapter
    from models.chapter_version import ChapterVersion
    from models.project import Project
    from models.user import User
    from services.conversation_service import confirm_task

    import services.conversation_service as cs

    monkeypatch.setattr(cs, "PLACEHOLDER_STALE_SECONDS", 0.01)

    tmp_db = Path(tempfile.mkdtemp()) / "crash.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_db}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.execute(_text("PRAGMA journal_mode=WAL"))
            await conn.execute(_text("PRAGMA busy_timeout=30000"))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            user = User(username=f"crash_{uuid.uuid4().hex[:8]}", hashed_password="x")
            s.add(user)
            await s.flush()
            project = Project(owner_id=str(user.id), title="宕机测试", mode="novel")
            s.add(project)
            await s.flush()
            chapter = Chapter(project_id=project.id, title="第一章",
                              sequence_number=1, content="第一章原始正文。宕机测试内容。",
                              status="draft")
            s.add(chapter)
            await s.flush()
            base_ver = ChapterVersion(chapter_id=str(chapter.id),
                                     content="第一章原始正文。宕机测试内容。",
                                     version_number=1)
            s.add(base_ver)
            await s.flush()
            chat = ChatSession(project_id=project.id, user_id=user.id)
            s.add(chat)
            await s.flush()
            user_msg = ChatMessage(session_id=str(chat.id), role="user",
                                   content="重写第一章", intent="rewrite")
            s.add(user_msg)
            await s.flush()
            task_card = {
                "schema_version": 1,
                "operation_type": "chapter_rewrite",
                "target": {"chapter_id": str(chapter.id), "chapter_no": 1,
                           "chapter_title": "第一章", "version_id": str(base_ver.id)},
                "base_version_id": str(base_ver.id),
                "constraints": {"must_preserve": [], "must_not_change": ["人物设定"],
                                "target_word_count": None, "user_instruction": "重写第一章"},
                "risk_level": "L2",
                "confirmation_required": True,
                "idempotency_key": None,
            }
            assistant_msg = ChatMessage(session_id=str(chat.id), role="assistant",
                                        content="", intent="rewrite", task_card=task_card,
                                        reply_to_id=str(user_msg.id))
            s.add(assistant_msg)
            await s.commit()
            return (str(user.id), str(project.id), str(chapter.id), str(chat.id),
                    str(assistant_msg.id), str(base_ver.id))
    uid, pid, cid, sid, mid, base = asyncio.run(_setup())
    key = f"crash-{uuid.uuid4().hex}"

    # ── 故障注入：第 2 次 commit **成功执行后**抛异常（模拟进程在提交后宕机）。
    #    单事务版：第 2 次 commit = 最终提交（CAS+objects+superseded+msg 一体）→
    #    提交成功后宕机，状态完整一致（占位成功 ⇔ objects 非空 ⇔ 候选存在 ⇔ msg 成功）。
    #    双事务版（变异）：第 2 次 commit = CAS 提交 → 提交成功后宕机 → 占位显示
    #    成功但 ChangeSet.objects 仍为空 → 一致性断言必须抓住这个半成品。
    #    仅触发一次，重试路径恢复正常。 ──
    real_commit = _AS.commit
    commit_count = {"n": 0}

    async def _crash_after_second_commit(self, *a, **kw):
        commit_count["n"] += 1
        result = await real_commit(self, *a, **kw)
        if commit_count["n"] == 2:
            raise RuntimeError("injected crash: right after commit #2")
        return result

    monkeypatch.setattr(_AS, "commit", _crash_after_second_commit)

    async def _confirm_once():
        async with factory() as session:
            chat = await session.get(ChatSession, uuid.UUID(sid))
            return await confirm_task(
                session, chat, mid, user_id=uid,
                confirmed=True, idempotency_key=key,
                base_version_id=base)

    # 1) 第一次确认：提交后"宕机" → 异常向上抛（进程死亡模拟）
    with pytest.raises(RuntimeError, match="injected crash"):
        asyncio.run(_confirm_once())

    # 2) 一致性不变量：不允许"占位宣告成功但 ChangeSet 无候选"的半成品
    async def _check_consistency():
        async with factory() as s:
            ph = (await s.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(sid),
                    ChatMessage.idempotency_key == f"confirm:{key}",
                )
            )).scalar_one()
            cs_rows = (await s.execute(select(ChangeSet))).scalars().all()
            tc = await s.get(ChatMessage, uuid.UUID(mid))
            recs = (await s.execute(select(GenerationRecord))).scalars().all()

            if ph.response_json is not None and ph.response_json.get("status") != "failed":
                # 占位宣告成功 → 必须成对出现：objects 非空 + 候选存在 + msg 成功
                assert cs_rows and cs_rows[0].objects, \
                    f"占位成功但 ChangeSet.objects 为空（半成品泄漏）: {cs_rows[0].objects if cs_rows else None}"
                assert len(recs) == 1 and recs[0].status == "candidate", \
                    f"占位成功但候选缺失: {[(str(r.id), r.status) for r in recs]}"
                assert tc.response_json is not None and tc.response_json.get("status") != "failed", \
                    f"占位成功但任务消息未成功: {tc.response_json}"
            else:
                # 占位未宣告成功 → 不得有任何候选/objects 残留
                assert len(recs) == 0, \
                    f"占位未成功但候选落库（孤儿候选）: {[(str(r.id), r.status) for r in recs]}"
                assert not (cs_rows and cs_rows[0].objects), "占位未成功但 objects 已写入"
    asyncio.run(_check_consistency())

    # 3) 同幂等键重试：与占位状态一致（要么成功候选、要么 failed），
    #    绝不出现"占位成功但重试失败/无候选"的矛盾
    resp = asyncio.run(_confirm_once())
    if resp["status"] == "failed":
        assert resp.get("candidate") is None
    else:
        assert resp.get("candidate") is not None

    async def _cleanup():
        await engine.dispose()
    asyncio.run(_cleanup())
