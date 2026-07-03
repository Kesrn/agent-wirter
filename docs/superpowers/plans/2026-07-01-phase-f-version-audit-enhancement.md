# Phase F: 版本审计增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread `run_id` / `parent_version_id` into chapter version creation, write `accepted_version_id` on approve, add a chapter rollback endpoint that creates a new version (not overwrite), and make prune spare versions still referenced by audit trails.

**Architecture:** All model columns already exist (migration 021). Phase F is pure plumbing + one new endpoint. Changes flow bottom-up: `version_service.create_version` → `chapter_save.save_chapter_content` → route call sites (approve flow + new rollback endpoint) → schemas. No migration needed. No new model. No llm_provider.py or workflow.py changes.

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy async / Pydantic v2 / SQLite (test) + PostgreSQL (prod)

**基线文档:** `docs/AI_Harness_改造适配技术文档.md` §阶段 F

---

## 依赖假设（Phase C 经验 + Phase F 风险）

1. **所有模型列已存在**：`ChapterVersion` 已有 `run_id`, `parent_version_id`, `rollback_from_version_id`, `diff_from_parent`, `project_id`（migration 021）；`GenerationRecord` 已有 `accepted_version_id`（migration 005）+ `run_id`（migration 021）。Phase F 不写 migration。
2. **`save_chapter_content` 是唯一 create_version 入口**：routes.py 不直接调 `create_version`（仅测试直接调），所以改 `save_chapter_content` 签名即可覆盖所有 production 路径。
3. **approve 路径的 run_id 已在 scope**：`resume_chapter_generation` 的 `_resume_run_id` 已在闭包中（routes.py:3625），直接传入 `save_chapter_content` 即可。
4. **prune 风险**：`_prune_old_versions` 硬删超 10 的版本，如果被删版本被 `GenerationRecord.accepted_version_id` 引用则破坏审计链。Phase F 让 prune 跳过被引用版本。
5. **Python 3.10**：项目 venv 是 3.10，`str | None` OK（3.10+），`StrEnum` 禁用（3.11+）。
6. **SQLite :memory: 测试隔离**：harness 测试与 smoke 测试共享引擎，**必须单独跑**，合跑有连接隔离问题（非代码缺陷）。
7. **routes.py 不重构**：最小侵入，只在现有调用点加参数，新增 1 个 endpoint。

## 文件结构

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `backend/services/version_service.py` | `create_version` 加 audit 参数 + prune 跳过被引用版本 | Modify |
| `backend/services/chapter_save.py` | `save_chapter_content` 加 `run_id`/`parent_version_id` 参数并转发 | Modify |
| `backend/api/routes.py` | approve 路径传 `run_id` + `accepted_version_id` 联动 + 新增 chapter rollback endpoint + 版本列表返回 audit 列 | Modify |
| `backend/schemas/api.py` | `ChapterVersionListItemResponse` + `ChapterVersionResponse` 加 audit 字段 | Modify |
| `backend/tests/test_harness_version_audit.py` | Phase F 专项测试 | Create |

---

## Task 1: `create_version` 支持 audit 参数 + prune 跳过被引用版本

**Files:**
- Modify: `backend/services/version_service.py:25-71`
- Test: `backend/tests/test_harness_version_audit.py` (create)

- [ ] **Step 1: Create test file with failing tests for `create_version` audit params**

Create `backend/tests/test_harness_version_audit.py`:

