"""对话式创作 PG 集成测试（真实 PostgreSQL，RUN_PG_TESTS=1 才执行）。

参照 tests/test_split_generation_pg.py 的建库/清理/单 loop 模式：
  1. alembic 035 在真实 PG 上执行（四张新表 + generation_records 三列）；
  2. 表/列类型断言（UUID、JSONB）；
  3. 服务级 roundtrip：chat_sessions / chat_messages / change_sets 在 PG 上
     GUID + JSON 读写正常。

用法：
  RUN_PG_TESTS=1 venv/bin/python -m pytest tests/test_conversation_pg.py -q
"""

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.change_set import ChangeSet
from models.chat_conversation import ChatMessage, ChatSession, TaskConstraint

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1",
    reason="PG 集成测试需 RUN_PG_TESTS=1（真实 PostgreSQL）",
)

PG_HOST = "localhost"
PG_PORT = "5432"
DB_NAME = f"ai_creative_chat_test_{uuid.uuid4().hex[:8]}"
PG_URL = f"postgresql+asyncpg://{PG_HOST}:{PG_PORT}/{DB_NAME}"

# asyncpg 连接池绑定创建时的 event loop：模块级单 loop，避免跨 loop 使用。
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pg_engine():
    """创建测试库 → 真实跑 alembic head（含 035）→ 提供 async engine。"""
    subprocess.run(["createdb", "-h", PG_HOST, "-p", PG_PORT, DB_NAME],
                   check=False, capture_output=True)

    from config.settings import settings as app_settings
    object.__setattr__(app_settings, "DATABASE_URL", PG_URL)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_async_engine(PG_URL)
    yield engine

    _run(engine.dispose())
    subprocess.run(["dropdb", "--if-exists", "-h", PG_HOST, "-p", PG_PORT, DB_NAME],
                   check=False, capture_output=True)
    _LOOP.close()


@pytest.fixture(scope="module")
def factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


# ── 1. 迁移与表结构 ─────────────────────────────────────

def test_pg_migration_schema(pg_engine):
    async def _check():
        async with pg_engine.connect() as conn:
            for tbl in ("chat_sessions", "chat_messages", "task_constraints", "change_sets"):
                exists = (await conn.execute(text(
                    "SELECT to_regclass('public." + tbl + "')"))).scalar()
                assert exists is not None, f"表 {tbl} 不存在"

            # generation_records 三列
            gen_cols = {
                r[0] for r in (await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='generation_records'"
                ))).fetchall()
            }
            for col in ("conversation_id", "base_version_id", "task_card_id",
                        "superseded_by_execution_id"):
                assert col in gen_cols, f"generation_records 缺列 {col}"
            gen_indexes = {row[0] for row in (await conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename='generation_records'"
            ))).fetchall()}
            assert "ix_generation_records_superseded_by" in gen_indexes, \
                "generation_records 缺 superseded 索引"

            # 列类型：PG 上 id / project_id / user_id / base_version_id 为 UUID，
            # plan / task_card / objects 为 jsonb
            uuid_cols = {
                ("chat_sessions", "id"), ("chat_sessions", "project_id"),
                ("chat_sessions", "user_id"),
                ("chat_messages", "id"), ("chat_messages", "session_id"),
                ("change_sets", "id"), ("change_sets", "session_id"),
                ("change_sets", "base_version_id"),
            }
            for table, col in uuid_cols:
                data_type = (await conn.execute(text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name=:t AND column_name=:c"
                ), {"t": table, "c": col})).scalar()
                assert data_type == "uuid", f"{table}.{col} 期望 uuid，实际 {data_type}"

            json_cols = {
                ("chat_messages", "plan"), ("chat_messages", "task_card"),
                ("chat_messages", "response_json"), ("change_sets", "objects"),
            }
            for table, col in json_cols:
                data_type = (await conn.execute(text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name=:t AND column_name=:c"
                ), {"t": table, "c": col})).scalar()
                assert data_type == "jsonb", f"{table}.{col} 期望 jsonb，实际 {data_type}"

            # 约束：每用户每项目唯一 / 会话内幂等号唯一 / FK
            uqs = {
                r[0] for r in (await conn.execute(text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type='UNIQUE' AND table_name IN "
                    "('chat_sessions','chat_messages')"
                ))).fetchall()
            }
            assert "uq_chat_sessions_user_project" in uqs
            assert "uq_chat_messages_session_idempotency" in uqs

            fks = {
                r[0] for r in (await conn.execute(text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type='FOREIGN KEY' AND table_name IN "
                    "('chat_sessions','chat_messages','task_constraints','change_sets')"
                ))).fetchall()
            }
            for fk in ("fk_chat_sessions_project", "fk_chat_sessions_user",
                       "fk_chat_messages_session", "fk_task_constraints_session",
                       "fk_change_sets_session"):
                assert fk in fks, f"缺外键 {fk}"
    _run(_check())


# ── 2. 服务 roundtrip（PG 双后端读写） ──────────────────

