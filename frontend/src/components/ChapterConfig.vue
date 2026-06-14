<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useCharacterEventStore, useCharacterStore, useWorldEntryStore, useOutlineStore, useHiddenThreadStore, useUiStore, friendlyError } from '../stores'
import type { CharacterEvent } from '../api/types'
import BaseSelect from './BaseSelect.vue'
const props = defineProps<{
  projectId: string
  projectMode: 'novel' | 'article'
  chapterNum: number
  chapterTitle: string
}>()

const emit = defineEmits<{
  close: []
}>()

const characterEventStore = useCharacterEventStore()
const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const outlineStore = useOutlineStore()
const hiddenThreadStore = useHiddenThreadStore()
const ui = useUiStore()

// ─── Chapter outline / lines ───
const chapterOutline = computed(() =>
  outlineStore.entriesForProject(props.projectId).find(o => o.chapter_num === props.chapterNum) ?? null
)

const outlineTitle = ref('')
const outlineSummary = ref('')
const lightLine = ref('')
const savingOutline = ref(false)

watch(chapterOutline, (item) => {
  outlineTitle.value = item?.title ?? props.chapterTitle
  outlineSummary.value = item?.summary ?? ''
  lightLine.value = item?.turning_point ?? ''
}, { immediate: true })

watch(() => props.chapterTitle, (title) => {
  if (!chapterOutline.value && !outlineTitle.value.trim()) {
    outlineTitle.value = title
  }
})

const chapterHiddenThreads = computed(() =>
  hiddenThreadStore.threadsForProject(props.projectId)
    .filter(thread => (thread.chapter_nums ?? []).includes(props.chapterNum))
)

const showDarkLineForm = ref(false)
const darkLineName = ref('')
const darkLineDescription = ref('')
const savingDarkLine = ref(false)

async function saveChapterOutline() {
  const title = outlineTitle.value.trim() || props.chapterTitle || `第${props.chapterNum}章`
  savingOutline.value = true
  try {
    if (chapterOutline.value) {
      await outlineStore.updateOutlineItem(props.projectId, chapterOutline.value.id, {
        title,
        summary: outlineSummary.value.trim(),
        turning_point: lightLine.value.trim(),
      })
    } else {
      await outlineStore.createOutlineItem(props.projectId, {
        sequence_number: props.chapterNum,
        title,
        summary: outlineSummary.value.trim(),
        turning_point: lightLine.value.trim(),
      })
    }
    ui.showToast('章节大纲已保存', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存章节大纲失败'), 'error')
  } finally {
    savingOutline.value = false
  }
}

function openDarkLineForm() {
  darkLineName.value = ''
  darkLineDescription.value = ''
  showDarkLineForm.value = true
}

async function saveDarkLine() {
  const name = darkLineName.value.trim()
  if (!name) {
    ui.showToast('暗线名称不能为空', 'error')
    return
  }
  savingDarkLine.value = true
  try {
    await hiddenThreadStore.createHiddenThreadItem(props.projectId, {
      name,
      description: darkLineDescription.value.trim(),
      chapter_nums: [props.chapterNum],
    })
    showDarkLineForm.value = false
    ui.showToast('暗线已加入本章', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '添加暗线失败'), 'error')
  } finally {
    savingDarkLine.value = false
  }
}

async function removeDarkLineFromChapter(threadId: string) {
  const thread = hiddenThreadStore.threadsForProject(props.projectId).find(item => item.id === threadId)
  if (!thread) return
  try {
    await hiddenThreadStore.updateHiddenThreadItem(props.projectId, thread.id, {
      chapter_nums: (thread.chapter_nums ?? []).filter(num => num !== props.chapterNum),
    })
    ui.showToast('已从本章移除暗线', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '移除暗线失败'), 'error')
  }
}

// ─── Chapter events ───
const chapterEvents = computed(() =>
  characterEventStore.eventsForProject(props.projectId)
    .filter(e => e.chapter_sequence_number === props.chapterNum)
)

// ─── Characters in this chapter ───
const chapterCharacterIds = computed(() =>
  new Set(chapterEvents.value.map(e => e.character_id))
)

const chapterCharacters = computed(() =>
  characterStore.charactersForProject(props.projectId)
    .filter(c => chapterCharacterIds.value.has(c.id))
)