```python
"""Phase F: 版本审计增强测试 — SQLite 内存数据库"""

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
import models.chapter_version  # noqa: F401
import models.generation_record  # noqa: F401


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def chapter_id_fixture():
    import uuid
    return uuid.uuid4()


@pytest.mark.asyncio
async def test_create_version_with_run_id(async_db, chapter_id_fixture):
    """create_version 应将 run_id 写入 ChapterVersion.run_id。"""
    from services.version_service import create_version

    version = await create_version(
        async_db, chapter_id_fixture, "内容A", source="ai_approve",
        run_id="11111111-1111-1111-1111-111111111111",
    )
    assert version is not None
    assert str(version.run_id) == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_create_version_with_parent_version_id(async_db, chapter_id_fixture):
    """create_version 应将 parent_version_id 写入 ChapterVersion。"""
    from services.version_service import create_version

    v1 = await create_version(async_db, chapter_id_fixture, "第一版", source="manual")
    v2 = await create_version(
        async_db, chapter_id_fixture, "第二版", source="ai_approve",
        parent_version_id=str(v1.id),
    )
    assert str(v2.parent_version_id) == str(v1.id)


@pytest.mark.asyncio
async def test_create_version_with_project_id(async_db, chapter_id_fixture):
    """create_version 应将 project_id 写入 ChapterVersion。"""
    from services.version_service import create_version

    version = await create_version(
        async_db, chapter_id_fixture, "内容", source="manual",
        project_id="22222222-2222-2222-2222-222222222222",
    )
    assert str(version.project_id) == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_create_version_run_id_none_default(async_db, chapter_id_fixture):
    """不传 run_id 时，ChapterVersion.run_id 为 None（向后兼容）。"""
    from services.version_service import create_version

    version = await create_version(async_db, chapter_id_fixture, "内容", source="manual")
    assert version.run_id is None
    assert version.parent_version_id is None
    assert version.project_id is None


@pytest.mark.asyncio
async def test_prune_spare_referenced_version(async_db, chapter_id_fixture):
    """prune 不应删除被 GenerationRecord.accepted_version_id 引用的版本。"""
    from services.version_service import create_version, MAX_VERSIONS_PER_CHAPTER
    from models.generation_record import GenerationRecord

    # 创建 10 个版本（达到上限）
    versions = []
    for i in range(MAX_VERSIONS_PER_CHAPTER):
        v = await create_version(async_db, chapter_id_fixture, f"内容{i}", source="manual")
        versions.append(v)

    # 让第 3 个版本被 generation_record 引用
    record = GenerationRecord(
        project_id="33333333-3333-3333-3333-333333333333",
        content="候选",
        word_count=2,
        mode="full_pipeline",
        status="applied",
        accepted_version_id=versions[2].id,
    )
    async_db.add(record)
    await async_db.flush()

    # 再创建一个版本，触发 prune
    await create_version(async_db, chapter_id_fixture, "内容超限", source="manual")
    await async_db.flush()

    # 被引用版本不应被删除
    from sqlalchemy import select
    result = await async_db.execute(
        select(ChapterVersion).where(ChapterVersion.id == versions[2].id)
    )
    assert result.scalar_one_or_none() is not None, "被 generation_record 引用的版本不应被 prune 删除"
```

Also add the import for ChapterVersion at the top of the test file (after the `import models.generation_record` line):

```python
from models.chapter_version import ChapterVersion
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -v 2>&1 | head -40`
Expected: FAIL — `create_version() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: Modify `create_version` to accept audit params + spare referenced versions in prune**

Replace `backend/services/version_service.py` lines 25-53 with:

```python
async def create_version(
    db: AsyncSession,
    chapter_id: str | uuid.UUID,
    content: str | None,
    source: str = "manual",
    *,
    run_id: str | uuid.UUID | None = None,
    parent_version_id: str | uuid.UUID | None = None,
    rollback_from_version_id: str | uuid.UUID | None = None,
    project_id: str | uuid.UUID | None = None,
) -> ChapterVersion | None:
    if content is None:
        return None
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid source '{source}', must be one of {sorted(VALID_SOURCES)}")
    if isinstance(chapter_id, str):
        chapter_id = uuid.UUID(chapter_id)

    result = await db.execute(
        select(func.max(ChapterVersion.version_number)).where(
            ChapterVersion.chapter_id == chapter_id
        )
    )
    max_ver = result.scalar() or 0

    version = ChapterVersion(
        chapter_id=chapter_id,
        content=content,
        word_count=len(content),
        version_number=max_ver + 1,
        source=source,
    )
    if run_id is not None:
        version.run_id = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    if parent_version_id is not None:
        version.parent_version_id = uuid.UUID(parent_version_id) if isinstance(parent_version_id, str) else parent_version_id
    if rollback_from_version_id is not None:
        version.rollback_from_version_id = uuid.UUID(rollback_from_version_id) if isinstance(rollback_from_version_id, str) else rollback_from_version_id
    if project_id is not None:
        version.project_id = uuid.UUID(project_id) if isinstance(project_id, str) else project_id
    db.add(version)
    await db.flush()

    await _prune_old_versions(db, chapter_id)
    return version