def test_pg_chat_roundtrip(factory):
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    async def _go():
        async with factory() as session:
            # 先建 project + user（PG 上 chat_sessions 有 FK 约束）
            await session.execute(
                text("INSERT INTO users (id, username, hashed_password, is_active) "
                     "VALUES (:id, :name, 'x', true)"),
                {"id": str(user_id), "name": f"pg_chat_{user_id.hex[:6]}"},
            )
            await session.execute(
                text("INSERT INTO projects (id, owner_id, title, mode) "
                     "VALUES (:id, :owner, 'PG 对话项目', 'novel')"),
                {"id": str(project_id), "owner": str(user_id)},
            )

            cs = ChatSession(id=session_id, project_id=project_id, user_id=user_id,
                             title="PG 会话")
            session.add(cs)
            await session.flush()

            msg = ChatMessage(
                session_id=session_id, role="user", content="重写第十章",
                intent="rewrite",
                task_card={"operation_type": "chapter_rewrite", "target": {"chapter_no": 10}},
                idempotency_key="pg-idem-1",
            )
            session.add(msg)
            await session.flush()

            cs_obj = (await session.execute(
                select(ChangeSet)
                .where(ChangeSet.id == session_id)  # 不存在的执行；仅验证类型兼容
            )).scalar_one_or_none()
            assert cs_obj is None  # ChangeSet 表可查询（空）

            constraint = TaskConstraint(
                session_id=session_id, content="不能删除伏笔",
                source_message_id=msg.id, status="active",
            )
            session.add(constraint)
            await session.commit()

            # 读回
            msg2 = (await session.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
            )).scalar_one()
            assert msg2.intent == "rewrite"
            assert msg2.task_card["target"]["chapter_no"] == 10
            constraint2 = (await session.execute(
                select(TaskConstraint).where(TaskConstraint.session_id == session_id)
            )).scalar_one()
            assert constraint2.content == "不能删除伏笔"
    _run(_go())


# ── 3. PG 真并发幂等（占位先行，双连接） ────────────────

def test_pg_concurrent_confirm_idempotent(pg_engine, factory):
    """PG 双连接并发 confirm_task：同一幂等键并发两次 →
    只允许一个生成任务、同一 execution_id、一条 GenerationRecord。

    PG 竞争行为与 SQLite 不同（READ COMMITTED 下占位 INSERT 后立即可见、
    唯一约束冲突即时抛出），必须真实验证。
    """
    from models.chapter import Chapter
    from models.chapter_version import ChapterVersion
    from models.generation_record import GenerationRecord
    from models.project import Project
    from models.user import User
    from services.conversation_service import confirm_task

    async def _setup():
        async with factory() as s:
            user = User(username=f"pgconc_{uuid.uuid4().hex[:8]}", hashed_password="x")
            s.add(user)
            await s.flush()
            project = Project(owner_id=str(user.id), title="PG并发", mode="novel")
            s.add(project)
            await s.flush()
            chapter = Chapter(project_id=project.id, title="第一章", sequence_number=1,
                              content="第一章原始正文。PG 并发测试。", status="draft")
            s.add(chapter)
            await s.flush()
            base_ver = ChapterVersion(chapter_id=str(chapter.id),
                                      content="第一章原始正文。PG 并发测试。", version_number=1)
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
                "schema_version": 1, "operation_type": "chapter_rewrite",
                "target": {"chapter_id": str(chapter.id), "chapter_no": 1,
                           "chapter_title": "第一章", "version_id": str(base_ver.id)},
                "base_version_id": str(base_ver.id),
                "constraints": {"must_preserve": [], "must_not_change": ["人物设定"],
                                "target_word_count": None, "user_instruction": "重写第一章"},
                "risk_level": "L2", "confirmation_required": True, "idempotency_key": None,
            }
            assistant_msg = ChatMessage(session_id=str(chat.id), role="assistant",
                                        content="", intent="rewrite", task_card=task_card,
                                        reply_to_id=str(user_msg.id))
            s.add(assistant_msg)
            await s.commit()
            return (str(user.id), str(chat.id), str(assistant_msg.id), str(base_ver.id))
    uid, sid, mid, base = _run(_setup())
    key = f"pgconc-{uuid.uuid4().hex}"

    async def _confirm_once():
        async with factory() as s:
            chat = await s.get(ChatSession, uuid.UUID(sid))
            return await confirm_task(
                s, chat, mid, user_id=uid, confirmed=True,
                idempotency_key=key, base_version_id=base)

    async def _run_concurrent():
        return await asyncio.gather(_confirm_once(), _confirm_once())

    r1, r2 = _run(_run_concurrent())

    # 同一 execution_id（只允许一个生成任务）
    assert r1["execution_id"] == r2["execution_id"], \
        f"PG 并发应返回同一 execution：{r1['execution_id']} vs {r2['execution_id']}"
    assert r1["status"] != "failed" and r2["status"] != "failed"

    # 只产生一条 GenerationRecord
    async def _count():
        async with factory() as s:
            recs = (await s.execute(
                select(GenerationRecord).where(
                    GenerationRecord.task_card_id == r1["execution_id"])
            )).scalars().all()
            return len(recs)
    assert _run(_count()) == 1
