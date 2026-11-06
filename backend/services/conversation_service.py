"""对话式创作 —— 会话 / 意图分流 / 任务卡 / 候选稿 / 采用/丢弃/继续 / change_set 撤销。

方案 §12 契约 + §14 强制约束的第一阶段实现（单章局部重写闭环）：

- POST /api/chat/sessions                        get-or-create 会话
- POST /api/chat/sessions/{id}/messages          先回意图不执行（幂等）
- POST /api/chat/sessions/{id}/messages/{mid}/confirm   确认任务卡 → 生成候选稿
- POST /api/chat/executions/{id}/candidate/accept       版本冲突检测 + 同事务写版本与 change_set
- POST /api/chat/executions/{id}/candidate/discard      丢弃候选稿（不写正式数据）
- POST /api/chat/executions/{id}/candidate/revise       按 revise_instruction 重新生成候选稿
- POST /api/chat/change-sets/{id}/revert                撤销单次操作
- POST /api/chat/change-sets/revert-session             按时间逆序回滚本轮全部操作

§14 强制约束：
1. base_version_id 只指向章节正式版本（ChapterVersion）；
2. idempotency_key 在用户+项目+接口范围内唯一，保存首次响应结果，重复请求直接返回；
3. confirm/accept/discard/revert 全幂等：重复调用返回当前状态，不重复生成/回滚/写版本；
4. change_set 落库与版本写入同一事务（accept 内先改章节再 flush，两侧同 commit）。

"执行"（execution）= change_set 行；execution_id 即 change_set.id。
候选稿落 generation_records（status=candidate），携带 conversation_id /
base_version_id / task_card_id 三列（§11 决策点 1）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from models.chat_conversation import ChatMessage, ChatSession
from models.change_set import ChangeSet
from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.generation_record import GenerationRecord
from services.content_sanitizer import sanitize_chapter_content
from services.intent_classifier import INTENT_UNSUPPORTED, classify_intent
from services.version_service import create_version

logger = logging.getLogger(__name__)

INTENTS = ("knowledge_query", "discussion", "rewrite", "data_operation", "flow_control")


# 占位视为 stale（进程宕机/强杀后永远无人回填）的时间阈值。
# 正常生成最坏约 90s（SSE idle 超时），阈值取 180s 避免误杀慢请求。
PLACEHOLDER_STALE_SECONDS = 180.0


async def _wait_for_placeholder(
    db: AsyncSession,
    session_id: str,
    idem_key: str,
    execution_id: str | None,
    timeout: float = 15.0,
) -> dict:
    """并发幂等：另一请求已落占位（response_json 为空）→ 轮询等待其完成并重放结果。

    轮询期间首个请求完成会回填 response_json；超时返回 running 状态（前端继续轮询）。
    **宕机恢复**：占位存在超过 PLACEHOLDER_STALE_SECONDS（原进程崩溃/被强杀，
    永远无人回填）→ CAS 接管标 failed，后续同幂等请求直接拿到失败原因，
    不再无限等待。原进程晚到回填会被 CAS 守卫拒绝（占位已被接管方写入 failed，
    不再匹配 "response_json IS NULL OR 'null'"），其生成事务整体回滚，
    不会覆盖 failed，也不会泄漏半成品候选。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        # SQLite WAL 快照隔离：同一事务内看不到其他连接的新提交 →
        # 每次查询前结束当前事务，确保读到对方最新的回填结果。
        await db.commit()
        # populate_existing：本 session 可能已缓存过该占位行（expire_on_commit=False
        # 不自动刷新），必须强制从数据库重新加载，否则永远读不到对方的回填。
        row = (
            await db.execute(
                select(ChatMessage)
                .execution_options(populate_existing=True)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "assistant",
                    ChatMessage.idempotency_key == idem_key,
                )
            )
        ).scalars().first()
        if row is not None and row.response_json is not None:
            return row.response_json
        if row is not None and row.created_at is not None:
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > PLACEHOLDER_STALE_SECONDS:
                # 宕机恢复：CAS 接管（仅当仍无人回填），标 failed。
                # **状态原子闭环**：同一事务内更新占位消息（带真实 execution_id）
                # + ChangeSet.status=FAILED——前端看到失败时执行记录也必须闭合，
                # 不得出现"消息失败但 change_set 仍 pending"。
                from sqlalchemy import update as sa_update
                stale_exec_id = row.execution_id
                failed_payload = {
                    "execution_id": str(stale_exec_id) if stale_exec_id else None,
                    "status": "failed",
                    "candidate": None,
                    "error_message": (
                        "候选稿生成超时（生成进程可能已中断），"
                        "请检查模型配置后重新发起。"
                    ),
                    "change_set": None,
                }
                result = await db.execute(
                    sa_update(ChatMessage)
                    .where(ChatMessage.id == row.id)
                    .where(sa.text("(response_json IS NULL OR response_json = 'null')"))
                    .values(response_json=failed_payload)
                )
                if result.rowcount == 1 and stale_exec_id:
                    # 闭合执行状态：ChangeSet pending → failed（同一事务）
                    await db.execute(
                        sa_update(ChangeSet)
                        .where(ChangeSet.id == stale_exec_id)
                        .where(ChangeSet.status == ChangeSetStatus.PENDING)
                        .values(status=ChangeSetStatus.FAILED)
                    )
                await db.commit()
                if result.rowcount == 1:
                    return failed_payload
        await asyncio.sleep(0.2)
    # 超时：返回 running 占位状态，前端可稍后重试（幂等键仍会命中占位）
    cs = None
    if execution_id:
        cs = await db.get(ChangeSet, uuid.UUID(str(execution_id)))
    return {
        "execution_id": execution_id,
        "status": "running",
        "candidate": None,
        "change_set": _change_set_dict(cs) if cs is not None else None,
    }


class ChatError(Exception):
    """对话层业务错误。status_code 对应 HTTP 状态；payload 为附加错误体。"""

    def __init__(self, message: str, status_code: int = 400, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


class ExecutionStatus:
    PENDING = "pending"      # 候选稿已生成，未采用/丢弃
    ACCEPTED = "accepted"    # 已采用（change_set applied）
    DISCARDED = "discarded"  # 已丢弃 / 已回滚
    FAILED = "failed"        # 候选稿生成失败


class ChangeSetStatus:
    PENDING = "pending"
    APPLIED = "applied"
    REVERTED = "reverted"
    FAILED = "failed"


# ── 会话 ──────────────────────────────────────────────

async def get_or_create_session(
    db: AsyncSession,
    project_id: str,
    user_id: str,
) -> ChatSession:
    """每用户每项目至多一个会话（get-or-create）。"""
    pid, uid = str(project_id), str(user_id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.project_id == pid,
            ChatSession.user_id == uid,
        )
    )
    session = result.scalar_one_or_none()
    if session is not None:
        return session
    session = ChatSession(project_id=pid, user_id=uid)
    db.add(session)
    await db.flush()
    return session