// ─── World entries relevant ───
const allWorldEntries = computed(() => worldEntryStore.entriesForProject(props.projectId))
const relevantWorldEntries = computed(() =>
  allWorldEntries.value.filter(w => w.scope_type === 'global')
)
const chapterSpecificWorldEntries = computed(() =>
  allWorldEntries.value.filter(w => w.scope_type === 'chapter')
)

// ─── Character event editing ───
const showEventForm = ref(false)
const editingEventId = ref<string | null>(null)
const eventForm = ref({
  character_id: '',
  appearance_type: 'appeared' as 'appeared' | 'mentioned' | 'absent',
  event_summary: '',
  actions: '',
  state_change: '',
  location: '',
  emotion: '',
  importance: 3,
})
const savingEvent = ref(false)

function openNewEvent() {
  editingEventId.value = null
  eventForm.value = {
    character_id: '',
    appearance_type: 'appeared',
    event_summary: '',
    actions: '',
    state_change: '',
    location: '',
    emotion: '',
    importance: 3,
  }
  showEventForm.value = true
}

function editEvent(event: CharacterEvent) {
  editingEventId.value = event.id
  eventForm.value = {
    character_id: event.character_id,
    appearance_type: event.appearance_type as 'appeared' | 'mentioned' | 'absent',
    event_summary: event.event_summary ?? '',
    actions: (event.actions ?? []).join(', '),
    state_change: event.state_change ?? '',
    location: event.location ?? '',
    emotion: event.emotion ?? '',
    importance: event.importance,
  }
  showEventForm.value = true
}

function cancelEventForm() {
  showEventForm.value = false
  editingEventId.value = null
}

async function saveEvent() {
  if (!eventForm.value.character_id) {
    ui.showToast('请选择角色', 'error')
    return
  }
  savingEvent.value = true
  try {
    const payload = {
      appearance_type: eventForm.value.appearance_type,
      event_summary: eventForm.value.event_summary.trim() || null,
      actions: eventForm.value.actions.trim()
        ? eventForm.value.actions.split(',').map(a => a.trim()).filter(Boolean)
        : null,
      state_change: eventForm.value.state_change.trim() || null,
      location: eventForm.value.location.trim() || null,
      emotion: eventForm.value.emotion.trim() || null,
      importance: eventForm.value.importance,
    }
    await characterEventStore.upsertCharacterEvent(
      props.projectId,
      eventForm.value.character_id,
      props.chapterNum,
      payload,
    )
    cancelEventForm()
    ui.showToast('事件已保存', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存事件失败'), 'error')
  } finally {
    savingEvent.value = false
  }
}

async function deleteEvent(event: CharacterEvent) {
  try {
    await characterEventStore.removeCharacterEvent(
      props.projectId,
      event.character_id,
      props.chapterNum,
    )
    ui.showToast('事件已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除事件失败'), 'error')
  }
}

// ─── Labels ───
const scopeLabel = (scope: string) =>
  ({ core: '主角团', recurring: '常驻角色', chapter: '章节角色', cameo: '客串角色' } as Record<string, string>)[scope] ?? scope

const appearanceLabel = (type: string) =>
  ({ appeared: '出场', mentioned: '提及', absent: '未出场' } as Record<string, string>)[type] ?? type

const roleLabel = (role: string) =>
  ({ protagonist: '主角', antagonist: '反派', supporting: '配角', minor: '路人' } as Record<string, string>)[role] ?? role

// Available characters for the event form dropdown
const availableCharacters = computed(() => characterStore.charactersForProject(props.projectId))

onMounted(async () => {
  // Ensure events are loaded
  if (!characterEventStore.events.length) {
    try {
      await characterEventStore.loadCharacterEvents(props.projectId)
    } catch {
      // silently fail
    }
  }
  if (!outlineStore.outlines.length) {
    outlineStore.loadOutlines(props.projectId).catch(() => {})
  }
  if (!hiddenThreadStore.hiddenThreads.length) {
    hiddenThreadStore.loadHiddenThreads(props.projectId).catch(() => {})
  }
})
</script>