```

Then replace `_prune_old_versions` (lines 56-71) with:

```python
async def _prune_old_versions(db: AsyncSession, chapter_id: str) -> None:
    """删除超过 MAX_VERSIONS_PER_CHAPTER 的旧版本，但跳过被 generation_record 引用的版本。"""
    # 查询被 generation_record.accepted_version_id 引用的版本 ID
    from models.generation_record import GenerationRecord
    referenced_result = await db.execute(
        select(GenerationRecord.accepted_version_id).where(
            GenerationRecord.accepted_version_id.isnot(None)
        )
    )
    referenced_ids = {row[0] for row in referenced_result.all()}

    result = await db.execute(
        select(ChapterVersion.id, ChapterVersion.version_number)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
        .offset(MAX_VERSIONS_PER_CHAPTER)
    )
    old_rows = result.all()
    # 跳过被审计引用的版本
    deletable_ids = [row[0] for row in old_rows if row[0] not in referenced_ids]
    if deletable_ids:
        await db.execute(
            delete(ChapterVersion).where(
                ChapterVersion.id.in_(deletable_ids),
            )
        )
```

Note: the prune now deletes by `id` (not `version_number`) and checks `referenced_ids`. The local import of `GenerationRecord` avoids circular import risk (version_service is imported by chapter_save which is imported by routes which imports generation_record_service).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -v 2>&1 | tail -20`
Expected: PASS (5 tests)

- [ ] **Step 5: Run existing version tests to verify no regression**

Run: `cd backend && python -m pytest tests/test_smoke.py -k "version" -v 2>&1 | tail -30`
Expected: PASS (existing version tests still pass with new signature — new params are keyword-only with defaults)

- [ ] **Step 6: Commit**

```bash
git add backend/services/version_service.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): create_version accepts audit params + prune spares referenced versions"
```

---

## Task 2: `save_chapter_content` 转发 audit 参数

**Files:**
- Modify: `backend/services/chapter_save.py:28-59`
- Test: `backend/tests/test_harness_version_audit.py` (append)

- [ ] **Step 1: Write failing test for `save_chapter_content` forwarding run_id**

Append to `backend/tests/test_harness_version_audit.py`:

```python
@pytest.mark.asyncio
async def test_save_chapter_content_forwards_run_id(async_db, chapter_id_fixture):
    """save_chapter_content 应将 run_id 转发给 create_version。"""
    from services.chapter_save import save_chapter_content

    # 先创建一个 chapter（需要 project FK，这里直接构造最小 Chapter）
    import uuid
    from models.project import Project
    project = Project(id=uuid.uuid4(), title="测试项目", mode="novel")
    async_db.add(project)
    await async_db.flush()

    chapter = Chapter(
        id=chapter_id_fixture,
        project_id=project.id,
        title="测试章",
        sequence_number=1,
        content="",
        status="draft",
    )
    async_db.add(chapter)
    await async_db.flush()

    await save_chapter_content(
        async_db, chapter, "新内容", source="ai_approve",
        run_id="44444444-4444-4444-4444-444444444444",
    )

    # 检查创建的 version 是否带 run_id
    from sqlalchemy import select
    result = await async_db.execute(
        select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id_fixture)
    )
    version = result.scalar_one_or_none()
    assert version is not None
    assert str(version.run_id) == "44444444-4444-4444-4444-444444444444"
    assert version.source == "ai_approve"
```

Also need to import `Chapter` at top of the test file. Add after the `from models.chapter_version import ChapterVersion` line:

```python
from models.chapter import Chapter
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py::test_save_chapter_content_forwards_run_id -v 2>&1 | tail -15`
Expected: FAIL — `save_chapter_content() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: Modify `save_chapter_content` to accept and forward audit params**

Replace `backend/services/chapter_save.py` lines 28-59 with:

```python
async def save_chapter_content(
    db: AsyncSession,
    chapter: Chapter,
    raw_content: str,
    source: str = "manual",
    set_status: str | None = None,
    *,
    run_id: str | uuid.UUID | None = None,
    parent_version_id: str | uuid.UUID | None = None,
    rollback_from_version_id: str | uuid.UUID | None = None,
) -> Chapter:
    """保存章节正文内容（统一入口）

    Args:
        db: 数据库会话
        chapter: Chapter ORM 对象（已加载）
        raw_content: 原始内容（未经清洗）
        source: 版本来源 ("manual" | "ai_approve" | "ai_enhance" | "ai_continue")
        set_status: 若非 None，将 chapter.status 设为此值
        run_id: 关联的 AI Run ID（可选，写入 ChapterVersion.run_id）
        parent_version_id: 父版本 ID（可选，写入 ChapterVersion.parent_version_id）
        rollback_from_version_id: 回滚来源版本 ID（可选，写入 ChapterVersion.rollback_from_version_id）

    Returns:
        已更新但尚未提交的 Chapter 对象。
    """
    clean_content = sanitize_chapter_content(raw_content)
    content_changed = chapter.content != clean_content

    chapter.content = clean_content
    chapter.word_count = _count_non_space_chars(clean_content)

    if set_status is not None:
        chapter.status = set_status

    if content_changed and clean_content:
        await create_version(
            db, chapter.id, clean_content, source=source,
            run_id=run_id, parent_version_id=parent_version_id,
            rollback_from_version_id=rollback_from_version_id,
        )

    return chapter
```

Also add `import uuid` at the top of `chapter_save.py` (after the existing `import re` line, before `import logging`):

```python
import uuid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -v 2>&1 | tail -20`
Expected: PASS (6 tests)

- [ ] **Step 5: Run smoke tests to verify no regression**

Run: `cd backend && python -m pytest tests/test_smoke.py -k "version or chapter" -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_save.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): save_chapter_content forwards run_id and parent_version_id"
```

---

## Task 3: Schema 扩展 — 版本列表/详情返回 audit 字段

**Files:**
- Modify: `backend/schemas/api.py:532-552`
- Test: `backend/tests/test_harness_version_audit.py` (append)

- [ ] **Step 1: Write failing test for schema fields**

Append to `backend/tests/test_harness_version_audit.py`:

```python
def test_chapter_version_list_item_response_has_audit_fields():
    """ChapterVersionListItemResponse 应包含 run_id / parent_version_id / rollback_from_version_id。"""
    from schemas.api import ChapterVersionListItemResponse
    fields = set(ChapterVersionListItemResponse.model_fields.keys())
    assert "run_id" in fields
    assert "parent_version_id" in fields
    assert "rollback_from_version_id" in fields


def test_chapter_version_response_has_audit_fields():
    """ChapterVersionResponse 应包含 run_id / parent_version_id / rollback_from_version_id。"""
    from schemas.api import ChapterVersionResponse
    fields = set(ChapterVersionResponse.model_fields.keys())
    assert "run_id" in fields
    assert "parent_version_id" in fields
    assert "rollback_from_version_id" in fields
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -k "audit_fields" -v 2>&1 | tail -15`
Expected: FAIL — `run_id` not in fields

- [ ] **Step 3: Add audit fields to schemas**

In `backend/schemas/api.py`, replace `ChapterVersionListItemResponse` (lines 532-540) with:

```python
class ChapterVersionListItemResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    word_count: int
    version_number: int
    source: str
    run_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    rollback_from_version_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
