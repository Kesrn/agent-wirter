"""Phase H1: 写作记忆 staging 数据层测试"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"

from models.base import Base
import models.project  # noqa: F401
import models.chapter  # noqa: F401
import models.writing_memory_staging  # noqa: F401


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_memory_staging_status_enum():
    from models.harness_enums import MemoryStagingStatus
    assert MemoryStagingStatus.GENERATED == "GENERATED"
    assert MemoryStagingStatus.CONFIRMED == "CONFIRMED"
    assert MemoryStagingStatus.REJECTED == "REJECTED"


def test_memory_type_enum():
    from models.harness_enums import MemoryType
    for t in ["CHARACTER", "WORLD_RULE", "PLOT_FACT", "EVENT", "FORESHADOWING"]:
        assert hasattr(MemoryType, t)


@pytest.mark.asyncio
async def test_writing_memory_staging_create(async_db):
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType

    item = WritingMemoryStaging(
        project_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        chapter_id="33333333-3333-3333-3333-333333333333",
        chapter_version_id="44444444-4444-4444-4444-444444444444",
        chapter_sequence_number=3,
        memory_type=MemoryType.CHARACTER,
        title="角色A",
        payload={"name": "角色A", "role_type": "protagonist", "profile": "冷酷杀手"},
        evidence="角色A在第三章出手击杀目标",
        status=MemoryStagingStatus.GENERATED,
    )
    async_db.add(item)
    await async_db.flush()

    assert item.id is not None
    assert item.status == MemoryStagingStatus.GENERATED
    assert item.memory_type == MemoryType.CHARACTER
    assert item.payload["name"] == "角色A"
    assert item.evidence == "角色A在第三章出手击杀目标"
    assert item.chapter_sequence_number == 3
    assert item.reviewed_at is None
    assert item.confirmed_target_type is None
    assert item.confirmed_target_id is None
    assert item.reviewed_by is None
    assert item.idempotency_key is None


@pytest.mark.asyncio
async def test_writing_memory_staging_status_transition(async_db):
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from datetime import datetime, timezone

    item = WritingMemoryStaging(
        project_id="55555555-5555-5555-5555-555555555555",
        memory_type=MemoryType.WORLD_RULE,
        title="魔法体系A",
        payload={},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
    )
    async_db.add(item)
    await async_db.flush()

    item.status = MemoryStagingStatus.CONFIRMED
    item.reviewed_at = datetime.now(timezone.utc)
    item.reviewed_by = "user123"
    item.confirmed_target_type = "WorldEntry"
    item.confirmed_target_id = "66666666-6666-6666-6666-666666666666"
    await async_db.flush()

    assert item.status == MemoryStagingStatus.CONFIRMED
    assert item.reviewed_at is not None
    assert item.reviewed_by == "user123"
    assert item.confirmed_target_type == "WorldEntry"


@pytest.mark.asyncio
async def test_writing_memory_staging_columns_exist(async_db):
    """验证表结构包含所有修正字段。"""
    cols = {c.name: c for c in Base.metadata.tables["writing_memory_staging"].columns}
    for name in [
        "id", "project_id", "run_id", "chapter_id", "chapter_version_id",
        "chapter_sequence_number", "memory_type", "title", "payload", "evidence",
        "status", "confirmed_target_type", "confirmed_target_id",
        "reviewed_by", "reviewed_at", "idempotency_key",
        "created_at", "updated_at",
    ]:
        assert name in cols, f"writing_memory_staging 缺列 {name}"
    # payload 不应是 nullable（有 default）
    assert cols["payload"].nullable is False or cols["payload"].default is not None


# ==================== API 测试 ====================
# 复用 smoke test 的 SQLite 引擎 + TestClient

import asyncio
import json
import tests.test_smoke as smoke
from db.session import get_db, set_engine
from main import app

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


def _auth_headers(username="msuser", password="mspass"):
    if username in _cached_auth:
        return {"Authorization": f"Bearer {_cached_auth[username]}"}
    client.post("/api/auth/register", json={"username": username, "password": password})
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    _cached_auth[username] = token
    return {"Authorization": f"Bearer {token}"}


def _create_project_and_staging(headers, memory_type="CHARACTER"):
    """建项目 + 直接写一条 staging 记录，返回 (project_id, staging_id)。"""
    proj = client.post("/api/projects", json={"title": "staging test", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]

    # 通过 DB 直接插入 staging 记录
    async def _insert():
        from models.writing_memory_staging import WritingMemoryStaging
        from models.harness_enums import MemoryStagingStatus, MemoryType
        async with _test_session_factory() as s:
            item = WritingMemoryStaging(
                project_id=pid,
                memory_type=MemoryType(memory_type),
                title="测试记忆",
                payload={"name": "角色X"},
                evidence="证据文本",
                status=MemoryStagingStatus.GENERATED,
                chapter_sequence_number=1,
            )
            s.add(item)
            await s.commit()
            await s.refresh(item)
            return str(item.id)

    staging_id = asyncio.new_event_loop().run_until_complete(_insert())
    return pid, staging_id


def test_list_memory_staging_by_project():
    headers = _auth_headers("mslist", "mslistpass")
    pid, sid = _create_project_and_staging(headers)

    resp = client.get(f"/api/projects/{pid}/memory-staging", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == sid
    assert data[0]["status"] == "GENERATED"
    assert data[0]["memory_type"] == "CHARACTER"


def test_list_memory_staging_status_filter():
    headers = _auth_headers("msfilter", "msfilterpass")
    pid, sid = _create_project_and_staging(headers)

    # confirm 一条
    client.post(f"/api/projects/{pid}/memory-staging/{sid}/confirm", headers=headers)

    # 查 GENERATED → 空
    resp = client.get(f"/api/projects/{pid}/memory-staging?status=GENERATED", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 0

    # 查 CONFIRMED → 1 条
    resp = client.get(f"/api/projects/{pid}/memory-staging?status=CONFIRMED", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_confirm_staging_happy_path():
    headers = _auth_headers("msconfirm", "msconfirmpass")
    pid, sid = _create_project_and_staging(headers)

    resp = client.post(f"/api/projects/{pid}/memory-staging/{sid}/confirm", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CONFIRMED"
    assert data["reviewed_at"] is not None
    assert data["reviewed_by"] is not None


def test_confirm_staging_idempotent():
    headers = _auth_headers("msidem", "msidempass")
    pid, sid = _create_project_and_staging(headers)

    r1 = client.post(f"/api/projects/{pid}/memory-staging/{sid}/confirm", headers=headers)
    assert r1.status_code == 200
    r2 = client.post(f"/api/projects/{pid}/memory-staging/{sid}/confirm", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "CONFIRMED"


def test_reject_staging_idempotent():
    headers = _auth_headers("msrej", "msrejpass")
    pid, sid = _create_project_and_staging(headers)

    r1 = client.post(f"/api/projects/{pid}/memory-staging/{sid}/reject", headers=headers)
    assert r1.status_code == 200
    r2 = client.post(f"/api/projects/{pid}/memory-staging/{sid}/reject", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "REJECTED"


def test_cross_state_409():
    """CONFIRMED → REJECT 返回 409；REJECTED → CONFIRM 返回 409。"""
    headers = _auth_headers("ms409", "ms409pass")

    # CONFIRMED → REJECT
    pid1, sid1 = _create_project_and_staging(headers, "CHARACTER")
    client.post(f"/api/projects/{pid1}/memory-staging/{sid1}/confirm", headers=headers)
    resp = client.post(f"/api/projects/{pid1}/memory-staging/{sid1}/reject", headers=headers)
    assert resp.status_code == 409

    # REJECTED → CONFIRM
    pid2, sid2 = _create_project_and_staging(headers, "WORLD_RULE")
    client.post(f"/api/projects/{pid2}/memory-staging/{sid2}/reject", headers=headers)
    resp = client.post(f"/api/projects/{pid2}/memory-staging/{sid2}/confirm", headers=headers)
    assert resp.status_code == 409


def test_staging_not_found():
    headers = _auth_headers("ms404", "ms404pass")
    proj = client.post("/api/projects", json={"title": "404 test", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = client.post(f"/api/projects/{pid}/memory-staging/{fake_id}/confirm", headers=headers)
    assert resp.status_code == 404


def test_staging_cross_project_isolation():
    """用户 A 的 staging 对用户 B 不可见。"""
    headers_a = _auth_headers("msisoA", "msisoApass")
    headers_b = _auth_headers("msisoB", "msisoBpass")

    pid_a, sid_a = _create_project_and_staging(headers_a)

    # B 看不到 A 的 staging
    resp = client.get(f"/api/projects/{pid_a}/memory-staging", headers=headers_b)
    assert resp.status_code == 404

    # B 不能 confirm A 的 staging
    resp = client.post(f"/api/projects/{pid_a}/memory-staging/{sid_a}/confirm", headers=headers_b)
    assert resp.status_code == 404


# ==================== H2: FactExtractionAgent parse 测试 ====================

def test_parse_facts_valid_json():
    """完整 JSON 应解析为 memories 列表。"""
    from agents.fact_extraction import parse_facts

    raw = '{"memories": [{"memory_type": "CHARACTER", "title": "角色A", "payload": {"name": "角色A", "role_type": "protagonist"}, "evidence": "角色A出手击杀"}]}'
    facts = parse_facts(raw)
    assert len(facts) == 1
    assert facts[0]["memory_type"] == "CHARACTER"
    assert facts[0]["title"] == "角色A"
    assert facts[0]["payload"]["name"] == "角色A"
    assert facts[0]["evidence"] == "角色A出手击杀"


def test_parse_facts_json_in_prose():
    """JSON 嵌在散文中应通过 regex 提取。"""
    from agents.fact_extraction import parse_facts

    raw = '以下是抽取结果：\n{"memories": [{"memory_type": "WORLD_RULE", "title": "魔法体系A", "payload": {"content": "火系克制木系"}, "evidence": "原文提到火克木"}]}\n以上。'
    facts = parse_facts(raw)
    assert len(facts) == 1
    assert facts[0]["memory_type"] == "WORLD_RULE"
    assert facts[0]["title"] == "魔法体系A"


def test_parse_facts_parse_failure():
    """纯文本（非 JSON）应容错返回空列表。"""
    from agents.fact_extraction import parse_facts

    raw = "本章未发现需要记录的新设定。"
    facts = parse_facts(raw)
    assert facts == []


def test_parse_facts_missing_fields():
    """JSON 缺字段时应填默认值。"""
    from agents.fact_extraction import parse_facts

    raw = '{"memories": [{"title": "事件A"}]}'
    facts = parse_facts(raw)
    assert len(facts) == 1
    assert facts[0]["memory_type"] == "PLOT_FACT"  # 默认
    assert facts[0]["title"] == "事件A"
    assert facts[0]["payload"] == {}  # 默认
    assert facts[0]["evidence"] is None  # 默认


# ==================== H2: staging service 测试 ====================

@pytest.mark.asyncio
async def test_create_staging_from_extraction_writes_rows(async_db):
    """create_staging_from_extraction 应写入 staging 记录，字段正确。"""
    from services.memory_staging_service import create_staging_from_extraction

    facts = [
        {"memory_type": "CHARACTER", "title": "角色A", "payload": {"name": "角色A"}, "evidence": "证据A"},
        {"memory_type": "WORLD_RULE", "title": "魔法体系", "payload": {"content": "火克木"}, "evidence": "证据B"},
    ]
    created = await create_staging_from_extraction(
        async_db,
        project_id="11111111-1111-1111-1111-111111111111",
        chapter_id="22222222-2222-2222-2222-222222222222",
        chapter_version_id="33333333-3333-3333-3333-333333333333",
        chapter_sequence_number=3,
        run_id="44444444-4444-4444-4444-444444444444",
        facts=facts,
    )
    await async_db.flush()

    assert len(created) == 2
    assert created[0].memory_type == "CHARACTER"
    assert created[0].title == "角色A"
    assert created[0].payload["name"] == "角色A"
    assert created[0].evidence == "证据A"
    assert created[0].status == "GENERATED"
    assert created[0].chapter_sequence_number == 3
    assert created[0].idempotency_key is not None
    assert created[1].memory_type == "WORLD_RULE"


@pytest.mark.asyncio
async def test_create_staging_idempotent_same_chapter_version(async_db):
    """同一 chapter_version_id 已存在任何 staging 记录时 skip（不限 status）。"""
    from services.memory_staging_service import create_staging_from_extraction
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType

    # 预插入一条 CONFIRMED 记录
    existing = WritingMemoryStaging(
        project_id="55555555-5555-5555-5555-555555555555",
        chapter_version_id="66666666-6666-6666-6666-666666666666",
        memory_type=MemoryType.CHARACTER,
        title="已有记忆",
        payload={},
        status=MemoryStagingStatus.CONFIRMED,
    )
    async_db.add(existing)
    await async_db.flush()

    # 再次抽取同一 version → 应 skip
    facts = [{"memory_type": "CHARACTER", "title": "新角色", "payload": {}, "evidence": None}]
    created = await create_staging_from_extraction(
        async_db,
        project_id="55555555-5555-5555-5555-555555555555",
        chapter_id=None,
        chapter_version_id="66666666-6666-6666-6666-666666666666",
        chapter_sequence_number=1,
        run_id=None,
        facts=facts,
    )
    assert created == []  # skip


@pytest.mark.asyncio
async def test_create_staging_idempotency_key_format(async_db):
    """idempotency_key 格式应为 {chapter_version_id}:{index}。"""
    from services.memory_staging_service import create_staging_from_extraction

    facts = [
        {"memory_type": "CHARACTER", "title": "A", "payload": {}, "evidence": None},
        {"memory_type": "EVENT", "title": "B", "payload": {}, "evidence": None},
    ]
    created = await create_staging_from_extraction(
        async_db,
        project_id="77777777-7777-7777-7777-777777777777",
        chapter_id=None,
        chapter_version_id="88888888-8888-8888-8888-888888888888",
        chapter_sequence_number=1,
        run_id=None,
        facts=facts,
    )
    await async_db.flush()
    assert created[0].idempotency_key == "88888888-8888-8888-8888-888888888888:0"
    assert created[1].idempotency_key == "88888888-8888-8888-8888-888888888888:1"


# ==================== H3a: confirm 写入正式表 ====================

@pytest.mark.asyncio
async def test_confirm_staging_character_writes_character(async_db):
    """confirm CHARACTER → Character upsert (find-or-create by project_id + name)。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a角色测试", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.CHARACTER,
        title="角色A",
        payload={"name": "角色A", "role_type": "protagonist", "profile": "冷酷杀手", "faction": "暗影"},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=1,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    await async_db.flush()

    assert target_type == "Character"
    assert target_id is not None

    from models.character import Character
    from sqlalchemy import select
    char = (await async_db.execute(
        select(Character).where(Character.project_id == pid, Character.name == "角色A")
    )).scalar_one_or_none()
    assert char is not None
    assert char.role_type == "protagonist"
    assert char.profile == "冷酷杀手"
    assert char.faction == "暗影"
    assert str(staging.confirmed_target_id) == target_id