<template>
  <div class="chapter-config">
    <header class="config-header">
      <h2 class="config-title">第{{ chapterNum }}章配置 — {{ chapterTitle }}</h2>
      <button class="btn-close" @click="emit('close')">← 返回</button>
    </header>

    <div class="config-body">
      <!-- 章节大纲 / 明暗线 -->
      <section class="config-section chapter-outline-section">
        <div class="section-head">
          <h3>章节大纲</h3>
          <button class="btn-add-inline" :disabled="savingOutline" @click="saveChapterOutline">
            {{ savingOutline ? '保存中...' : '保存' }}
          </button>
        </div>
        <div class="form-row">
          <label>大纲标题</label>
          <input v-model="outlineTitle" type="text" class="form-input" placeholder="本章大纲标题" />
        </div>
        <div class="form-row">
          <label>本章摘要</label>
          <textarea v-model="outlineSummary" class="form-textarea" rows="4" placeholder="这一章发生了什么，解决什么问题，推到哪里"></textarea>
        </div>
        <div class="form-row">
          <label>明线推进</label>
          <textarea v-model="lightLine" class="form-textarea" rows="2" placeholder="读者能直接看到的剧情目标、冲突或转折"></textarea>
        </div>
      </section>

      <!-- 本章角色 -->
      <section class="config-section">
        <h3>本章角色</h3>
        <div v-if="chapterCharacters.length" class="char-list">
          <div v-for="char in chapterCharacters" :key="char.id" class="char-item">
            <span class="char-name">{{ char.name }}</span>
            <span class="badge badge-role" :class="char.role_type">{{ roleLabel(char.role_type) }}</span>
            <span class="badge badge-scope" :class="char.scope_type">{{ scopeLabel(char.scope_type) }}</span>
            <span class="char-faction" v-if="char.faction">{{ char.faction }}</span>
          </div>
        </div>
        <div v-else class="empty-hint">暂未记录本章角色</div>
      </section>

      <!-- 角色事件 -->
      <section class="config-section">
        <div class="section-head">
          <h3>本章事件</h3>
          <button class="btn-add-inline" @click="openNewEvent">+ 添加事件</button>
        </div>

        <!-- Event form -->
        <div v-if="showEventForm" class="event-form">
          <div class="form-row">
            <label>角色 <span class="required">*</span></label>
            <select v-model="eventForm.character_id" class="form-input">
              <option value="" disabled>选择角色</option>
              <option v-for="char in availableCharacters" :key="char.id" :value="char.id">
                {{ char.name }}
              </option>
            </select>
          </div>
          <div class="form-row">
            <label>出现类型</label>
            <BaseSelect
              v-model="eventForm.appearance_type"
              :options="[
                { value: 'appeared', label: '出场' },
                { value: 'mentioned', label: '提及' },
                { value: 'absent', label: '未出场' },
              ]"
            />
          </div>
          <div class="form-row">
            <label>事件摘要</label>
            <textarea v-model="eventForm.event_summary" class="form-textarea" rows="2" placeholder="角色在本章做了什么"></textarea>
          </div>
          <div class="form-row">
            <label>行动（逗号分隔）</label>
            <input v-model="eventForm.actions" type="text" class="form-input" placeholder="行动1, 行动2" />
          </div>
          <div class="form-row">
            <label>状态变化</label>
            <input v-model="eventForm.state_change" type="text" class="form-input" placeholder="角色状态变化" />
          </div>
          <div class="form-row form-row-horizontal">
            <div class="form-field">
              <label>地点</label>
              <input v-model="eventForm.location" type="text" class="form-input" placeholder="地点" />
            </div>
            <div class="form-field">
              <label>情绪</label>
              <input v-model="eventForm.emotion" type="text" class="form-input" placeholder="情绪" />
            </div>
            <div class="form-field form-field-sm">
              <label>重要度 (1-5)</label>
              <input v-model.number="eventForm.importance" type="number" min="1" max="5" class="form-input" />
            </div>
          </div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="savingEvent" @click="saveEvent">
              {{ savingEvent ? '保存中...' : '保存' }}
            </button>
            <button class="btn-cancel" @click="cancelEventForm">取消</button>
          </div>
        </div>

        <!-- Event list -->
        <div v-if="chapterEvents.length" class="event-list">
          <div v-for="event in chapterEvents" :key="event.id" class="event-card">
            <div class="event-card-head">
              <span class="event-char-name">{{ characterStore.characters.find(c => c.id === event.character_id)?.name ?? '未知角色' }}</span>
              <span class="badge badge-appearance" :class="event.appearance_type">{{ appearanceLabel(event.appearance_type) }}</span>
              <span class="event-importance">重要度 {{ event.importance }}/5</span>
              <span class="event-actions-btn">
                <button class="icon-btn" title="编辑" @click="editEvent(event)">&#9998;</button>
                <button class="icon-btn icon-btn-danger" title="删除" @click="deleteEvent(event)">&#10005;</button>
              </span>
            </div>
            <p v-if="event.event_summary" class="event-summary">{{ event.event_summary }}</p>
            <div v-if="event.actions?.length" class="event-actions">
              <span v-for="(action, i) in event.actions" :key="i" class="action-tag">{{ action }}</span>
            </div>
            <div v-if="event.state_change" class="event-state">状态: {{ event.state_change }}</div>
            <div v-if="event.location || event.emotion" class="event-meta">
              <span v-if="event.location">📍 {{ event.location }}</span>
              <span v-if="event.emotion">{{ event.emotion }}</span>
            </div>
          </div>
        </div>
        <div v-else-if="!showEventForm" class="empty-hint">暂无角色事件，点击上方「添加事件」或通过 AI 自动提炼</div>
      </section>

      <!-- 暗线 -->
      <section class="config-section">
        <div class="section-head">
          <h3>暗线</h3>
          <button class="btn-add-inline" @click="openDarkLineForm">+ 添加暗线</button>
        </div>
        <div v-if="showDarkLineForm" class="event-form">
          <div class="form-row">
            <label>暗线名称 <span class="required">*</span></label>
            <input v-model="darkLineName" type="text" class="form-input" placeholder="例如：幕后势力试探主角" />
          </div>
          <div class="form-row">
            <label>线索说明</label>
            <textarea v-model="darkLineDescription" class="form-textarea" rows="2" placeholder="本章埋下或推进了什么隐藏线索"></textarea>
          </div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="savingDarkLine" @click="saveDarkLine">
              {{ savingDarkLine ? '保存中...' : '保存' }}
            </button>
            <button class="btn-cancel" @click="showDarkLineForm = false">取消</button>
          </div>
        </div>
        <div v-if="chapterHiddenThreads.length" class="dark-line-list">
          <article v-for="thread in chapterHiddenThreads" :key="thread.id" class="dark-line-card">
            <div class="dark-line-head">
              <span class="dark-line-name">{{ thread.name }}</span>
              <button class="icon-btn icon-btn-danger" title="从本章移除" @click="removeDarkLineFromChapter(thread.id)">&#10005;</button>
            </div>
            <p v-if="thread.description" class="dark-line-desc">{{ thread.description }}</p>
          </article>
        </div>
        <div v-else-if="!showDarkLineForm" class="empty-hint">本章暂无暗线</div>
      </section>

      <!-- 相关设定 -->
      <section class="config-section">
        <h3>相关设定</h3>
        <div class="world-subsections">
          <div v-if="relevantWorldEntries.length" class="world-subsection">
            <div class="world-sub-label">全局设定</div>
            <div class="world-tag-list">
              <span v-for="entry in relevantWorldEntries" :key="entry.id" class="world-tag">
                {{ entry.title }}
                <span class="world-tag-category" v-if="entry.category">{{ entry.category }}</span>
              </span>
            </div>
          </div>
          <div v-if="chapterSpecificWorldEntries.length" class="world-subsection">
            <div class="world-sub-label">章节专属设定</div>
            <div class="world-tag-list">
              <span v-for="entry in chapterSpecificWorldEntries" :key="entry.id" class="world-tag world-tag-chapter">
                {{ entry.title }}
                <span class="world-tag-category" v-if="entry.category">{{ entry.category }}</span>
              </span>
            </div>
          </div>
          <div v-if="!relevantWorldEntries.length && !chapterSpecificWorldEntries.length" class="empty-hint">暂无相关设定</div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.chapter-config {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.config-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  background: var(--bg-panel);
}
.config-title {
  font-size: var(--text-lg);
  font-weight: 700;
  margin: 0;
  color: var(--text);
}
.btn-close {
  background: none;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px 14px;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: var(--text-sm);
}
.btn-close:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.config-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-4) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
}
.config-section {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: var(--sp-4);
}
.config-section h3 {
  font-size: var(--text-base);
  font-weight: 600;
  margin: 0 0 var(--sp-3);
  color: var(--text);
}
.chapter-outline-section {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}
.section-head h3 {
  margin: 0;
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-bottom: var(--sp-3);
}
.form-row label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
}
.form-input,
.form-textarea {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-input);
  color: var(--text);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.form-input:focus,