def _session_dict(session: ChatSession) -> dict:
    return {
        "session_id": str(session.id),
        "project_id": str(session.project_id),
        "title": session.title,
        "summary": session.summary,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


# ── 消息分类（先回意图，不执行） ────────────────────────

async def _find_message_by_key(
    db: AsyncSession,
    session_id: str,
    idempotency_key: str,
) -> ChatMessage | None:
    """会话内按请求幂等号查找已有 user 消息。"""
    result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == str(session_id),
            ChatMessage.role == "user",
            ChatMessage.idempotency_key == idempotency_key,
        ).order_by(ChatMessage.created_at.desc()).limit(1)
    )
    return result.scalars().first()


def _build_task_card(
    content: str,
    chapter: Chapter,
    base_version_id: str | None,
    classifier: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict:
    """构建 chapter_rewrite 任务卡（方案 §12.3）。base_version_id 指向正式版本。

    classifier 非空时追加 LLM 解析字段（方案补锁 2）：
    resolved_instruction / operation / mentioned_entities / requirements /
    router_source / prompt_version；原始 user_instruction 保留便于追溯。
    context_snapshot（P1-2）：记录 AI 理解时基于的工作对象上下文
    （章节号/版本/选中文本/当前段落），便于事后追溯。
    任务卡整体写入 GenerationRecord.request_params → 存根自动溯源。
    """
    card = {
        "schema_version": 1,
        "operation_type": "chapter_rewrite",
        "target": {
            "chapter_no": chapter.sequence_number,
            "chapter_id": str(chapter.id),
            "chapter_title": chapter.title,
            "version_id": base_version_id,
        },
        "base_version_id": base_version_id,
        "constraints": {
            "must_preserve": [],
            "must_not_change": ["人物设定", "大纲"],
            "target_word_count": None,
            "user_instruction": content,
        },
        "risk_level": "L2",
        "confirmation_required": True,
        "idempotency_key": None,
    }
    if classifier:
        card["resolved_instruction"] = classifier.get("resolved_instruction")
        card["operation"] = classifier.get("operation")
        card["mentioned_entities"] = classifier.get("mentioned_entities") or []
        card["requirements"] = classifier.get("requirements") or []
        card["router_source"] = "llm" if classifier.get("llm_used") else "rule"
        card["prompt_version"] = "intent-router-v2"
    if context:
        card["context_snapshot"] = {
            "chapter_no": chapter.sequence_number,
            "version_id": base_version_id,
            "selected_text": (context.get("selection") or "").strip() or None,
            "current_paragraph": (context.get("current_paragraph") or "").strip() or None,
        }
    return card


async def _current_chapter_version_id(db: AsyncSession, chapter_id: str) -> str | None:
    """章节当前正式版本 id（version_number 最大的一条 ChapterVersion）。"""
    result = await db.execute(
        select(ChapterVersion.id)
        .where(ChapterVersion.chapter_id == str(chapter_id))
        .order_by(ChapterVersion.version_number.desc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def _load_chat_history(db: AsyncSession, session_id: str, limit_rounds: int = 5) -> list[dict]:
    """最近 N 个 user-assistant 回合（补锁 3）。

    先按 created_at DESC 取最近 N 条 user 消息，再按 reply_to_id 补齐对应
    assistant 消息（跳过未完成的占位消息：无回复内容且 response_json 为空）。
    不用裸 LIMIT 2N——那会取到不成对的消息。返回按时间正序的
    [{role, content}, ...]，最多 2N 条。
    """
    user_msgs = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == str(session_id),
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(limit_rounds)
        )
    ).scalars().all()
    history: list[dict] = []
    for um in reversed(list(user_msgs)):  # 时间正序
        am = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == str(session_id),
                    ChatMessage.role == "assistant",
                    ChatMessage.reply_to_id == str(um.id),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        history.append({"role": "user", "content": um.content})
        if am is not None:
            # 占位消息：response_json 未回填且无回复内容 → 跳过
            reply = (am.content or "").strip()
            resp = am.response_json if isinstance(am.response_json, dict) else None
            if reply:
                history.append({"role": "assistant", "content": reply})
            elif resp and (resp.get("reply") or resp.get("content")):
                history.append({
                    "role": "assistant",
                    "content": resp.get("reply") or resp.get("content"),
                })
    return history


async def _load_confirmed_constraints(db: AsyncSession, session_id: str) -> list[str]:
    """最近一条已确认执行任务卡的约束（补锁 3："按刚才第二种方案改"）。

    真实消息链路（P0-2 修复）：
    - "执行"= change_set 行；消息上的 execution_id 即 change_set.id（模块头注释）
    - confirm 成功后回填**被确认消息**的 execution_id = change_set.id：
      前端传 user 消息 id → user 消息被回填（reply_to_id=None）
      幂等占位消息（idem_key 时）execution_id = change_set.id（reply_to=user 消息）
    - 分类助手消息：有 task_card、execution_id=None（reply_to_id=user 消息）
    - ChangeSet.execution_id 列第一版恒为 None，**不能作为关联键**

    正确链路：ChangeSet.id（=消息上的 execution_id）→ 定位 user 消息 →
    分类助手消息（reply_to_id=user 消息、带 task_card）→ 取约束。
    """
    cses = (
        await db.execute(
            select(ChangeSet)
            .where(ChangeSet.session_id == str(session_id))
            .order_by(ChangeSet.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    for cs in cses:
        if cs.status in {ChangeSetStatus.FAILED, ChangeSetStatus.REVERTED}:
            continue
        # 1) 消息 execution_id == change_set.id → 定位原始 user 消息
        #    （被回填的 user 消息本身，或幂等占位消息 reply_to 指向的 user 消息）
        linked = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == str(session_id),
                    ChatMessage.execution_id == str(cs.id),
                )
                .limit(1)
            )
        ).scalars().first()
        if linked is None:
            continue
        user_msg_id = str(linked.id) if linked.role == "user" else (
            linked.reply_to_id or None)
        if user_msg_id is None:
            continue
        # 2) 原始 user 消息 → 分类助手消息（带 task_card 的那条）
        am = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == str(session_id),
                    ChatMessage.role == "assistant",
                    ChatMessage.reply_to_id == user_msg_id,
                    ChatMessage.task_card.is_not(None),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if am is None or not am.task_card:
            continue
        card = am.task_card
        constraints: list[str] = []
        if card.get("resolved_instruction"):
            constraints.append(card["resolved_instruction"])
        constraints.extend(card.get("requirements") or [])
        return constraints  # 只取最近一条已确认的
    return []


async def _session_available_actions(db: AsyncSession, session_id: str) -> set[str]:
    """会话当前可执行的流程控制动作（补锁 5：accept/discard/revert 分别判断）。

    - 存在 pending 候选稿（change_set.pending）→ 可 accept / discard
    - 存在已应用变更（change_set.applied，本轮）→ 可 revert
    """
    rows = (
        await db.execute(
            select(ChangeSet.status).where(
                ChangeSet.session_id == str(session_id),
                ChangeSet.status.in_(
                    (ChangeSetStatus.PENDING, ChangeSetStatus.APPLIED)
                ),
            )
        )
    ).scalars().all()
    actions: set[str] = set()
    if ChangeSetStatus.PENDING in rows:
        actions.update(("accept", "discard"))
    if ChangeSetStatus.APPLIED in rows:
        actions.add("revert")
    return actions


def _canned_reply(intent: str, classifier: dict[str, Any]) -> str:
    # P0-1：识别但拒绝执行（复合任务/定稿）→ 直接展示 unsupported_reason，
    # 不得用"已准备任务卡"等默认文案误导用户认为可以确认执行。
    if classifier.get("intent_unsupported"):
        return classifier.get("unsupported_reason") or "该指令暂不支持自动执行。"
    if intent == "knowledge_query":
        return "收到，这是知识查询意图。第一版对话助手暂不执行知识检索，请在资料问答入口查询。"
    if intent == "discussion":
        return "收到，创作讨论。可以直接交流想法，我不会修改任何正式数据。"
    if intent == "rewrite":
        return "收到修改指令。我准备了一张单章局部重写的任务卡，请确认后我再生成候选稿（不会覆盖正式正文）。"
    if intent == "data_operation":
        return "收到数据操作指令。第一版仅支持单章局部重写，跨对象数据变更暂不执行。"
    if intent == "flow_control":
        return "收到流程控制指令。撤销/采用/丢弃通过对应的候选稿与变更集操作完成。"
    return "已收到消息。"


async def classify_message(
    db: AsyncSession,
    session: ChatSession,
    content: str,
    idempotency_key: str,
    *,
    user_id: str,
    context: dict[str, Any] | None = None,
) -> dict:
    """用户发消息 → 先回意图不执行。

    幂等：同一会话内同一 idempotency_key 只保存首次响应，重复请求返回首次响应
    （response_json 完整保存首次分类结果，含 message_id）。
    """
    # 幂等：已处理过该幂等号 → 直接返回首次响应
    existing_user = await _find_message_by_key(db, str(session.id), idempotency_key)
    if existing_user is not None:
        assistant = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(session.id),
                    ChatMessage.reply_to_id == str(existing_user.id),
                ).order_by(ChatMessage.created_at.desc()).limit(1)
            )
        ).scalars().first()
        if assistant is not None and assistant.response_json is not None:
            return assistant.response_json

    # 意图分流：流程控制预筛（按会话当前可用动作）+ LLM 主路径 + 规则降级。
    # 补锁 3：注入最近 5 回合对话历史 / 当前工作对象 / 已确认约束——
    # "按刚才第二种方案改"依赖前文才能解析成具体指令。
    available_actions = await _session_available_actions(db, str(session.id))
    chat_history = await _load_chat_history(db, str(session.id))
    confirmed_constraints = await _load_confirmed_constraints(db, str(session.id))
    classifier = await classify_intent(
        user_id, content, db,
        available_actions=available_actions,
        chat_history=chat_history,
        workspace_context=context,
        confirmed_constraints=confirmed_constraints,
    )
    intent = classifier["intent"]
    if intent == INTENT_UNSUPPORTED:
        intent = "flow_control"

    # P0-1：识别但拒绝执行（intent_unsupported，如复合任务/定稿）→ 强制
    # task_card=null、requires_confirmation=false，不进入目标解析与任务卡构建。
    # assistant 消息因此无 task_card，confirm 接口无法执行该消息（见 confirm_task）。
    unsupported = bool(classifier.get("intent_unsupported"))

    # 目标对象（rewrite）：四层优先级解析（显式 > 本章指示词 > LLM 推断 > 澄清）
    chapter = None
    base_version_id = None
    task_card = None
    requires_confirmation = False
    if not unsupported:
        if intent == "rewrite":
            chapter = await _resolve_rewrite_target(
                db,
                str(session.project_id),
                content,
                context=context,
                llm_target=classifier.get("target_chapter_no"),
            )
            if chapter is not None:
                base_version_id = await _current_chapter_version_id(db, str(chapter.id))

        if intent == "rewrite" and chapter is not None:
            task_card = _build_task_card(
                content, chapter, base_version_id,
                classifier=classifier, context=context,
            )
            # L2（修改单章正式内容）→ 必须确认
            requires_confirmation = True
        elif intent == "data_operation":
            requires_confirmation = True

    # 澄清条件（方案 §3：宁可多问一步，不要推断执行）。
    # 统一返回结构化对象 {needs_clarification, reason, questions}，与前端
    # ConversationClarification 契约一致（第一版 questions 承载反问）。
    clarification = None
    if not unsupported and intent == "rewrite" and chapter is None:
        clarification = {
            "needs_clarification": True,
            "reason": "修改指令需要指定目标章节",
            "questions": [
                {"id": "target_chapter", "question": "请问要修改第几章？",
                 "type": "free_text", "required": True},
            ],
        }

    plan = classifier.get("plan")
    reply = _canned_reply(intent, classifier)

    user_msg = ChatMessage(
        session_id=str(session.id),
        role="user",
        content=content,
        intent=intent,
        idempotency_key=idempotency_key,
    )
    db.add(user_msg)
    await db.flush()

    response = {
        "message_id": str(user_msg.id),
        "intent": intent,
        "plan": plan,
        "requires_confirmation": requires_confirmation,
        "task_card": task_card,
        "clarification": clarification,
        "reply": reply,
    }
    if classifier.get("intent_unsupported"):
        response["intent_unsupported"] = True
        response["unsupported_reason"] = classifier.get("unsupported_reason")
    if task_card is not None:
        task_card["idempotency_key"] = idempotency_key

    assistant_msg = ChatMessage(
        session_id=str(session.id),
        role="assistant",
        content=reply,
        intent=intent,
        plan=plan,
        task_card=task_card,
        reply_to_id=str(user_msg.id),
        response_json=response,
    )
    db.add(assistant_msg)
    await db.flush()
    await db.commit()
    return response


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _parse_chapter_no(token: str) -> int | None:
    """解析章节号 token（支持阿拉伯数字与中文数字 1~99）。"""
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    # 中文数字
    if token in ("十", "拾"):
        return 10
    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if "十" in token:
        parts = token.split("十")
        tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        ones = _CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        value = tens * 10 + ones
        return value if 1 <= value <= 99 else None
    return None


