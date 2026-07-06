# K-5: 角色弧线聚合视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only aggregation endpoint that shows a character's arc across chapters, combining CharacterEvent data, confirmed writing memory staging items, and story-recorder output into a chronological timeline view.

**Architecture:** One new API endpoint `GET /api/projects/{project_id}/characters/{character_id}/arc`. No new tables, no new models. Pure read-time aggregation of existing data. Frontend adds a simple "角色弧线" panel in the character details area.

**Tech Stack:** FastAPI + SQLAlchemy async queries, Pydantic response model, Vue 3 + TypeScript

---

### Task 1: Character arc API — backend response schema

**Files:**
- Modify: `backend/schemas/api.py`

- [ ] **Step 1: Add CharacterArcItem and CharacterArcResponse schemas**

Add after CharacterEventResponse (around line 381):

```python
# --- 角色弧线 (K-5) ---

class CharacterArcItem(BaseModel):
    """角色弧线中的单个事件"""
    chapter_sequence_number: int | None
    source_type: str  # CharacterEvent / WritingMemory / StoryRecorder
    title: str
    summary: str = ""
    state_change: str | None = None
    emotion: str | None = None
    importance: int = 3
    confidence: str = "confirmed"  # confirmed / ai_extracted


class CharacterArcResponse(BaseModel):
    """角色弧线聚合视图"""
    character_id: uuid.UUID
    character_name: str
    role_type: str
    items: list[CharacterArcItem]
    chapter_range: str = ""  # "第1-10章"
```

- [ ] **Step 2: Run python import check**

Run: `cd backend && python -c "from schemas.api import CharacterArcItem, CharacterArcResponse; print('OK')"`
Expected: OK

---

### Task 2: Character arc API — endpoint

**Files:**
- Modify: `backend/api/routes.py`

- [ ] **Step 1: Add import for new schemas**

Find the schema imports (around line 59) and add:

```python
from schemas.api import (
    # ... existing imports ...
    CharacterArcResponse,
)
```

- [ ] **Step 2: Add the GET endpoint**

Add after the character-events routes (around line 2030):

```python
# ==================== 角色弧线 (K-5) ====================

@router.get("/projects/{project_id}/characters/{character_id}/arc", response_model=CharacterArcResponse)
async def get_character_arc(
    project_id: str,
    character_id: str,
    to_chapter: int | None = Query(default=None, description="只返回到第N章为止的数据"),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """获取角色弧线聚合视图。

    聚合来源：
    - CharacterEvent（手动录入 + AI 抽取确认）
    - WritingMemoryStaging（已确认的 CHARACTER/EVENT 类型）
    - 按章节序号排序，to_chapter 可过滤未来章节
    """
    from schemas.api import CharacterArcItem

    uid = _to_uuid(project_id)
    cid = _to_uuid(character_id)
    await _verify_project_owner(uid, user.id, db)

    # 查角色基本信息
    character = await _get_project_character(uid, cid, db)

    items: list[CharacterArcItem] = []

    # 1. 聚合 CharacterEvent
    event_result = await db.execute(
        select(CharacterEvent).where(
            CharacterEvent.project_id == uid,
            CharacterEvent.character_id == cid,
            CharacterEvent.appeared == True,  # noqa: E712
        ).order_by(CharacterEvent.chapter_sequence_number.asc())
    )
    for evt in event_result.scalars().all():
        if to_chapter and evt.chapter_sequence_number > to_chapter:
            continue
        items.append(CharacterArcItem(
            chapter_sequence_number=evt.chapter_sequence_number,
            source_type="CharacterEvent",
            title=evt.event_summary or f"第{evt.chapter_sequence_number}章出场",
            summary=evt.event_summary or "",
            state_change=evt.state_change,
            emotion=evt.emotion,
            importance=evt.importance or 3,
            confidence="confirmed",
        ))

    # 2. 聚合 WritingMemoryStaging (已确认的 CHARACTER/EVENT 类型)
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus

    staging_result = await db.execute(
        select(WritingMemoryStaging).where(
            WritingMemoryStaging.project_id == uid,
            WritingMemoryStaging.memory_type.in_(["CHARACTER", "EVENT"]),
            WritingMemoryStaging.status == MemoryStagingStatus.CONFIRMED.value,
            # 匹配角色名：payload.character_name 或 title 包含 character.name
        ).order_by(WritingMemoryStaging.chapter_sequence_number.asc().nulls_last())
    )
    for sm in staging_result.scalars().all():
        if to_chapter and sm.chapter_sequence_number and sm.chapter_sequence_number > to_chapter:
            continue
        payload = sm.payload or {}
        char_name = payload.get("character_name") or payload.get("name") or ""
        if char_name.lower() != character.name.lower():
            continue
        items.append(CharacterArcItem(
            chapter_sequence_number=sm.chapter_sequence_number,
            source_type="WritingMemory",
            title=sm.title,
            summary=payload.get("description") or sm.title,
            state_change=payload.get("state_change"),
            importance=3,
            confidence="ai_extracted",
        ))

    # 按章节排序
    items.sort(key=lambda x: x.chapter_sequence_number or 0)

    # 计算章节范围
    chapters = [i.chapter_sequence_number for i in items if i.chapter_sequence_number]
    chapter_range = ""
    if chapters:
        chapter_range = f"第{min(chapters)}-{max(chapters)}章"

    return CharacterArcResponse(
        character_id=cid,
        character_name=character.name,
        role_type=character.role_type or "supporting",
        items=items,
        chapter_range=chapter_range,
    )
```