.form-textarea:focus {
  outline: none;
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}
.form-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.btn-submit,
.btn-cancel,
.btn-add-inline {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 5px 12px;
  font-size: var(--text-xs);
  cursor: pointer;
}
.btn-submit {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.btn-cancel,
.btn-add-inline {
  background: var(--bg-panel);
  color: var(--text-secondary);
}
.btn-add-inline {
  color: var(--accent);
}
.btn-submit:disabled,
.btn-add-inline:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.icon-btn {
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: 6px;
  padding: 2px 6px;
}
.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.icon-btn-danger:hover {
  background: var(--status-error-bg);
  color: var(--status-error);
}

/* Characters */
.char-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.char-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--bg-hover);
  border-radius: 8px;
  font-size: var(--text-sm);
}
.char-name {
  font-weight: 600;
  color: var(--text);
}
.char-faction {
  font-size: 11px;
  color: var(--text-tertiary);
}

/* Event form */
.event-form {
  padding: var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: 10px;
  background: var(--bg-hover);
  margin-bottom: var(--sp-3);
}
.form-field {
  flex: 1;
}
.form-field-sm {
  max-width: 100px;
}
.form-row-horizontal {
  display: flex;
  gap: var(--sp-3);
}
.form-row-horizontal .form-field {
  min-width: 0;
}