# "本章/这章"指示词（决策 1：上下文只在用户指明当前章节时生效，不无条件回退）
_THIS_CHAPTER_WORDS = ("本章", "这章")


async def _chapter_by_seq(db: AsyncSession, project_id: str, seq: int) -> Chapter | None:
    result = await db.execute(
        select(Chapter).where(
            Chapter.project_id == str(project_id),
            Chapter.sequence_number == seq,
        )
    )
    return result.scalar_one_or_none()


async def _resolve_rewrite_target(
    db: AsyncSession,
    project_id: str,
    content: str,
    *,
    context: dict[str, Any] | None = None,
    llm_target: int | None = None,
) -> Chapter | None:
    """目标章节四层优先级解析（方案决策 1）：

    1. 用户显式章节号（"第 N 章"）     ← LLM 不得覆盖；不存在 → None（澄清）
    2. "本章/这章"指示词 + 当前章节     ← 次高；指示词但无上下文 → None（澄清）
    3. LLM 推断章节（classifier.target_chapter_no）← 仅补充隐含目标；
       不存在 → None（澄清，绝不偷偷回退当前章节，防 LLM 幻觉改错章）
    4. 无法确定 → None（澄清）

    返回 None 一律由调用方走澄清流程（宁可多问一步，不要推断执行）。
    """
    import re

    clean = re.sub(r"\s+", "", content or "")

    # 1) 显式章节号
    m = re.search(r"第\s*([0-9零一二两三四五六七八九十百]+)\s*[章节回]", content)
    if m:
        seq = _parse_chapter_no(m.group(1))
        if seq is not None:
            return await _chapter_by_seq(db, project_id, seq)

    # 2) "本章/这章"指示词 → 前端当前章节
    if any(k in clean for k in _THIS_CHAPTER_WORDS):
        if context and context.get("chapter_no") is not None:
            try:
                seq = int(context["chapter_no"])
            except (TypeError, ValueError):
                seq = None
            if seq is not None:
                return await _chapter_by_seq(db, project_id, seq)
        return None

    # 3) LLM 推断（无显式号、无指示词时）
    if llm_target is not None:
        return await _chapter_by_seq(db, project_id, llm_target)

    # 4) 无法确定
    return None