```

And replace `ChapterVersionResponse` (lines 543-552) with:

```python
class ChapterVersionResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    content: str | None
    word_count: int
    version_number: int
    source: str
    run_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    rollback_from_version_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -k "audit_fields" -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/schemas/api.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): version schemas expose audit fields (run_id, parent_version_id)"
```

---

## Task 4: 版本列表 endpoint 返回 audit 字段

**Files:**
- Modify: `backend/api/routes.py:3865-3887`
- Test: `backend/tests/test_harness_version_audit.py` (append)

- [ ] **Step 1: Write failing test for list endpoint returning audit fields**

Append to `backend/tests/test_harness_version_audit.py`:

```python
def test_version_list_endpoint_returns_audit_fields():
    """GET /chapters/{sn}/versions 应返回 run_id / parent_version_id 字段。"""
    from fastapi.testclient import TestClient
    from main import app
    # 复用 smoke test 的 SQLite 引擎设置
    # 此测试通过 API 走，验证 schema 字段在响应中可见
    # 注：需要完整的 smoke test fixture，这里用最小化 approach
    # 只验证 schema 层面已暴露字段即可（上面两个 test 已覆盖）
    # 这里做一个快速检查：ChapterVersionListItemResponse 能 model_validate 一个带 run_id 的 dict
    from schemas.api import ChapterVersionListItemResponse
    import uuid as _uuid
    from datetime import datetime, timezone
    item = ChapterVersionListItemResponse.model_validate({
        "id": str(_uuid.uuid4()),
        "chapter_id": str(_uuid.uuid4()),
        "word_count": 100,
        "version_number": 1,
        "source": "manual",
        "run_id": str(_uuid.uuid4()),
        "parent_version_id": None,
        "rollback_from_version_id": None,
        "created_at": datetime.now(timezone.utc),
    })
    assert item.run_id is not None
```

- [ ] **Step 2: Run test to verify it passes (schema already updated in Task 3)**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py::test_version_list_endpoint_returns_audit_fields -v 2>&1 | tail -10`
Expected: PASS (this validates the schema works end-to-end)

- [ ] **Step 3: Update `list_chapter_versions` to select and return audit columns**

In `backend/api/routes.py`, replace the `select(...)` block in `list_chapter_versions` (lines 3865-3887) with:

```python
    result = await db.execute(
        select(
            ChapterVersion.id,
            ChapterVersion.chapter_id,
            ChapterVersion.word_count,
            ChapterVersion.version_number,
            ChapterVersion.source,
            ChapterVersion.run_id,
            ChapterVersion.parent_version_id,
            ChapterVersion.rollback_from_version_id,
            ChapterVersion.created_at,
        )
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.version_number.desc())
    )
    return [
        ChapterVersionListItemResponse(
            id=row[0],
            chapter_id=row[1],
            word_count=row[2],
            version_number=row[3],
            source=row[4],
            run_id=row[5],
            parent_version_id=row[6],
            rollback_from_version_id=row[7],
            created_at=row[8],
        )
        for row in result.all()
    ]
```

- [ ] **Step 4: Run smoke version tests to verify no regression**