@pytest.mark.asyncio
async def test_confirm_staging_world_rule_writes_world_entry(async_db):
    """confirm WORLD_RULE → WorldEntry upsert (find-or-create by project_id + title)。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a设定测试", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.WORLD_RULE,
        title="魔法体系A",
        payload={"category": "magic", "content": "火系克制木系"},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=2,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    await async_db.flush()

    assert target_type == "WorldEntry"
    assert target_id is not None

    from models.world_entry import WorldEntry
    from sqlalchemy import select
    entry = (await async_db.execute(
        select(WorldEntry).where(WorldEntry.project_id == pid, WorldEntry.title == "魔法体系A")
    )).scalar_one_or_none()
    assert entry is not None
    assert entry.category == "magic"
    assert entry.content == "火系克制木系"


@pytest.mark.asyncio
async def test_confirm_staging_event_writes_character_event(async_db):
    """confirm EVENT → CharacterEvent upsert (有 character_name 时)。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a事件测试", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.EVENT,
        title="角色A战斗",
        payload={"character_name": "角色A", "event_summary": "角色A击败敌人", "state_change": "实力提升"},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=3,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    await async_db.flush()

    assert target_type == "CharacterEvent"
    assert target_id is not None

    from models.character_event import CharacterEvent
    from models.character import Character
    from sqlalchemy import select
    # Character 被 find-or-create
    char = (await async_db.execute(
        select(Character).where(Character.project_id == pid, Character.name == "角色A")
    )).scalar_one_or_none()
    assert char is not None
    # CharacterEvent 被 upsert
    event = (await async_db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == pid,
            CharacterEvent.character_id == char.id,
            CharacterEvent.chapter_sequence_number == 3,
        )
    )).scalar_one_or_none()
    assert event is not None
    assert event.event_summary == "角色A击败敌人"
    assert event.state_change == "实力提升"