# ── 任务卡确认 → 生成候选稿 ─────────────────────────────

async def _load_message_with_assistant(db: AsyncSession, session_id: str, message_id: str) -> ChatMessage:
    result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == str(session_id),
            ChatMessage.id == uuid.UUID(str(message_id)),
        )
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise ChatError("消息不存在", 404)
    return msg


async def confirm_task(
    db: AsyncSession,
    session: ChatSession,
    message_id: str,
    *,
    user_id: str,
    confirmed: bool = True,
    overrides: dict | None = None,
    idempotency_key: str | None = None,
    base_version_id: str | None = None,
) -> dict:
    """确认任务卡 → 生成候选稿（只写 generation_records，不覆盖正式正文）。

    幂等：同一幂等号重复调用返回首次响应；同一任务卡已执行过 → 返回当前状态。
    **重新生成语义**：`base_version_id` 显式传入且与任务卡旧值不同时，视为
    "基于最新版本的重新生成"——跳过幂等短路，用新 base_version_id 重建候选；
    否则同 message + 幂等号再次确认永远返回首次结果（无法重新生成）。
    """
    msg = await _load_message_with_assistant(db, str(session.id), message_id)
    if not confirmed:
        raise ChatError("任务卡未确认，未执行任何操作", 400)

    # 找到任务卡与原始 user 消息：
    # - 消息是 assistant → 任务卡在它自己身上，原始 user 消息 = reply_to
    # - 消息是 user → 任务卡在它关联的 assistant 消息上（分类响应里 message_id 即 user 消息 id）
    user_msg = None
    task_card = None
    classified_resp = None  # 分类响应的 response_json（供 unsupported 判定）
    if msg.role == "assistant":
        task_card = msg.task_card
        classified_resp = msg.response_json
        result = await db.execute(
            select(ChatMessage).where(
                ChatMessage.session_id == str(session.id),
                ChatMessage.role == "user",
                ChatMessage.id == msg.reply_to_id,
            )
        )
        user_msg = result.scalar_one_or_none()
    elif msg.role == "user":
        user_msg = msg
        linked = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(session.id),
                    ChatMessage.role == "assistant",
                    ChatMessage.reply_to_id == str(msg.id),
                )
            )
        ).scalars().first()
        if linked is not None:
            # P0-1：unsupported 消息 task_card 为 None，但仍要取 response_json
            # 供 confirm 判定 intent_unsupported（明确拒绝而非通用 400）。
            if linked.task_card is not None:
                task_card = linked.task_card
            classified_resp = linked.response_json
    if user_msg is None or not task_card:
        # P0-1：intent_unsupported 消息（复合任务/定稿）无任务卡 → 明确拒绝，
        # 不得降级成可执行路径（即使绕过前端直接调 confirm）。
        if isinstance(classified_resp, dict) and classified_resp.get("intent_unsupported"):
            raise ChatError(
                classified_resp.get("unsupported_reason") or "该指令暂不支持自动执行",
                422,
                {"code": "intent_unsupported"},
            )
        raise ChatError("该消息没有可确认的任务卡", 400)

    task_card = dict(task_card)
    if overrides:
        task_card = _apply_overrides(task_card, overrides)

    # 重新生成判定：显式 base_version_id 与任务卡旧值不同 → 跳过幂等短路，
    # 用新 base_version_id 重建候选（否则同 message+幂等号永远返回首次结果）
    is_regenerate = (
        base_version_id is not None
        and task_card.get("base_version_id") != base_version_id
    )
    if is_regenerate:
        task_card["base_version_id"] = base_version_id
        if isinstance(task_card.get("target"), dict):
            task_card["target"]["version_id"] = base_version_id

    # 幂等短路（重新生成时跳过：必须真正基于新版本执行）
    if not is_regenerate and msg.role == "assistant" and msg.response_json is not None and msg.execution_id is not None:
        # 已确认并执行过 → 返回首次执行响应（幂等）
        return msg.response_json
    # 幂等号：同会话同接口重复确认 → 返回首次响应。
    # 命名空间前缀避免与 classify 阶段（role=user）的幂等号冲突（同表唯一约束）。
    # 注意：regenerate 只跳过"任务卡已执行"短路，**不得跳过幂等键查询**——
    # 否则同一"新版本+新幂等键"的网络重试会被反复判定为 regenerate，
    # 重复生成候选稿、重复消耗 Token。幂等记录在首次 regenerate 时写入尾部。
    idem_key = f"confirm:{idempotency_key}" if idempotency_key else None
    if idem_key:
        existing = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == str(session.id),
                    ChatMessage.role == "assistant",
                    ChatMessage.idempotency_key == idem_key,
                )
            )
        ).scalars().first()
        if existing is not None and existing.response_json is not None:
            return existing.response_json
        if existing is not None:
            # 并发占位：另一请求已落占位（结果未回填）→ 轮询等待重放首次结果
            return await _wait_for_placeholder(
                db, str(session.id), idem_key, execution_id=existing.execution_id)

    if not confirmed:
        raise ChatError("任务卡未确认，未执行任何操作", 400)

    if task_card.get("operation_type") != "chapter_rewrite":
        raise ChatError("第一版仅支持 chapter_rewrite 单章局部重写", 400, {
            "code": "intent_unsupported",
            "unsupported_reason": "第一版不支持该操作类型，仅支持 chapter_rewrite。",
        })

    target = task_card.get("target") or {}
    chapter = None
    if target.get("chapter_id"):
        chapter = await db.get(Chapter, uuid.UUID(str(target["chapter_id"])))
    if chapter is None and target.get("chapter_no") is not None:
        result = await db.execute(
            select(Chapter).where(
                Chapter.project_id == str(session.project_id),
                Chapter.sequence_number == int(target["chapter_no"]),
            )
        )
        chapter = result.scalar_one_or_none()
    if chapter is None:
        raise ChatError("目标章节不存在", 404)

    effective_base_version_id = task_card.get("base_version_id") or await _current_chapter_version_id(db, str(chapter.id))
    if not effective_base_version_id:
        raise ChatError("目标章节还没有正式版本，请先手动保存一次章节正文", 409, {
            "code": "no_base_version",
            "latest_version_id": None,
        })

    # 一致性 2：重新生成时验证新 base_version_id 属于目标章节，且必须是当前最新正式版本。
    # 防止客户端随便传一个版本号（可能属于其他章节或已过时）作为重新生成基线。
    if is_regenerate:
        from models.chapter_version import ChapterVersion as _CV
        base_ver = await db.get(_CV, uuid.UUID(str(effective_base_version_id)))
        latest_ver = await _current_chapter_version_id(db, str(chapter.id))
        if base_ver is None or str(base_ver.chapter_id) != str(chapter.id):
            raise ChatError("指定的 base_version_id 不属于目标章节", 409, {
                "code": "version_conflict",
                "latest_version_id": latest_ver,
            })
        if latest_ver is not None and effective_base_version_id != latest_ver:
            raise ChatError("指定的 base_version_id 已不是该章节的最新版本", 409, {
                "code": "version_conflict",
                "latest_version_id": latest_ver,
            })

    # 创建执行记录（change_set，pending）
    execution = ChangeSet(
        session_id=str(session.id),
        operation_type="chapter_rewrite",
        intent="rewrite",
        status=ChangeSetStatus.PENDING,
        base_version_id=effective_base_version_id,
    )
    db.add(execution)
    await db.flush()

    # ── 并发幂等闭环（占位先行） ──
    # 先落幂等占位消息（response_json=None，execution_id=execution）并提交，
    # **再调用大模型**。并发相同请求会命中占位 → 轮询等待重放首次结果，
    # 而不是各自调一次模型。唯一约束 (session_id, idempotency_key) 兜底：
    # 若两个请求同时插入占位，后到者 IntegrityError → 重查占位轮询。
    placeholder = None
    if idem_key:
        from sqlalchemy.exc import IntegrityError, OperationalError
        sid_str = str(session.id)
        placeholder = ChatMessage(
            session_id=str(session.id),
            role="assistant",
            content="",
            intent="rewrite",
            execution_id=str(execution.id),
            idempotency_key=idem_key,
            reply_to_id=str(user_msg.id),
            response_json=None,
        )
        db.add(placeholder)
        try:
            await db.flush()
            await db.commit()
        except (IntegrityError, OperationalError):
            # 并发竞争：对方已落占位（唯一约束冲突）或写锁竞争（busy 超时）。
            # rollback + expunge 后本 session 干净（无 pending → SELECT 无 autoflush），
            # 用预取的 sid_str 轮询对方占位，等待其完成并重放首次结果。
            await db.rollback()
            db.sync_session.expunge_all()
            return await _wait_for_placeholder(
                db, sid_str, idem_key, execution_id=None)
        # 模型调用期间释放写锁（占位已提交，并发请求可读）

    # 生成候选稿
    # 重新生成/常规确认都基于 effective base 版本的不可变快照生成
    base_snapshot_content = None
    if effective_base_version_id:
        from models.chapter_version import ChapterVersion as _CV
        base_ver = await db.get(_CV, uuid.UUID(str(effective_base_version_id)))
        if base_ver is not None:
            base_snapshot_content = base_ver.content or ""
    record = await _generate_candidate(
        db, user_id=user_id, project_id=str(session.project_id),
        chapter=chapter, task_card=task_card,
        conversation_id=str(session.id), execution_id=str(execution.id),
        base_snapshot_content=base_snapshot_content,
    )
    if record is None:
        # 失败也必须是单事务：ChangeSet.failed + 占位失败回填一次提交。
        # 若进程在提交前退出，占位仍为空 → stale 机制兜底（接管标 failed），
        # 不留下"ChangeSet 已失败但占位永远 running"的矛盾态。
        execution.status = ChangeSetStatus.FAILED
        failed_response = {
            "execution_id": str(execution.id),
            "status": ExecutionStatus.FAILED,
            "candidate": None,
            "error_message": "候选稿生成失败：LLM provider 不可用或调用失败，请检查 API Key 配置后重试。",
            "change_set": _change_set_dict(execution),
        }
        # 失败也要回填占位（幂等重试可见失败原因，不会卡在 running）
        if placeholder is not None:
            placeholder.response_json = failed_response
        await db.flush()
        await db.commit()
        return failed_response

    # 业务状态先在内存中就位：CAS 填充占位时必须携带完整的 change_set（含 objects），
    # 且 CAS/objects/superseded/消息回填必须落在**同一个事务**里统一提交一次——
    # 否则进程在两次提交之间宕机，会留下"占位显示候选已生成、ChangeSet 无关联
    # 候选稿"的半成品，采用接口会报"该执行没有候选稿"。
    execution.objects = [{
        "object_kind": "chapter",
        "object_id": str(chapter.id),
        "before_ref": effective_base_version_id,
        "candidate_record_id": str(record.id),
        "after_ref": str(record.id),
    }]
    # 最终响应：execution.objects 就位后才构造（正常响应/幂等重放的
    # change_set.objects 都非空，不再出现"成功但 objects 为空"的矛盾体）
    response = {
        "execution_id": str(execution.id),
        "status": ExecutionStatus.PENDING,
        "candidate": _candidate_dict(record),
        "change_set": _change_set_dict(execution),
    }

    # ── 占位 CAS 先行：决定本次执行的命运（成功才写成功响应） ──
    # 若占位已被 stale 接管（ChangeSet 已被接管方标 failed），本次执行必须
    # **整体转为 failed**：候选记录 discarded、接口返回失败，且不得把此前构造的
    # 成功响应写入 msg/返回——否则会出现"占位失败 / ChangeSet 失败 / 候选已丢弃
    # / 原任务消息成功 / 请求返回成功"的矛盾状态。
    from sqlalchemy import update as sa_update
    execution_failed = False
    if idem_key and placeholder is not None:
        fill_result = await db.execute(
            sa_update(ChatMessage)
            .where(ChatMessage.id == placeholder.id)
            .where(sa.text("(response_json IS NULL OR response_json = 'null')"))
            .values(
                content="候选稿已生成，请对照原文决定采用或丢弃。",
                response_json=response,
            )
        )
        # 先看 rowcount，不立即提交：成功时与 objects/superseded/msg 同一事务提交
        if fill_result.rowcount != 1:
            execution_failed = True

    if execution_failed:
        # 整体失败：**回滚当前生成事务**（候选记录/占位填充/ChangeSet 变更全部撤销，
        # 不泄漏任何半成品），再读取 stale 接管方已提交的失败状态返回。
        execution_id_str = str(execution.id)
        msg_id_str = str(msg.id)
        await db.rollback()
        db.sync_session.expunge_all()
        failed_execution = await db.get(ChangeSet, uuid.UUID(execution_id_str))
        failed_response = {
            "execution_id": execution_id_str,
            "status": ExecutionStatus.FAILED,
            "candidate": None,
            "error_message": (
                "候选稿生成超时（生成进程可能已中断），本次结果已作废，请重新发起。"
            ),
            "change_set": _change_set_dict(failed_execution) if failed_execution is not None else None,
        }
        # 任务消息补写失败状态（独立小事务；权威 failed 已由接管方提交，
        # 这里只是让原任务消息与占位/ChangeSet 状态保持一致，不影响幂等重放）
        task_msg = await db.get(ChatMessage, uuid.UUID(msg_id_str))
        if task_msg is not None:
            task_msg.execution_id = execution_id_str
            task_msg.response_json = failed_response
            await db.commit()
        return failed_response

    # 成功路径：superseded + msg 回填与 CAS 同一事务，统一提交一次
    # 重新生成成功后：同章节的其他 candidate 记录标 superseded 并记录替代执行
    if is_regenerate:
        await db.execute(
            sa_update(GenerationRecord)
            .where(GenerationRecord.chapter_id == str(chapter.id))
            .where(GenerationRecord.status == "candidate")
            .where(GenerationRecord.id != record.id)
            .values(status="superseded",
                    superseded_by_execution_id=str(execution.id))
        )
    msg.execution_id = str(execution.id)
    msg.response_json = response
    await db.flush()
    await db.commit()

    if idem_key and placeholder is None:
        # 兼容路径（占位创建失败但幂等键存在；占位 CAS 未执行）
        assistant_reply = ChatMessage(
            session_id=str(session.id),
            role="assistant",
            content="候选稿已生成，请对照原文决定采用或丢弃。",
            intent="rewrite",
            execution_id=str(execution.id),
            idempotency_key=idem_key,
            reply_to_id=str(user_msg.id),
            response_json=response,
        )
        db.add(assistant_reply)
        await db.flush()
        await db.commit()
    return response