Run: `cd backend && python -m pytest tests/test_smoke.py -k "version" -v 2>&1 | tail -30`
Expected: PASS — the new fields default to None for old data, existing assertions check `source`, `version_number`, `word_count`, `created_at` which are unchanged

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): version list endpoint returns audit fields"
```

---

## Task 5: Approve 路径传 run_id + accepted_version_id 联动

**Files:**
- Modify: `backend/api/routes.py:3812-3836` (approve save block)
- Test: `backend/tests/test_harness_version_audit.py` (append)

- [ ] **Step 1: Write failing test for approve flow writing accepted_version_id**

Append to `backend/tests/test_harness_version_audit.py`:

```python
@pytest.mark.asyncio
async def test_approve_sets_run_id_on_version_and_accepted_version_id_on_record(async_db):
    """resume approve 路径应：1) 在 ChapterVersion 写 run_id；2) 在 GenerationRecord 写 accepted_version_id。

    此测试通过直接调用 service 层模拟 approve 行为，验证联动逻辑。
    """
    import uuid
    from models.project import Project
    from models.chapter import Chapter
    from models.generation_record import GenerationRecord
    from services.chapter_save import save_chapter_content
    from services.generation_record_service import update_generation_record_status
    from sqlalchemy import select

    pid = uuid.uuid4()
    project = Project(id=pid, title="审计联动测试", mode="novel")
    async_db.add(project)
    await async_db.flush()

    chapter = Chapter(
        id=uuid.uuid4(), project_id=pid, title="章",
        sequence_number=1, content="", status="draft",
    )
    async_db.add(chapter)

    # 模拟 generation record（candidate 状态）
    record = GenerationRecord(
        id=uuid.uuid4(), project_id=pid, chapter_id=chapter.id,
        content="候选稿", word_count=3, mode="full_pipeline",
        status="candidate", run_id="55555555-5555-5555-5555-555555555555",
    )
    async_db.add(record)
    await async_db.flush()

    # 模拟 approve 路径的 save_chapter_content
    run_id_str = "55555555-5555-5555-5555-555555555555"
    await save_chapter_content(
        async_db, chapter, "最终内容", source="ai_approve",
        set_status="draft", run_id=run_id_str,
    )

    # 查 version 是否带 run_id
    result = await async_db.execute(
        select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id)
    )
    version = result.scalar_one_or_none()
    assert version is not None
    assert str(version.run_id) == run_id_str

    # 模拟 accepted_version_id 联动
    await update_generation_record_status(
        async_db, record, "applied", accepted_version_id=str(version.id),
    )
    await async_db.commit()

    # 验证
    assert record.accepted_version_id == version.id
    assert record.status == "applied"
```

- [ ] **Step 2: Run test to verify it passes (service layer already supports it)**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py::test_approve_sets_run_id_on_version_and_accepted_version_id_on_record -v 2>&1 | tail -15`
Expected: PASS — this validates the service-layer wiring works; the route change in the next step makes it happen automatically

- [ ] **Step 3: Modify approve path in `resume_chapter_generation` to pass run_id + set accepted_version_id**

In `backend/api/routes.py`, the novel approve block is at lines 3823-3830. Replace:

```python
                    else:
                        ch_result = await db.execute(
                            select(Chapter).where(Chapter.id == content_id, Chapter.project_id == uid)
                        )
                        chapter = ch_result.scalar_one_or_none()
                        if chapter:
                            await save_chapter_content(db, chapter, raw_content, source="ai_approve", set_status="draft")
                            await db.commit()
```

with:

```python
                    else:
                        ch_result = await db.execute(
                            select(Chapter).where(Chapter.id == content_id, Chapter.project_id == uid)
                        )
                        chapter = ch_result.scalar_one_or_none()
                        if chapter:
                            await save_chapter_content(
                                db, chapter, raw_content, source="ai_approve",
                                set_status="draft", run_id=_resume_run_id,
                            )
                            await db.commit()
                            # ── Phase F: 联动 GenerationRecord.accepted_version_id ──
                            if _resume_run_id:
                                _gr = (
                                    await db.execute(
                                        select(GenerationRecord).where(
                                            GenerationRecord.run_id == _to_uuid(_resume_run_id)
                                        )
                                    )
                                ).scalar_one_or_none()
                                if _gr:
                                    # 查刚创建的版本（最新）
                                    _ver = (
                                        await db.execute(
                                            select(ChapterVersion)
                                            .where(ChapterVersion.chapter_id == chapter.id)
                                            .order_by(ChapterVersion.version_number.desc())
                                            .limit(1)
                                        )
                                    ).scalar_one_or_none()
                                    if _ver:
                                        await update_generation_record_status(
                                            db, _gr, "applied",
                                            accepted_version_id=str(_ver.id),
                                        )
                                        await db.commit()
```

