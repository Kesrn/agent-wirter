# K-4: Outline Pacing 节奏标记 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pacing (节奏类型), tension_level (张力等级), and target_scene_count (目标场景数) fields to outlines, so each chapter has a clear pacing role and the chapter architect can use this to adjust its output.

**Architecture:** Three new columns on the existing `outlines` table. Frontend adds pacing select in ChapterConfig and Workspace sidebar. Context Builder injects pacing info into the `## 本章大纲` prompt section. No new tables, no new endpoints.

**Tech Stack:** SQLAlchemy ORM + Alembic migration (026_outline_pacing), Pydantic schemas, Vue 3 + TypeScript

---

### Task 1: Add pacing fields to Outline model

**Files:**
- Modify: `backend/models/outline.py`

- [ ] **Step 1: Add pacing, tension_level, target_scene_count to Outline model**

```python
"""大纲模型

每个章节对应一条大纲条目，用于规划章节走向。
K-4: pacing / tension_level / target_scene_count 节奏标记
"""

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class Outline(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "outlines"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    turning_point: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden_thread_ids: Mapped[list | None] = mapped_column("hidden_thread_ids", JSONValue(), nullable=True)
    story_arc_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    arc_position: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # ── K-4: 节奏标记 ──
    pacing: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    # SETUP / BUILDUP / REVERSAL / CLIMAX / AFTERMATH / TRANSITION / SLICE
    tension_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1-5, 张力等级
    target_scene_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 目标场景数
```

- [ ] **Step 2: Verify model loads correctly**

Run: `cd backend && python -c "from models.outline import Outline; print([c.name for c in Outline.__table__.columns])"`
Expected: Output includes `pacing`, `tension_level`, `target_scene_count`

- [ ] **Step 3: Commit**

```bash
git add backend/models/outline.py
git commit -m "feat(pacing): add pacing/tension_level/target_scene_count to Outline model"
```

---

### Task 2: Create migration 026_outline_pacing

**Files:**
- Create: `backend/alembic/versions/026_outline_pacing.py`

- [ ] **Step 1: Create migration file**

```python
"""outlines pacing 字段 — K-4 节奏标记

Revision ID: 026_outline_pacing
Revises: 025_hidden_thread_status
Create Date: 2026-07-07

阶段 K-4：outlines 增加 pacing / tension_level / target_scene_count。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "026_outline_pacing"
down_revision = "025_hidden_thread_status"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    if not _has_table("outlines"):
        return
    _add_column_if_missing("outlines", sa.Column("pacing", sa.String(30), nullable=True))
    _add_column_if_missing("outlines", sa.Column("tension_level", sa.Integer, nullable=True))
    _add_column_if_missing("outlines", sa.Column("target_scene_count", sa.Integer, nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_outlines_pacing ON outlines (pacing)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outlines_pacing")
    if _has_table("outlines"):
        for col in ["target_scene_count", "tension_level", "pacing"]:
            _drop_column_if_exists("outlines", col)
```

- [ ] **Step 2: Verify migration runs**

Run: `cd backend && python -m pytest tests/test_smoke.py::test_health -q`
Expected: PASS (migration runs during table setup)

- [ ] **Step 3: Commit**

```bash
git add backend/alembic/versions/026_outline_pacing.py
git commit -m "feat(pacing): migration 026 — outlines pacing/tension_level/target_scene_count"
```

---

### Task 3: Update Outline schemas

**Files:**
- Modify: `backend/schemas/api.py`

- [ ] **Step 1: Add pacing fields to OutlineCreate, OutlineUpdate, OutlineResponse**

Find `OutlineCreate` (around line 385) and add:

```python
class OutlineCreate(BaseModel):
    sequence_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)
    story_arc_id: str | None = None
    arc_position: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|TURNING_POINT|CLIMAX|AFTERMATH)$")
    pacing: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|REVERSAL|CLIMAX|AFTERMATH|TRANSITION|SLICE)$")
    tension_level: int | None = Field(default=None, ge=1, le=5)
    target_scene_count: int | None = Field(default=None, ge=1)
```

Update `OutlineUpdate`:

```python
class OutlineUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=10000)
    turning_point: str | None = Field(default=None, max_length=5000)
    hidden_thread_ids: list[str] | None = None
    story_arc_id: str | None = None
    arc_position: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|TURNING_POINT|CLIMAX|AFTERMATH)$")
    pacing: str | None = Field(default=None, pattern=r"^(SETUP|BUILDUP|REVERSAL|CLIMAX|AFTERMATH|TRANSITION|SLICE)$")
    tension_level: int | None = Field(default=None, ge=1, le=5)
    target_scene_count: int | None = Field(default=None, ge=1)
```

Update `OutlineResponse`:

```python
class OutlineResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    sequence_number: int
    title: str
    summary: str | None
    turning_point: str | None
    hidden_thread_ids: list[str] | None
    story_arc_id: uuid.UUID | None = None
    arc_position: str | None = None
    pacing: str | None = None
    tension_level: int | None = None
    target_scene_count: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Verify schemas load**

Run: `cd backend && python -c "from schemas.api import OutlineCreate, OutlineUpdate, OutlineResponse; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add backend/schemas/api.py
git commit -m "feat(pacing): add pacing/tension_level/target_scene_count to Outline schemas"
```

---

### Task 4: Context Builder — inject pacing into prompt

**Files:**
- Modify: `backend/services/chapter_context.py`

- [ ] **Step 1: Add pacing fields to OutlineInfo dataclass**

Find `OutlineInfo` (around line 38):

```python
@dataclass
class OutlineInfo:
    id: str
    sequence_number: int
    title: str
    summary: str
    turning_point: str
    story_arc_id: str | None = None
    arc_position: str | None = None
    pacing: str | None = None
    tension_level: int | None = None
    target_scene_count: int | None = None
```

- [ ] **Step 2: Update _load_outline to capture pacing fields**

Find `_load_outline` (around line 390):

```python
if outline:
    ctx.outline = OutlineInfo(
        id=str(outline.id),
        sequence_number=outline.sequence_number,
        title=outline.title,
        summary=outline.summary or "",
        turning_point=outline.turning_point or "",
        story_arc_id=str(outline.story_arc_id) if outline.story_arc_id else None,
        arc_position=outline.arc_position,
        pacing=outline.pacing,
        tension_level=outline.tension_level,
        target_scene_count=outline.target_scene_count,
    )
```

- [ ] **Step 3: Update format_chapter_context_for_prompt to show pacing**

Find the `## 本章大纲` formatting section (around line 1071):

```python
if context.outline:
    ol = context.outline
    ol_text = f"## 本章大纲\n第{ol.sequence_number}章 {ol.title}"
    if ol.summary:
        ol_text += f"\n概要：{ol.summary}"
    if ol.turning_point:
        ol_text += f"\n转折点：{ol.turning_point}"
    # K-4: 节奏标记
    pacing_parts = []
    if ol.pacing:
        pacing_parts.append(f"节奏：{ol.pacing}")
    if ol.tension_level:
        pacing_parts.append(f"张力等级：{ol.tension_level}/5")
    if ol.target_scene_count:
        pacing_parts.append(f"目标场景数：{ol.target_scene_count}")
    if pacing_parts:
        ol_text += "\n" + " | ".join(pacing_parts)
    parts.append(ol_text)
```

- [ ] **Step 4: Verify context builds with pacing**

Run: `cd backend && python -m pytest tests/test_chapter_context.py -q`
Expected: 24 passed

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_context.py
git commit -m "feat(pacing): inject pacing/tension_level/target_scene_count into prompt context"
```

---

### Task 5: Frontend types

**Files:**
- Modify: `frontend/src/api/types.ts`

- [ ] **Step 1: Add pacing fields to OutlineItem, ApiOutline, OutlineCreatePayload, OutlineUpdatePayload**

```typescript
// OutlineItem (around line 240)
export interface OutlineItem {
  id: string
  project_id: string
  chapter_num: number
  title: string
  summary: string
  turning_point: string | null
  hidden_thread_ids: string[]
  story_arc_id: string | null
  arc_position: string | null
  pacing: string | null
  tension_level: number | null
  target_scene_count: number | null
}

// ApiOutline (around line 425)
export interface ApiOutline {
  id: string
  project_id: string
  sequence_number: number
  title: string
  summary: string | null
  turning_point: string | null
  hidden_thread_ids: string[]
  story_arc_id: string | null
  arc_position: string | null
  pacing: string | null
  tension_level: number | null
  target_scene_count: number | null
  created_at: string
  updated_at: string
}

// OutlineCreatePayload (around line 644)
export interface OutlineCreatePayload {
  sequence_number: number
  title: string
  summary?: string
  turning_point?: string
  story_arc_id?: string | null
  arc_position?: string | null
  pacing?: string | null
  tension_level?: number | null
  target_scene_count?: number | null
}