- [ ] **Step 3: Verify endpoint loads**

Run: `cd backend && python -c "from api.routes import get_character_arc; print('OK')"`
Expected: OK

---

### Task 3: Frontend types

**Files:**
- Modify: `frontend/src/api/types.ts`

- [ ] **Step 1: Add CharacterArcItem and CharacterArcResponse types**

Add after existing types:

```typescript
// ─── K-5: Character Arc ───

export interface ApiCharacterArcItem {
  chapter_sequence_number: number | null
  source_type: string
  title: string
  summary: string
  state_change: string | null
  emotion: string | null
  importance: number
  confidence: string
}

export interface ApiCharacterArcResponse {
  character_id: string
  character_name: string
  role_type: string
  items: ApiCharacterArcItem[]
  chapter_range: string
}
```

---

### Task 4: Frontend API client

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add getCharacterArc method**

After the existing character methods (around line 336):

```typescript
// ─── Character Arc (K-5) ───
getCharacterArc: (projectId: string, characterId: string, toChapter?: number) =>
  request<ApiCharacterArcResponse>(`/projects/${projectId}/characters/${characterId}/arc${toChapter ? `?to_chapter=${toChapter}` : ''}`),
```

- [ ] **Step 2: Add import**

```typescript
import {
  // ... existing imports ...
  type ApiCharacterArcResponse,
} from '../api/types'
```

- [ ] **Step 3: Verify types**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: No errors

---

### Task 5: Frontend CharacterArcPanel component

**Files:**
- Create: `frontend/src/components/CharacterArcPanel.vue`

- [ ] **Step 1: Create the component**