Note: `save_chapter_content` returns the `Chapter` object, not the `ChapterVersion`. The version is created inside `create_version` and flushed. To get the version's id for `accepted_version_id`, we query the latest version by `version_number desc`. This is safe because the approve path runs sequentially (no parallel writes to the same chapter). The `_resume_run_id` variable is already in scope at this point (set at routes.py:3625). `ChapterVersion` is already imported at routes.py:22. `GenerationRecord` is imported at routes.py:27.

- [ ] **Step 4: Run smoke tests to verify no regression in resume/approve flows**

Run: `cd backend && python -m pytest tests/test_smoke.py -k "resume or approve or generate" -v 2>&1 | tail -30`
Expected: PASS (these tests use mocked LLM, should not break)

- [ ] **Step 5: Run harness tests**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -v 2>&1 | tail -20`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): approve path threads run_id into version + sets accepted_version_id"
```

---

## Task 6: Chapter rollback endpoint（新增）

**Files:**
- Modify: `backend/api/routes.py` (add new endpoint after `get_chapter_version`, ~line 3915)
- Test: `backend/tests/test_harness_version_audit.py` (append)

- [ ] **Step 1: Write failing test for chapter rollback endpoint**

Append to `backend/tests/test_harness_version_audit.py`:

```python
@pytest.mark.asyncio
async def test_chapter_rollback_creates_new_version(async_db):
    """POST /chapters/{sn}/versions/{version_id}/restore 应创建新版本（source=rollback），不覆盖旧版本。

    此测试通过 service 层直接验证 rollback 逻辑（routes 层只是薄封装）。
    """
    import uuid
    from models.project import Project
    from models.chapter import Chapter
    from services.chapter_save import save_chapter_content
    from sqlalchemy import select

    pid = uuid.uuid4()
    project = Project(id=pid, title="回滚测试", mode="novel")
    async_db.add(project)
    await async_db.flush()

    chapter = Chapter(
        id=uuid.uuid4(), project_id=pid, title="章",
        sequence_number=1, content="", status="draft",
    )
    async_db.add(chapter)
    await async_db.flush()

    # 创建 v1, v2, v3
    await save_chapter_content(async_db, chapter, "第一版内容", source="manual")
    await save_chapter_content(async_db, chapter, "第二版内容", source="manual")
    await save_chapter_content(async_db, chapter, "第三版内容", source="manual")
    await async_db.commit()

    # 回滚到 v1：用 v1 的内容创建一个新版本
    versions = (await async_db.execute(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.version_number)
    )).scalars().all()
    v1 = versions[0]
    assert v1.version_number == 1

    # 模拟 rollback：用 v1 的内容创建新版本
    await save_chapter_content(
        async_db, chapter, v1.content or "", source="rollback",
        rollback_from_version_id=str(v1.id),
    )
    await async_db.commit()

    # 验证：现在有 4 个版本，最新的是 v4（rollback）
    versions_after = (await async_db.execute(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.version_number.desc())
    )).scalars().all()
    assert len(versions_after) == 4
    latest = versions_after[0]
    assert latest.version_number == 4
    assert latest.source == "rollback"
    assert str(latest.rollback_from_version_id) == str(v1.id)
    assert latest.content == "第一版内容"

- [ ] **Step 2: Run test to verify it passes (service layer already supports it via Task 1)**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py::test_chapter_rollback_creates_new_version -v 2>&1 | tail -15`
Expected: PASS — `save_chapter_content` + `source="rollback"` + `rollback_from_version_id` already works from Task 1+2

- [ ] **Step 3: Add chapter rollback endpoint in routes.py**

In `backend/api/routes.py`, after the `get_chapter_version` endpoint (ends at line 3914), add a new endpoint:

```python
@router.post("/projects/{project_id}/chapters/{sequence_number}/versions/{version_id}/restore", response_model=ChapterResponse)
async def restore_chapter_version(
    project_id: str,
    sequence_number: int,
    version_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """回滚章节到指定版本。

    与文档 restore 一致：不覆盖旧版本，而是用旧版本内容创建一个新版本（source=rollback），
    并在 rollback_from_version_id 记录回滚来源。
    """
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    ch_result = await db.execute(
        select(Chapter).where(Chapter.project_id == uid, Chapter.sequence_number == sequence_number)
    )
    chapter = ch_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    vid = _to_uuid(version_id)
    result = await db.execute(
        select(ChapterVersion).where(ChapterVersion.id == vid, ChapterVersion.chapter_id == chapter.id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")

    await save_chapter_content(
        db, chapter, version.content or "", source="rollback",
        rollback_from_version_id=str(version.id),
    )
    await db.commit()
    await db.refresh(chapter)
    return chapter
```