def _apply_overrides(task_card: dict, overrides: dict) -> dict:
    """把确认请求的 overrides 合并进任务卡（只覆盖已声明的字段）。"""
    result = dict(task_card)
    if "constraints" in overrides and isinstance(overrides["constraints"], dict):
        constraints = dict(result.get("constraints") or {})
        constraints.update(overrides["constraints"])
        result["constraints"] = constraints
    if "target_word_count" in overrides and overrides["target_word_count"] is not None:
        constraints = dict(result.get("constraints") or {})
        constraints["target_word_count"] = overrides["target_word_count"]
        result["constraints"] = constraints
    return result


async def _generate_candidate(
    db: AsyncSession,
    *,
    user_id: str,
    project_id: str,
    chapter: Chapter,
    task_card: dict,
    conversation_id: str,
    execution_id: str,
    base_snapshot_content: str | None = None,
) -> GenerationRecord | None:
    """调用现有章节生成/润色工作流的上下文组装，生成候选稿落 generation_records。

    生成结果先落 generation_records status=candidate，不覆盖正式正文（§11 决策点 1）。
    """
    from agents.llm_provider import get_llm_provider
    from api.llm_deps import get_user_llm_config
    from services.chapter_context import build_chapter_context, format_chapter_context_for_prompt
    from services.extraction_service import _resolve_provider_name

    provider_name = await _resolve_provider_name(user_id, db)
    try:
        llm_config = await get_user_llm_config(user_id, db)
        provider = get_llm_provider(llm_config)
    except Exception as exc:  # noqa: BLE001 — provider 不可用时记录并返回失败
        logger.warning("candidate 生成 provider 不可用: %s", exc)
        return None

    constraints = task_card.get("constraints") or {}
    target = task_card.get("target") or {}
    # 指令优先级（方案补锁 2）：resolved_instruction（LLM 解析后的具体指令）
    # > 原始消息。两者都保留，prompt 组装用解析后的指令。
    instruction = (
        task_card.get("resolved_instruction")
        or constraints.get("user_instruction")
        or task_card.get("user_instruction")
        or ""
    )
    target_words = constraints.get("target_word_count")

    # 章节上下文（角色/大纲/事件/设定 + 上章锚点）
    ctx = await build_chapter_context(
        db, project_id, chapter.sequence_number, intent="generate",
    )
    context_text = format_chapter_context_for_prompt(ctx)

    # 一致性 2：prompt 必须基于 base 版本快照（ChapterVersion.content，不可变），
    # 而不是可变的 chapter.content——否则用户手动修改正文后重新生成，
    # 会基于错误基线改写。
    current_content = base_snapshot_content if base_snapshot_content is not None else (chapter.content or "")
    preserve = constraints.get("must_preserve") or []
    must_not = constraints.get("must_not_change") or []

    system_prompt = (
        "你是资深小说编辑，负责对单个章节进行局部重写。"
        "你必须基于提供的当前正式版本正文改写，不引入与上下文冲突的新设定。"
        "只输出修改后的完整章节正文，不要任何解释、标题、markdown 标记或“生成内容”字样。"
    )
    parts = [
        f"## 章节信息\n第{chapter.sequence_number}章 {chapter.title}",
        f"## 章节上下文\n{context_text or '(暂无资料)'}",
        f"## 当前正式版本正文\n{current_content or '(当前章节正文为空)'}",
    ]
    if instruction:
        parts.append(f"## 用户修改要求\n{instruction}")
    if preserve:
        parts.append(f"## 必须保留\n" + "\n".join(f"- {item}" for item in preserve))
    if must_not:
        parts.append(f"## 禁止修改\n" + "\n".join(f"- {item}" for item in must_not))
    if target_words:
        parts.append(f"## 目标字数\n约 {target_words} 字")

    user_prompt = "\n\n".join(parts)

    try:
        raw = await provider.generate(
            system_prompt, user_prompt, temperature=0.4, max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("候选稿生成失败 execution=%s: %s", execution_id, exc)
        return None

    clean = sanitize_chapter_content(raw or "")
    if not clean.strip():
        logger.warning("候选稿为空 execution=%s", execution_id)
        return None

    record = GenerationRecord(
        project_id=project_id,
        chapter_id=str(chapter.id),
        mode="chapter_rewrite",
        direction=None,
        user_note=instruction or None,
        target_words=target_words,
        content=clean,
        word_count=len(clean.replace(" ", "").replace("\n", "")),
        status="candidate",
        request_params={
            "provider": provider_name,
            "task_card": task_card,
        },
        # 035 追加列：候选稿归属
        conversation_id=conversation_id,
        base_version_id=task_card.get("base_version_id"),
        task_card_id=execution_id,
    )
    db.add(record)
    await db.flush()
    return record


def _candidate_dict(record: GenerationRecord) -> dict:
    return {
        "record_id": str(record.id),
        "content": record.content,
        "word_count": record.word_count,
        "base_version_id": str(record.base_version_id)
        if getattr(record, "base_version_id", None) else None,
        "status": record.status,
    }


def _change_set_dict(cs: ChangeSet) -> dict:
    return {
        "id": str(cs.id),
        "session_id": str(cs.session_id),
        "execution_id": str(cs.execution_id) if cs.execution_id else None,
        "operation_type": cs.operation_type,
        "intent": cs.intent,
        "status": cs.status,
        "objects": cs.objects,
        "base_version_id": str(cs.base_version_id) if cs.base_version_id else None,
        "created_at": cs.created_at.isoformat() if cs.created_at else None,
    }


async def _execution_response_from_state(
    db: AsyncSession,
    session: ChatSession,
    execution_id: str | None,
) -> dict:
    """幂等重放：按当前执行状态返回响应（不重新生成/回滚/写版本）。"""
    execution = await db.get(ChangeSet, uuid.UUID(str(execution_id)))
    if execution is None:
        raise ChatError("执行不存在", 404)

    record = None
    if execution.objects:
        # 候选稿记录 id 单独存（after_ref 在 accept 后指向版本 id）
        candidate_id = execution.objects[0].get("candidate_record_id") or execution.objects[0].get("after_ref")
        if candidate_id:
            record = await db.get(GenerationRecord, uuid.UUID(str(candidate_id)))

    if record is None:
        return {
            "execution_id": str(execution.id),
            "status": ExecutionStatus.FAILED,
            "candidate": None,
            "change_set": _change_set_dict(execution),
        }

    # ChangeSet.failed 必须明确返回 failed——即使存在候选记录（被 stale 接管后
    # 旧进程晚到结果已 discarded），也不得返回 pending/成功。
    if execution.status == ChangeSetStatus.FAILED:
        return {
            "execution_id": str(execution.id),
            "status": ExecutionStatus.FAILED,
            "candidate": None,
            "error_message": "候选稿生成超时（生成进程可能已中断），本次结果已作废，请重新发起。",
            "change_set": _change_set_dict(execution),
        }
    status = ExecutionStatus.ACCEPTED if execution.status == ChangeSetStatus.APPLIED else (
        ExecutionStatus.DISCARDED if execution.status == ChangeSetStatus.REVERTED
        else ExecutionStatus.PENDING
    )
    response = {
        "execution_id": str(execution.id),
        "status": status,
        "candidate": _candidate_dict(record),
        "change_set": _change_set_dict(execution),
    }
    if execution.status == ChangeSetStatus.APPLIED and execution.objects:
        after_ref = execution.objects[0].get("after_ref")
        if after_ref:
            response["change_set_id"] = str(execution.id)
            response["new_version_id"] = after_ref
    return response


# ── 候选稿采用（版本冲突检测 + 同事务） ─────────────────

async def _load_execution(db: AsyncSession, session: ChatSession, execution_id: str) -> ChangeSet:
    execution = await db.get(ChangeSet, uuid.UUID(str(execution_id)))
    if execution is None or str(execution.session_id) != str(session.id):
        raise ChatError("执行不存在", 404)
    return execution


async def accept_candidate(
    db: AsyncSession,
    session: ChatSession,
    execution_id: str,
) -> dict:
    """采用候选稿。

    重查 当前章节版本 == base_version_id，不一致 → 409 version_conflict。
    一致 → 写正式章节版本 + change_set 落库，两者在同一事务（任一侧失败整体回滚）。
    """
    execution = await _load_execution(db, session, execution_id)
    # 幂等：已采用/已回滚 → 返回当前状态
    if execution.status == ChangeSetStatus.APPLIED:
        return await _execution_response_from_state(db, session, execution_id)
    if execution.status == ChangeSetStatus.REVERTED:
        raise ChatError("该候选稿已丢弃/回滚，无法采用", 409, {
            "code": "already_discarded",
            "latest_version_id": None,
        })

    if not execution.objects:
        raise ChatError("该执行没有候选稿", 404)
    object_item = execution.objects[0]
    record = await db.get(GenerationRecord, uuid.UUID(str(object_item.get("after_ref"))))
    if record is None or record.status == "discarded":
        raise ChatError("候选稿不存在或已丢弃", 404)
    if record.status == "applied":
        # 幂等：已 applied → 返回当前状态
        return await _execution_response_from_state(db, session, execution_id)

    base_version_id = str(execution.base_version_id) if execution.base_version_id else None
    if not base_version_id:
        raise ChatError("缺少 base_version_id，无法校验版本冲突", 409, {
            "code": "no_base_version",
            "latest_version_id": None,
        })

    chapter = await db.get(Chapter, uuid.UUID(str(object_item.get("object_id"))))
    if chapter is None:
        raise ChatError("目标章节不存在", 404)

    # 版本冲突检测：当前正式版本 == base_version_id
    current_version_id = await _current_chapter_version_id(db, str(chapter.id))
    if current_version_id != base_version_id:
        raise ChatError(
            "当前章节已被修改，候选稿基于旧版本，请基于最新版本重新生成。",
            409,
            {"code": "version_conflict", "latest_version_id": current_version_id},
        )

    # 同事务：写章节正式版本 + change_set applied + generation record applied
    from services.chapter_save import save_chapter_content

    content = record.content
    clean = sanitize_chapter_content(content)
    chapter.content = clean
    chapter.word_count = len(clean.replace(" ", "").replace("\n", ""))
    version = await create_version(
        db, chapter.id, clean, source="ai_enhance",
        project_id=str(chapter.project_id),
        parent_version_id=uuid.UUID(base_version_id) if base_version_id else None,
    )
    if version is None:
        raise ChatError("候选稿内容为空，无法采用", 400)
    # 对话式“采用”生成的是新的正式版本。必须同步更新章节指针，
    # 否则后续上下文仍会读取旧 final_version_id，造成刚采用的内容像是丢失。
    chapter.final_version_id = version.id

    # 更新 change_set（与版本写入同事务）；candidate_record_id 保留供幂等重放定位候选稿
    execution.status = ChangeSetStatus.APPLIED
    execution.applied_at = datetime.now(timezone.utc)
    execution.objects = [{
        "object_kind": "chapter",
        "object_id": str(chapter.id),
        "before_ref": base_version_id,
        "candidate_record_id": str(record.id),
        "after_ref": str(version.id),
    }]

    # generation record 标 applied 并关联版本
    record.status = "applied"
    record.accepted_version_id = str(version.id)

    await db.commit()
    return {
        "change_set_id": str(execution.id),
        "new_version_id": str(version.id),
        "status": ExecutionStatus.ACCEPTED,
    }


# ── 丢弃候选稿 ─────────────────────────────────────────

async def discard_candidate(
    db: AsyncSession,
    session: ChatSession,
    execution_id: str,
) -> dict:
    """丢弃候选稿（不写正式数据）。幂等：已丢弃/已回滚/已采用返回当前状态。"""
    execution = await _load_execution(db, session, execution_id)
    if execution.status == ChangeSetStatus.REVERTED:
        return _discard_state(execution)
    if execution.status == ChangeSetStatus.APPLIED:
        raise ChatError("该候选稿已采用，请使用撤销接口回滚", 409, {
            "code": "already_applied",
            "latest_version_id": None,
        })
    if execution.status == ChangeSetStatus.FAILED:
        return _discard_state(execution)

    # 标记 generation record discarded
    if execution.objects:
        after_ref = execution.objects[0].get("after_ref")
        if after_ref:
            record = await db.get(GenerationRecord, uuid.UUID(str(after_ref)))
            if record is not None and record.status == "candidate":
                record.status = "discarded"
    execution.status = ChangeSetStatus.REVERTED
    execution.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return _discard_state(execution)


def _discard_state(execution: ChangeSet) -> dict:
    return {
        "execution_id": str(execution.id),
        "status": ExecutionStatus.DISCARDED,
        "change_set": _change_set_dict(execution),
    }


# ── 继续修改（重新生成候选稿） ──────────────────────────

async def revise_candidate(
    db: AsyncSession,
    session: ChatSession,
    execution_id: str,
    revise_instruction: str,
) -> dict:
    """按 revise_instruction 重新生成候选稿。

    原候选稿未采用时允许；已采用/已回滚返回当前状态（幂等语义）。
    """
    execution = await _load_execution(db, session, execution_id)
    if execution.status == ChangeSetStatus.APPLIED:
        raise ChatError("该候选稿已采用，无法继续修改", 409, {
            "code": "already_applied",
            "latest_version_id": None,
        })
    if execution.status == ChangeSetStatus.REVERTED:
        raise ChatError("该候选稿已丢弃/回滚，无法继续修改", 409, {
            "code": "already_discarded",
            "latest_version_id": None,
        })
    if execution.status == ChangeSetStatus.FAILED:
        raise ChatError("该执行已失败，请重新发起确认", 409)

    if not execution.objects:
        raise ChatError("该执行没有候选稿", 404)
    object_item = execution.objects[0]

    chapter = await db.get(Chapter, uuid.UUID(str(object_item.get("object_id"))))
    if chapter is None:
        raise ChatError("目标章节不存在", 404)

    task_card = {
        "schema_version": 1,
        "operation_type": "chapter_rewrite",
        "target": {
            "chapter_no": chapter.sequence_number,
            "chapter_id": str(chapter.id),
            "chapter_title": chapter.title,
        },
        "base_version_id": str(execution.base_version_id) if execution.base_version_id else None,
        "constraints": {
            "must_preserve": [],
            "must_not_change": ["人物设定", "大纲"],
            "target_word_count": None,
            "user_instruction": revise_instruction,
        },
        "risk_level": "L2",
        "confirmation_required": True,
    }

    record = await _generate_candidate(
        db, user_id=str(session.user_id), project_id=str(session.project_id),
        chapter=chapter, task_card=task_card,
        conversation_id=str(session.id), execution_id=str(execution.id),
    )
    if record is None:
        raise ChatError("候选稿重新生成失败", 502)

    execution.objects = [{
        "object_kind": "chapter",
        "object_id": str(chapter.id),
        "before_ref": str(execution.base_version_id) if execution.base_version_id else None,
        "after_ref": str(record.id),
    }]
    await db.commit()
    return {
        "execution_id": str(execution.id),
        "status": ExecutionStatus.PENDING,
        "candidate": _candidate_dict(record),
        "change_set": _change_set_dict(execution),
    }


# ── change_set 撤销 ────────────────────────────────────

async def revert_change_set(
    db: AsyncSession,
    session: ChatSession,
    change_set_id: str,
) -> dict:
    """撤销单个 change_set：恢复对象 before_ref（章节正文恢复 base 版本快照）。

    幂等：已 reverted → 返回当前状态；pending（未采用）→ 直接标记 reverted。
    """
    cs = await db.get(ChangeSet, uuid.UUID(str(change_set_id)))
    if cs is None or str(cs.session_id) != str(session.id):
        raise ChatError("变更集不存在", 404)
    if cs.status == ChangeSetStatus.REVERTED:
        return _change_set_dict(cs)
    if cs.status == ChangeSetStatus.FAILED:
        raise ChatError("该变更集处于失败状态，无需撤销", 409)

    if cs.status == ChangeSetStatus.PENDING:
        # 候选稿未采用：丢弃候选并标记 reverted（无版本写入）
        if cs.objects:
            after_ref = cs.objects[0].get("after_ref")
            if after_ref:
                record = await db.get(GenerationRecord, uuid.UUID(str(after_ref)))
                if record is not None and record.status == "candidate":
                    record.status = "discarded"
        cs.status = ChangeSetStatus.REVERTED
        cs.reverted_at = datetime.now(timezone.utc)
        await db.commit()
        return _change_set_dict(cs)

    # applied → 恢复章节正文到 base_version 快照（rollback 版本 + generation discarded）
    if not cs.objects:
        raise ChatError("变更集缺少对象记录", 409)
    object_item = cs.objects[0]
    chapter = await db.get(Chapter, uuid.UUID(str(object_item.get("object_id"))))
    base_version_id = str(cs.base_version_id) if cs.base_version_id else None
    base_version = None
    if base_version_id:
        base_version = await db.get(ChapterVersion, uuid.UUID(base_version_id))
    if chapter is None or base_version is None:
        raise ChatError("撤销所需对象缺失，无法回滚", 409, {
            "code": "revert_target_missing",
            "latest_version_id": None,
        })

    from services.chapter_save import save_chapter_content

    await save_chapter_content(
        db, chapter, base_version.content or "",
        source="rollback",
        rollback_from_version_id=base_version_id,
    )
    cs.status = ChangeSetStatus.REVERTED
    cs.reverted_at = datetime.now(timezone.utc)
    if object_item.get("candidate_record_id") or object_item.get("after_ref"):
        record = await db.get(
            GenerationRecord,
            uuid.UUID(str(object_item.get("candidate_record_id") or object_item["after_ref"])),
        )
        if record is not None and record.status == "applied":
            record.status = "discarded"
    await db.commit()
    return _change_set_dict(cs)


async def revert_session(
    db: AsyncSession,
    session: ChatSession,
) -> dict:
    """按时间逆序回滚本轮会话全部已应用（applied）的 change_set。幂等。"""
    result = await db.execute(
        select(ChangeSet).where(
            ChangeSet.session_id == str(session.id),
            ChangeSet.status == ChangeSetStatus.APPLIED,
        ).order_by(ChangeSet.created_at.asc())
    )
    applied = list(result.scalars().all())

    reverted_items = []
    for cs in reversed(applied):
        item = await revert_change_set(db, session, str(cs.id))
        reverted_items.append({
            "change_set_id": str(cs.id),
            "status": item["status"],
        })

    return {
        "session_id": str(session.id),
        "reverted_count": len(reverted_items),
        "items": reverted_items,
    }