/* Event list */
.event-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.event-card {
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  font-size: var(--text-sm);
}
.event-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.event-char-name {
  font-weight: 600;
  color: var(--text);
}
.event-importance {
  font-size: 10px;
  color: var(--text-tertiary);
}
.event-actions-btn {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.event-summary {
  margin: 6px 0 0;
  color: var(--text-secondary);
  line-height: 1.5;
}
.event-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.action-tag {
  font-size: 10px;
  padding: 1px 6px;
  background: var(--accent-subtle);
  color: var(--accent);
  border-radius: 4px;
}
.event-state {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 4px;
}
.event-meta {
  display: flex;
  gap: var(--sp-3);
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-tertiary);
}

/* Dark lines */
.dark-line-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.dark-line-card {
  padding: var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  border-radius: 8px;
  background: var(--bg);
}
.dark-line-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.dark-line-name {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text);
}
.dark-line-desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.5;
}

/* World entries */
.world-subsections {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.world-subsection {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.world-sub-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.world-tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.world-tag {
  padding: 3px 10px;
  background: var(--bg-hover);
  border-radius: 6px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.world-tag-chapter {
  border: 1px dashed var(--status-reviewing);
}
.world-tag-category {
  font-size: 9px;
  color: var(--text-tertiary);
}

/* Badges */
.badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 6px;
  white-space: nowrap;
}
.badge-role.protagonist {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.badge-role.antagonist {
  background: var(--status-error-bg);
  color: var(--status-error);
}
.badge-role.supporting {
  background: var(--accent-subtle);
  color: var(--accent);
}
.badge-role.minor {
  background: var(--bg-hover);
  color: var(--text-tertiary);
}
.badge-scope.core {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.badge-scope.recurring {
  background: var(--accent-subtle);
  color: var(--accent);
}
.badge-scope.chapter {
  background: var(--status-reviewing-bg);
  color: var(--status-reviewing);
}
.badge-scope.cameo {
  background: var(--bg-hover);
  color: var(--text-tertiary);
}
.badge-appearance.appeared {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.badge-appearance.mentioned {
  background: var(--status-reviewing-bg);
  color: var(--status-reviewing);
}
.badge-appearance.absent {
  background: var(--bg-hover);
  color: var(--text-tertiary);
}
</style>