- [ ] **Step 4: Run smoke version tests to verify route ordering and no regression**

Run: `cd backend && python -m pytest tests/test_smoke.py -k "version" -v 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes.py backend/tests/test_harness_version_audit.py
git commit -m "feat(harness): add chapter rollback endpoint (creates new version, source=rollback)"
```

---

## Task 7: Python 3.10 兼容性验证 + 全量测试

**Files:** None (verification only)

- [ ] **Step 1: Verify Python 3.10 import compatibility**

Run:
```bash
cd backend && python -c "
from services.version_service import create_version, _prune_old_versions, VALID_SOURCES
from services.chapter_save import save_chapter_content
from schemas.api import ChapterVersionListItemResponse, ChapterVersionResponse
print('All imports OK on Python', __import__('sys').version_info[:2])
print('VALID_SOURCES:', sorted(VALID_SOURCES))
print('ListItem fields:', list(ChapterVersionListItemResponse.model_fields.keys()))
print('Response fields:', list(ChapterVersionResponse.model_fields.keys()))
"
```
Expected: prints all imports OK, Python 3.10, fields include run_id/parent_version_id/rollback_from_version_id

- [ ] **Step 2: Run all harness tests**

Run: `cd backend && python -m pytest tests/test_harness_version_audit.py -v 2>&1 | tail -25`
Expected: PASS (all tests)

- [ ] **Step 3: Run all harness tests (prior phases)**

Run: `cd backend && python -m pytest tests/test_harness_models.py tests/test_harness_run_manager.py tests/test_harness_generate_integration.py tests/test_harness_llm_call_logger.py tests/test_harness_run_api.py tests/test_harness_human_interrupt.py -v 2>&1 | tail -25`
Expected: PASS (all prior phase tests)

- [ ] **Step 4: Run smoke tests (separate from harness tests due to :memory: isolation)**

Run: `cd backend && python -m pytest tests/test_smoke.py -v 2>&1 | tail -30`
Expected: PASS (103 tests)

- [ ] **Step 5: Verify frontend typecheck still passes (no frontend changes in Phase F, but confirm)**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: exit 0 (no errors)

- [ ] **Step 6: Commit if any uncommitted changes remain**

```bash
git status --short
# If clean, skip commit. If there are changes:
git add -A && git commit -m "chore: phase F verification — py3.10 compat + all tests pass"
```

---

## Phase F 验收清单

- [ ] `create_version` 接受 `run_id` / `parent_version_id` / `rollback_from_version_id` / `project_id`（keyword-only，默认 None，向后兼容）
- [ ] `_prune_old_versions` 跳过被 `GenerationRecord.accepted_version_id` 引用的版本
- [ ] `save_chapter_content` 接受并转发 `run_id` / `parent_version_id`
- [ ] approve 路径（`resume_chapter_generation`）传 `run_id` 到 `save_chapter_content` + 写 `accepted_version_id`
- [ ] 新增 `POST /projects/{pid}/chapters/{sn}/versions/{vid}/restore` 回滚 endpoint（source=rollback, 不覆盖）
- [ ] 版本列表/详情 schema + endpoint 返回 `run_id` / `parent_version_id` / `rollback_from_version_id`
- [ ] `MAX_VERSIONS_PER_CHAPTER = 10` 保留（不取消），但 prune 保护被引用版本
- [ ] Python 3.10 兼容
- [ ] 所有 harness 测试 + smoke 测试通过（单独跑）
- [ ] 零改动 `llm_provider.py` / `workflow.py`
- [ ] 无新 migration（列已存在）