@pytest.mark.asyncio
async def test_confirm_staging_event_without_character_name_skips(async_db):
    """confirm EVENT 无 character_name → 不写 CharacterEvent，target 为 None。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a无角色事件", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.EVENT,
        title="无名事件",
        payload={"event_summary": "某事件"},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=1,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    assert target_type is None
    assert target_id is None


@pytest.mark.asyncio
async def test_confirm_staging_plot_fact_no_formal_write(async_db):
    """confirm PLOT_FACT → 不写正式表，target 为 None。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a剧情事实", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.PLOT_FACT,
        title="主角觉醒",
        payload={"description": "主角在第3章觉醒了能力"},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=3,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    assert target_type is None
    assert target_id is None


@pytest.mark.asyncio
async def test_confirm_staging_foreshadowing_writes_hidden_thread(async_db):
    """confirm FORESHADOWING → HiddenThread upsert (find-or-create by project_id + name)。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a伏笔测试", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.FORESHADOWING,
        title="神秘戒指",
        payload={"description": "主角捡到的戒指有未知力量", "chapter_nums": [1, 3]},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=1,
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    await async_db.flush()

    assert target_type == "HiddenThread"
    assert target_id is not None

    from models.hidden_thread import HiddenThread
    from sqlalchemy import select
    thread = (await async_db.execute(
        select(HiddenThread).where(HiddenThread.project_id == pid, HiddenThread.name == "神秘戒指")
    )).scalar_one_or_none()
    assert thread is not None
    assert thread.description == "主角捡到的戒指有未知力量"
    assert thread.chapter_nums == [1, 3]


@pytest.mark.asyncio
async def test_confirm_staging_idempotent_already_confirmed(async_db):
    """已 CONFIRMED 且有 confirmed_target_id → 直接返回，不重复写。"""
    import uuid
    from models.project import Project
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.memory_staging_service import confirm_staging_item

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3a幂等", mode="novel"))
    await async_db.flush()

    staging = WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.CHARACTER,
        title="角色B",
        payload={"name": "角色B"},
        status=MemoryStagingStatus.CONFIRMED,
        confirmed_target_type="Character",
        confirmed_target_id="99999999-9999-9999-9999-999999999999",
    )
    async_db.add(staging)
    await async_db.flush()

    target_type, target_id = await confirm_staging_item(async_db, staging, background_tasks=None)
    # 直接返回已有 target，不重新写
    assert target_type == "Character"
    assert target_id == "99999999-9999-9999-9999-999999999999"


# ==================== H3b: Context Builder 注入 confirmed 记忆 ====================

@pytest.mark.asyncio
async def test_context_builder_includes_confirmed_memories(async_db):
    """build_chapter_context 应包含 CONFIRMED staging 记忆。"""
    import uuid
    from models.project import Project
    from models.chapter import Chapter
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.chapter_context import build_chapter_context, format_chapter_context_for_prompt

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3b上下文测试", mode="novel"))
    await async_db.flush()

    async_db.add(Chapter(
        id=uuid.uuid4(), project_id=pid, title="第3章",
        sequence_number=3, content="正文", status="draft",
    ))
    await async_db.flush()

    # 一条 CONFIRMED 的 PLOT_FACT（来源第2章，<= 当前第3章）
    async_db.add(WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.PLOT_FACT,
        title="主角觉醒",
        payload={"description": "主角在第2章觉醒了能力"},
        status=MemoryStagingStatus.CONFIRMED,
        chapter_sequence_number=2,
    ))
    # 一条 GENERATED（未确认，不应出现）
    async_db.add(WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.CHARACTER,
        title="角色X",
        payload={"name": "角色X"},
        status=MemoryStagingStatus.GENERATED,
        chapter_sequence_number=2,
    ))
    await async_db.flush()

    ctx = await build_chapter_context(async_db, pid, 3)
    # confirmed_memories 应有 1 条（PLOT_FACT）
    assert len(ctx.confirmed_memories) == 1
    assert ctx.confirmed_memories[0].title == "主角觉醒"

    # prompt 中应有 ## 已确认记忆
    prompt = format_chapter_context_for_prompt(ctx)
    assert "## 已确认记忆" in prompt
    assert "主角觉醒" in prompt


@pytest.mark.asyncio
async def test_context_builder_confirmed_seq_filter(async_db):
    """chapter_sequence_number > 当前章节的 CONFIRMED 不应注入。"""
    import uuid
    from models.project import Project
    from models.chapter import Chapter
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from services.chapter_context import build_chapter_context

    pid = uuid.uuid4()
    async_db.add(Project(id=pid, title="H3b seq filter", mode="novel"))
    await async_db.flush()
    async_db.add(Chapter(
        id=uuid.uuid4(), project_id=pid, title="第2章",
        sequence_number=2, content="正文", status="draft",
    ))
    await async_db.flush()

    # CONFIRMED 来源第1章（<= 2）→ 应注入
    async_db.add(WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.WORLD_RULE,
        title="规则A",
        payload={"content": "规则A内容"},
        status=MemoryStagingStatus.CONFIRMED,
        chapter_sequence_number=1,
    ))
    # CONFIRMED 来源第5章（> 2）→ 不应注入
    async_db.add(WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.WORLD_RULE,
        title="规则B",
        payload={"content": "规则B内容"},
        status=MemoryStagingStatus.CONFIRMED,
        chapter_sequence_number=5,
    ))
    # CONFIRMED 来源 NULL → 应注入
    async_db.add(WritingMemoryStaging(
        project_id=pid,
        memory_type=MemoryType.FORESHADOWING,
        title="伏笔A",
        payload={"description": "伏笔A"},
        status=MemoryStagingStatus.CONFIRMED,
        chapter_sequence_number=None,
    ))
    await async_db.flush()

    ctx = await build_chapter_context(async_db, pid, 2)
    titles = [m.title for m in ctx.confirmed_memories]
    assert "规则A" in titles
    assert "规则B" not in titles  # seq 5 > 2
    assert "伏笔A" in titles      # seq None → 注入
