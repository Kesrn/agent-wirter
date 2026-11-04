"""意图路由测试（方案 v2.4，S2）——LLM 主路径 / 规则降级 / 流程控制门。

对照验收顺序第 2 项：
- flow_control_passthrough 预筛：命中流程词 + 动作可用 + 无否定/疑问 → 直达 flow_control；
  否定 / 疑问 / 动作不可用 / 定稿（第一版不支持）→ 不直达或返回 intent_unsupported
- classify_with_llm 主路径：严格 Pydantic 解析（LLMIntentOutput），
  非法输出 / 校验不过 / provider 异常 / 无 Key → 规则回退（llm_used 缺失）
- classify_intent 三层路由：gate → LLM 主路径 → 规则降级（含否定/疑问检测）
- conversation_service 集成：_session_available_actions 按会话 change_set 状态
  计算 accept/discard/revert，传入后 gate 在真实会话上生效
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

import test_smoke as smoke
from db.session import set_engine
from models import Project, User
from models.base import Base
from models.change_set import ChangeSet
from models.chat_conversation import ChatSession
from services.conversation_service import ChangeSetStatus, _session_available_actions
from services.intent_classifier import (
    classify_intent,
    classify_rule,
    classify_with_llm,
    flow_control_passthrough,
)

_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory


@pytest.fixture(autouse=True)
def _ensure_db():
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── LLM mock 设施 ──────────────────────────────────────────


class FakeProvider:
    """记录调用并返回预设 raw 输出的假 provider。"""

    def __init__(self, raw: str | None = None, exc: Exception | None = None):
        self.raw = raw
        self.exc = exc
        self.calls: list[str] = []

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        self.calls.append(user_prompt)
        if self.exc is not None:
            raise self.exc
        return self.raw


def _patch_llm(monkeypatch, provider: FakeProvider):
    """patch classify_with_llm 内部的两个动态 import 来源。"""

    async def _fake_config(user_id, db):
        return {"provider": "mock", "api_key": "test"}

    monkeypatch.setattr(
        "agents.llm_provider.get_llm_provider", lambda config: provider
    )
    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _fake_config)


def _valid_rewrite_json(**overrides) -> str:
    payload = {
        "intent": "rewrite",
        "operation": "polish_chapter",
        "target_scope": "explicit_chapter",
        "target_chapter_no": 3,
        "mentioned_entities": ["穆白"],
        "requirements": ["加强穆白内心戏"],
        "needs_clarification": False,
        "clarification_question": None,
        "plan": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── A. flow_control_passthrough 预筛（纯函数） ──────────────


def test_gate_hit_revert_when_available():
    r = flow_control_passthrough("撤销上一版", {"revert"})
    assert r is not None
    assert r["intent"] == "flow_control"
    assert r["action_hint"] == "revert"
    assert r["requires_confirmation"] is False
    assert "llm_used" not in r  # 规则直达，不经过 LLM


def test_gate_hit_accept_discard_mapping():
    assert flow_control_passthrough("采用这版", {"accept"})["action_hint"] == "accept"
    assert flow_control_passthrough("丢弃这个候选稿", {"discard"})["action_hint"] == "discard"


def test_gate_hit_ba_yao_le_is_discard_not_negation():
    # "不要了" 整体是丢弃短语，自身"不要"前缀不得触发否定检测
    assert flow_control_passthrough("不要了", {"discard"})["action_hint"] == "discard"
    r = classify_rule("不要了")
    assert r["intent"] == "flow_control"


def test_gate_miss_when_action_unavailable():
    # 动作不可用（无 pending 候选稿 / 无可回滚对象）→ 放行给 LLM/规则
    assert flow_control_passthrough("撤销上一版", set()) is None
    assert flow_control_passthrough("采用这版", {"revert"}) is None


def test_gate_miss_on_negation():
    assert flow_control_passthrough("不要撤销，继续修改", {"revert"}) is None


def test_gate_miss_on_question_tone():
    assert flow_control_passthrough("讨论一下该不该定稿", {"accept", "discard", "revert"}) is None


def test_gate_finalize_unsupported():
    r = flow_control_passthrough("可以定稿了", {"accept", "discard", "revert"})
    assert r is not None
    assert r["intent"] == "flow_control"
    assert r["intent_unsupported"] is True
    assert "不支持定稿" in r["unsupported_reason"]


# ── B. classify_with_llm 主路径 ─────────────────────────────


def test_llm_main_path_valid_rewrite(monkeypatch):
    provider = FakeProvider(_valid_rewrite_json())
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色一下第三章，重点写穆白", None, rule_result))

    assert result["llm_used"] is True
    assert result["intent"] == "rewrite"
    assert result["operation"] == "polish_chapter"
    assert result["target_chapter_no"] == 3
    assert result["mentioned_entities"] == ["穆白"]
    assert result["requirements"] == ["加强穆白内心戏"]
    # rewrite → 后端计算 requires_confirmation（补锁 1）
    assert result["requires_confirmation"] is True
    # 方案 A：operation + requirements 组装 resolved_instruction
    assert "执行润色章节" in result["resolved_instruction"]
    assert "穆白" in result["resolved_instruction"]
    assert "加强穆白内心戏" in result["resolved_instruction"]


def test_llm_main_path_discussion(monkeypatch):
    provider = FakeProvider(
        json.dumps(
            {"intent": "discussion", "operation": None, "target_scope": "none",
             "target_chapter_no": None, "mentioned_entities": [],
             "requirements": [], "needs_clarification": False,
             "clarification_question": None, "plan": None},
            ensure_ascii=False,
        )
    )
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "收到，创作讨论。可以直接交流想法", None, rule_result))

    assert result["llm_used"] is True
    assert result["intent"] == "discussion"
    assert result["requires_confirmation"] is False


def test_llm_clarification_mapped(monkeypatch):
    provider = FakeProvider(
        json.dumps(
            {"intent": "rewrite", "operation": "rewrite_chapter", "target_scope": "none",
             "target_chapter_no": None, "mentioned_entities": [],
             "requirements": [], "needs_clarification": True,
             "clarification_question": "请问要修改第几章？", "plan": None},
            ensure_ascii=False,
        )
    )
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "帮我改一下", None, rule_result))

    assert result["clarification"]["needs_clarification"] is True
    assert result["clarification"]["reason"] == "请问要修改第几章？"


def test_llm_extra_field_rejected_falls_back(monkeypatch):
    # 严格模型 extra="forbid"：LLM 输出 requires_confirmation → 整体拒绝 → 规则回退
    bad = json.loads(_valid_rewrite_json())
    bad["requires_confirmation"] = False
    provider = FakeProvider(json.dumps(bad, ensure_ascii=False))
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result
    assert result == rule_result


def test_llm_unknown_operation_falls_back(monkeypatch):
    bad = json.loads(_valid_rewrite_json())
    bad["operation"] = "delete_chapter"  # 白名单外
    provider = FakeProvider(json.dumps(bad, ensure_ascii=False))
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result


def test_llm_target_zero_falls_back(monkeypatch):
    bad = json.loads(_valid_rewrite_json())
    bad["target_chapter_no"] = 0  # ge=1 校验不过
    provider = FakeProvider(json.dumps(bad, ensure_ascii=False))
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result


def test_llm_garbage_json_falls_back(monkeypatch):
    provider = FakeProvider("这不是 JSON")
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result
    assert result == rule_result


def test_llm_provider_error_falls_back(monkeypatch):
    provider = FakeProvider(exc=RuntimeError("provider down"))
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result
    assert result == rule_result


def test_llm_no_key_falls_back(monkeypatch):
    # 无 Key：get_user_llm_config 抛异常 → 规则回退
    async def _no_key(user_id, db):
        raise RuntimeError("未配置 LLM Key")

    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _no_key)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "润色第三章", None, rule_result))

    assert "llm_used" not in result
    assert result == rule_result


# ── C. classify_intent 三层路由 ─────────────────────────────


def test_route_gate_hit_never_calls_llm(monkeypatch):
    # gate 命中 → 不调用 LLM（provider 若被调用必然抛异常，测试可证）
    provider = FakeProvider(exc=RuntimeError("gate 不应触发 LLM"))
    _patch_llm(monkeypatch, provider)

    result = asyncio.run(classify_intent("u1", "撤销上一版", None, available_actions={"revert"}))

    assert result["intent"] == "flow_control"
    assert result["action_hint"] == "revert"
    assert provider.calls == []


def test_route_gate_miss_uses_llm(monkeypatch):
    provider = FakeProvider(_valid_rewrite_json())
    _patch_llm(monkeypatch, provider)

    result = asyncio.run(classify_intent("u1", "润色第三章，重点写穆白", None, available_actions=set()))

    assert result["llm_used"] is True
    assert result["intent"] == "rewrite"
    assert result["resolved_instruction"]
    assert provider.calls  # LLM 确实被调用


def test_route_gate_miss_llm_fails_rule_fallback(monkeypatch):
    provider = FakeProvider(exc=RuntimeError("down"))
    _patch_llm(monkeypatch, provider)

    result = asyncio.run(classify_intent("u1", "压缩第十章前半段", None, available_actions=set()))

    assert "llm_used" not in result
    assert result["intent"] == "rewrite"  # 规则降级仍正确识别改写


def test_route_fallback_negation_beats_flow_keyword(monkeypatch):
    # 无 Key + "不要撤销，继续修改" → 规则降级必须识别为改写/讨论，不是 flow_control
    provider = FakeProvider(exc=RuntimeError("down"))
    _patch_llm(monkeypatch, provider)

    result = asyncio.run(classify_intent("u1", "不要撤销，继续修改第三章", None, available_actions={"revert"}))

    assert result["intent"] == "rewrite"
    assert result["intent"] != "flow_control"


def test_route_fallback_question_beats_flow_keyword(monkeypatch):
    provider = FakeProvider(exc=RuntimeError("down"))
    _patch_llm(monkeypatch, provider)

    result = asyncio.run(classify_intent("u1", "讨论一下该不该定稿", None, available_actions=set()))

    assert result["intent"] == "discussion"
    assert result["intent"] != "flow_control"


def test_rule_question_tone_covers_plain_rewrite_words():
    """P1-1：疑问检测必须覆盖普通修改词——"讨论一下该不该修改第三章"不得生成任务卡。"""
    for text in ("讨论一下该不该修改第三章", "要不要把第三章改得更紧凑"):
        r = classify_rule(text)
        assert r["intent"] == "discussion", text
    # 明确指令不受影响
    for text in ("修改第三章", "润色第三章"):
        assert classify_rule(text)["intent"] == "rewrite", text
    # 知识查询本身就是提问，不受疑问检测拦截
    assert classify_rule("查一下第三章在哪里")["intent"] == "knowledge_query"


def test_route_llm_compound_unsupported(monkeypatch):
    compound = json.loads(_valid_rewrite_json())
    compound["plan"] = {
        "steps": [
            {"intent": "knowledge_query", "raw": "查穆白最后状态", "target_chapter_no": 3},
            {"intent": "rewrite", "raw": "修改第三章", "target_chapter_no": 3},
        ],
        "dependencies": [{"from_step": 1, "to_step": 2}],
    }
    provider = FakeProvider(json.dumps(compound, ensure_ascii=False))
    _patch_llm(monkeypatch, provider)
    rule_result = {"intent": "discussion", "steps": [], "plan": None,
                   "requires_confirmation": False, "clarification": None, "confidence": 0.9}

    result = asyncio.run(classify_with_llm("u1", "查穆白最后状态然后修改第三章", None, rule_result))

    # 识别但拒绝执行（补锁 4）：intent_unsupported + task_card=None
    assert result["intent_unsupported"] is True
    assert result["task_card"] is None
    assert result["plan"] is not None
    # 对外计划：steps 带 step/intent/raw，依赖聚合为 {step:2, depends_on:[1]}
    assert result["steps"] == ["knowledge_query", "rewrite"]
    assert result["plan"]["dependencies"] == [{"step": 2, "depends_on": [1]}]
    assert result["plan"]["steps"][0]["target"] == {"chapter_no": 3}


# ── D. conversation_service 集成：available_actions → gate ──


def test_session_available_actions_computed_from_change_sets():
    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"act_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="动作测试", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)
            # 无任何 change_set → 无动作
            assert await _session_available_actions(session, sid) == set()
            # pending 候选稿 → accept/discard 可用
            pending = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.PENDING,
            )
            session.add(pending)
            await session.flush()
            assert await _session_available_actions(session, sid) == {"accept", "discard"}
            # 已应用变更 → 追加 revert
            applied = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.APPLIED,
            )
            session.add(applied)
            await session.flush()
            assert await _session_available_actions(session, sid) == {"accept", "discard", "revert"}
            # 失败/已回滚不贡献动作
            failed = ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.FAILED,
            )
            session.add(failed)
            await session.flush()
            assert await _session_available_actions(session, sid) == {"accept", "discard", "revert"}

    asyncio.run(_go())


def test_classify_intent_gate_works_with_real_session(monkeypatch):
    """会话有 pending 候选稿时，'采用这版' 经 _session_available_actions → gate 直达。"""
    provider = FakeProvider(exc=RuntimeError("gate 不应触发 LLM"))
    _patch_llm(monkeypatch, provider)

    async def _go():
        async with _test_session_factory() as session:
            user = User(username=f"gate_{uuid.uuid4().hex[:8]}", hashed_password="x")
            session.add(user)
            await session.flush()
            project = Project(owner_id=str(user.id), title="gate 测试", mode="novel")
            session.add(project)
            await session.flush()
            chat = ChatSession(project_id=str(project.id), user_id=str(user.id))
            session.add(chat)
            await session.flush()
            sid = str(chat.id)
            session.add(ChangeSet(
                session_id=sid, operation_type="chapter_rewrite",
                intent="rewrite", status=ChangeSetStatus.PENDING,
            ))
            await session.flush()

            actions = await _session_available_actions(session, sid)
            assert actions == {"accept", "discard"}

            result = await classify_intent(
                str(user.id), "采用这版", session, available_actions=actions
            )
            assert result["intent"] == "flow_control"
            assert result["action_hint"] == "accept"
            assert provider.calls == []  # gate 直达，未调 LLM

    asyncio.run(_go())