// OutlineUpdatePayload (around line 653)
export interface OutlineUpdatePayload {
  sequence_number?: number
  title?: string
  summary?: string
  turning_point?: string
  story_arc_id?: string | null
  arc_position?: string | null
  pacing?: string | null
  tension_level?: number | null
  target_scene_count?: number | null
}
```

- [ ] **Step 2: Update store mapper**

In `frontend/src/stores/index.ts`, find `apiOutlineToOutlineItem` (around line 976):

```typescript
function apiOutlineToOutlineItem(ao: ApiOutline): OutlineItem {
  return {
    id: ao.id,
    project_id: ao.project_id,
    chapter_num: ao.sequence_number,
    title: ao.title,
    summary: ao.summary ?? '',
    turning_point: ao.turning_point ?? null,
    hidden_thread_ids: ao.hidden_thread_ids ?? [],
    story_arc_id: ao.story_arc_id ?? null,
    arc_position: ao.arc_position ?? null,
    pacing: ao.pacing ?? null,
    tension_level: ao.tension_level ?? null,
    target_scene_count: ao.target_scene_count ?? null,
  }
}
```

- [ ] **Step 3: Verify types**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/stores/index.ts
git commit -m "feat(pacing): add pacing types to frontend types and store mapper"
```

---

### Task 6: Frontend UI — ChapterConfig pacing select

**Files:**
- Modify: `frontend/src/components/ChapterConfig.vue`

- [ ] **Step 1: Add pacing refs and save logic**

Add refs after `lightLine`:

```typescript
const pacing = ref<string | null>(null)
const tensionLevel = ref<number | null>(null)
const targetSceneCount = ref<number | null>(null)

const pacingOptions = [
  { value: '', label: '（无）' },
  { value: 'SETUP', label: '铺垫 SETUP' },
  { value: 'BUILDUP', label: '升级 BUILDUP' },
  { value: 'REVERSAL', label: '反转 REVERSAL' },
  { value: 'CLIMAX', label: '高潮 CLIMAX' },
  { value: 'AFTERMATH', label: '余波 AFTERMATH' },
  { value: 'TRANSITION', label: '过渡 TRANSITION' },
  { value: 'SLICE', label: '日常 SLICE' },
]
```

Update watcher:

```typescript
watch(chapterOutline, (item) => {
  outlineTitle.value = item?.title ?? props.chapterTitle
  outlineSummary.value = item?.summary ?? ''
  lightLine.value = item?.turning_point ?? ''
  selectedArcId.value = item?.story_arc_id ?? null
  selectedArcPosition.value = item?.arc_position ?? null
  pacing.value = item?.pacing ?? null
  tensionLevel.value = item?.tension_level ?? null
  targetSceneCount.value = item?.target_scene_count ?? null
}, { immediate: true })
```

Update `saveChapterOutline` to pass pacing fields:

```typescript
async function saveChapterOutline() {
  // ... existing code ...
  if (chapterOutline.value) {
    await outlineStore.updateOutlineItem(props.projectId, chapterOutline.value.id, {
      title,
      summary: outlineSummary.value.trim(),
      turning_point: lightLine.value.trim(),
      story_arc_id: selectedArcId.value || null,
      arc_position: selectedArcPosition.value || null,
      pacing: pacing.value || null,
      tension_level: tensionLevel.value,
      target_scene_count: targetSceneCount.value,
    })
  } else {
    await outlineStore.createOutlineItem(props.projectId, {
      sequence_number: props.chapterNum,
      title,
      summary: outlineSummary.value.trim(),
      turning_point: lightLine.value.trim(),
      story_arc_id: selectedArcId.value || null,
      arc_position: selectedArcPosition.value || null,
      pacing: pacing.value || null,
      tension_level: tensionLevel.value,
      target_scene_count: targetSceneCount.value,
    })
  }
  // ...
}
```

- [ ] **Step 2: Add pacing select in template**

After the "明线推进" textarea and before "所属长线" select:

```html
<div class="form-row">
  <label>节奏类型</label>
  <select v-model="pacing" class="form-input">
    <option v-for="opt in pacingOptions" :key="opt.value" :value="opt.value || null">{{ opt.label }}</option>
  </select>
</div>
<div class="form-row" style="display: flex; gap: var(--sp-2);">
  <div style="flex: 1;">
    <label>张力等级 (1-5)</label>
    <input v-model.number="tensionLevel" type="number" min="1" max="5" class="form-input form-input-sm" />
  </div>
  <div style="flex: 1;">
    <label>目标场景数</label>
    <input v-model.number="targetSceneCount" type="number" min="1" class="form-input form-input-sm" />
  </div>
</div>
```

- [ ] **Step 3: Verify build**

Run: `cd frontend && npx vue-tsc --noEmit && npm run build`
Expected: No errors, build succeeds

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ChapterConfig.vue
git commit -m "feat(pacing): add pacing/tension_level/target_scene_count select in ChapterConfig"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run backend tests**

Run: `cd backend && python -m pytest tests/test_smoke.py tests/test_chapter_context.py -q`
Expected: 105 + 24 passed

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All passing

- [ ] **Step 3: Commit if any remaining changes**

```bash
git status
# commit any remaining changes
```