```vue
<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api/client'
import type { ApiCharacterArcResponse, ApiCharacterArcItem } from '../api/types'
import { friendlyError } from '../stores'

const props = defineProps<{
  projectId: string
  characterId: string
  characterName: string
}>()

const loading = ref(false)
const arc = ref<ApiCharacterArcResponse | null>(null)
const error = ref('')

async function loadArc() {
  loading.value = true
  error.value = ''
  try {
    arc.value = await api.getCharacterArc(props.projectId, props.characterId)
  } catch (e: unknown) {
    error.value = friendlyError(e, '加载角色弧线失败')
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => {
  if (props.characterId) loadArc()
}, { immediate: true })

function sourceLabel(item: ApiCharacterArcItem): string {
  const map: Record<string, string> = {
    CharacterEvent: '章节事件',
    WritingMemory: 'AI提取',
  }
  return map[item.source_type] ?? item.source_type
}
</script>

<template>
  <div class="character-arc-panel" v-if="arc || loading">
    <div v-if="loading" class="arc-loading">加载中...</div>
    <div v-else-if="error" class="arc-error">{{ error }}</div>
    <template v-else-if="arc">
      <div class="arc-header">
        <h4>{{ characterName }} — 角色弧线</h4>
        <span class="arc-range" v-if="arc.chapter_range">{{ arc.chapter_range }}</span>
      </div>
      <div v-if="!arc.items.length" class="arc-empty">暂无弧线数据</div>
      <div v-else class="arc-timeline">
        <div v-for="item in arc.items" :key="`${item.chapter_sequence_number}-${item.source_type}`" class="arc-item">
          <div class="arc-item-header">
            <span class="arc-chapter" v-if="item.chapter_sequence_number">第{{ item.chapter_sequence_number }}章</span>
            <span class="arc-source">{{ sourceLabel(item) }}</span>
            <span v-if="item.confidence === 'ai_extracted'" class="arc-confidence">AI</span>
          </div>
          <div class="arc-item-title">{{ item.title }}</div>
          <div v-if="item.summary && item.summary !== item.title" class="arc-item-summary">{{ item.summary }}</div>
          <div v-if="item.state_change" class="arc-item-change">变化：{{ item.state_change }}</div>
          <div v-if="item.emotion" class="arc-item-emotion">情绪：{{ item.emotion }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.character-arc-panel {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: var(--sp-4);
  margin-top: var(--sp-3);
}
.arc-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.arc-header h4 {
  margin: 0;
  font-size: 14px;
}
.arc-range {
  font-size: 12px;
  color: var(--color-text-muted);
}
.arc-empty, .arc-loading, .arc-error {
  font-size: 13px;
  color: var(--color-text-muted);
  padding: var(--sp-2) 0;
}
.arc-error { color: var(--color-danger); }
.arc-timeline {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.arc-item {
  border-left: 2px solid var(--color-primary);
  padding-left: var(--sp-3);
}
.arc-item-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}
.arc-chapter {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary);
}
.arc-source {
  font-size: 11px;
  color: var(--color-text-muted);
}
.arc-confidence {
  font-size: 10px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  padding: 0 4px;
  border-radius: 3px;
}
.arc-item-title {
  font-size: 13px;
  font-weight: 500;
}
.arc-item-summary {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-top: 2px;
}
.arc-item-change {
  font-size: 12px;
  color: #3b82f6;
  margin-top: 2px;
}
.arc-item-emotion {
  font-size: 12px;
  color: var(--color-text-muted);
  margin-top: 2px;
}
</style>
```

- [ ] **Step 2: Verify component compiles**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: No errors

---

### Task 6: Integrate CharacterArcPanel into ChapterConfig

**Files:**
- Modify: `frontend/src/components/ChapterConfig.vue`

- [ ] **Step 1: Import and render CharacterArcPanel**

Add import at top:

```typescript
import CharacterArcPanel from './CharacterArcPanel.vue'
```

Add in template after the character events section (before the closing `</div>` of `config-body`):

```html
<!-- K-5: 角色弧线视图 -->
<CharacterArcPanel
  v-if="selectedCharacterForArc"
  :project-id="projectId"
  :character-id="selectedCharacterForArc.id"
  :character-name="selectedCharacterForArc.name"
/>
```

Add state:

```typescript
const selectedCharacterForArc = ref<{ id: string; name: string } | null>(null)
```

Add a click handler on character names in the event list to toggle the arc panel.

- [ ] **Step 3: Verify build**

Run: `cd frontend && npx vue-tsc --noEmit && npm run build`
Expected: No errors, build succeeds

---

### Task 7: Final tests and verification

- [ ] **Step 1: Run backend smoke tests**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 105 passed

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All passing

- [ ] **Step 3: Commit all remaining changes**
