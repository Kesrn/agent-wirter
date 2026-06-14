<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useCharacterStore, useCharacterEventStore, useOutlineStore, useProjectStore, useUiStore, friendlyError } from '../stores'
import type { Character, CharacterEvent } from '../api/types'

const props = defineProps<{
  projectId: string
  projectMode: 'novel' | 'article'
}>()

const emit = defineEmits<{
  close: []
}>()

const characterStore = useCharacterStore()
const characterEventStore = useCharacterEventStore()
const outlineStore = useOutlineStore()
const projectStore = useProjectStore()
const ui = useUiStore()

const project = computed(() => projectStore.projects.find(p => p.id === props.projectId))
const overviewOutline = ref(project.value?.overall_outline ?? '')
const savingOverview = ref(false)
const loadingEvents = ref(false)

// ─── Computed ───

const characters = computed(() => {
  const order = { core: 0, recurring: 1, chapter: 2, cameo: 3 }
  return [...characterStore.charactersForProject(props.projectId)]
    .sort((a, b) => order[a.scope_type] - order[b.scope_type] || a.name.localeCompare(b.name))
})

const outline = computed(() => outlineStore.entriesForProject(props.projectId))

const allCharacterEvents = computed(() =>
  characterEventStore.eventsForProject(props.projectId)
)

const eventsByChapter = computed(() => {
  const map = new Map<number, CharacterEvent[]>()
  for (const event of allCharacterEvents.value) {
    const seq = event.chapter_sequence_number
    if (!map.has(seq)) map.set(seq, [])
    map.get(seq)!.push(event)
  }
  return new Map([...map.entries()].sort((a, b) => a[0] - b[0]))
})

const charactersByScope = computed(() => {
  const map: Record<string, Character[]> = { core: [], recurring: [], chapter: [], cameo: [] }
  for (const char of characters.value) {
    if (map[char.scope_type]) map[char.scope_type].push(char)
  }
  return map
})

const scopeLabel = (scope: string) =>
  ({ core: '主角团', recurring: '常驻角色', chapter: '章节角色', cameo: '客串角色' } as Record<string, string>)[scope] ?? scope

const roleLabel = (role: string) =>
  ({ protagonist: '主角', antagonist: '反派', supporting: '配角', minor: '路人' } as Record<string, string>)[role] ?? role

const appearanceLabel = (type: string) =>
  ({ appeared: '出场', mentioned: '提及', absent: '未出场' } as Record<string, string>)[type] ?? type

const roleOptions = [
  { value: 'protagonist', label: '主角' },
  { value: 'antagonist', label: '反派' },
  { value: 'supporting', label: '配角' },
  { value: 'minor', label: '路人' },
] as const

const scopeOptions = [
  { value: 'core', label: '主角团' },
  { value: 'recurring', label: '常驻角色' },
  { value: 'chapter', label: '章节角色' },
  { value: 'cameo', label: '客串角色' },
] as const

const showCharacterForm = ref(false)
const editingCharacterId = ref<string | null>(null)
const savingCharacter = ref(false)
const mergingCharacterId = ref<string | null>(null)
const mergeTargetCharacterId = ref('')
const savingMerge = ref(false)
const characterForm = ref<{
  name: string
  role_type: 'protagonist' | 'antagonist' | 'supporting' | 'minor'
  scope_type: 'core' | 'recurring' | 'chapter' | 'cameo'
  faction: string
  profile: string
}>({
  name: '',
  role_type: 'supporting',
  scope_type: 'recurring',
  faction: '',
  profile: '',
})

// ─── Overall outline ───

async function saveOverview() {
  savingOverview.value = true
  try {
    await projectStore.updateProjectRemote(props.projectId, {
      overall_outline: overviewOutline.value.trim() || null,
    })
    ui.showToast('整体大纲已保存', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存失败'), 'error')
  } finally {
    savingOverview.value = false
  }
}

function openNewCharacterForm() {
  editingCharacterId.value = null
  characterForm.value = {
    name: '',
    role_type: 'supporting',
    scope_type: 'recurring',
    faction: '',
    profile: '',
  }
  showCharacterForm.value = true
}

function editCharacter(char: Character) {
  editingCharacterId.value = char.id
  characterForm.value = {
    name: char.name,
    role_type: char.role_type,
    scope_type: char.scope_type,
    faction: char.faction ?? '',
    profile: char.profile ?? '',
  }
  showCharacterForm.value = true
}

function cancelCharacterForm() {
  showCharacterForm.value = false
  editingCharacterId.value = null
}

async function saveCharacter() {
  if (!characterForm.value.name.trim()) {
    ui.showToast('角色名称不能为空', 'error')
    return
  }
  savingCharacter.value = true
  try {
    const payload = {
      name: characterForm.value.name.trim(),
      role_type: characterForm.value.role_type,
      scope_type: characterForm.value.scope_type,
      faction: characterForm.value.faction.trim(),
      profile: characterForm.value.profile.trim(),
    }
    if (editingCharacterId.value) {
      await characterStore.updateCharacter(props.projectId, editingCharacterId.value, payload)
      ui.showToast('角色已更新', 'success')
    } else {
      await characterStore.addCharacter(props.projectId, payload)
      ui.showToast('角色已新增', 'success')
    }
    cancelCharacterForm()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存角色失败'), 'error')
  } finally {
    savingCharacter.value = false
  }
}

async function deleteCharacter(char: Character) {
  if (!window.confirm(`确定删除角色「${char.name}」吗？`)) return
  try {
    await characterStore.removeCharacter(props.projectId, char.id)
    ui.showToast('角色已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除角色失败'), 'error')
  }
}

function openMergeCharacter(char: Character) {
  mergingCharacterId.value = char.id
  mergeTargetCharacterId.value = suggestedMergeTarget(char)?.id ?? ''
}

function cancelMergeCharacter() {
  mergingCharacterId.value = null
  mergeTargetCharacterId.value = ''
}

function mergeTargetOptions(sourceId: string) {
  return characters.value.filter(char => char.id !== sourceId)
}

function suggestedMergeTarget(source: Character) {
  const candidates = mergeTargetOptions(source.id).filter(char => characterNamesLookSimilar(source.name, char.name))
  return candidates.length === 1 ? candidates[0] : null
}

function characterNamesLookSimilar(a: string, b: string) {
  if (a === b) return true
  if (a.length !== b.length || a.length < 2 || a.length > 4) return false
  if (a[0] !== b[0]) return false
  let diff = 0
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) diff += 1
  }
  return diff === 1
}

async function mergeCharacter() {
  if (!mergingCharacterId.value || !mergeTargetCharacterId.value) {
    ui.showToast('请选择要合并到的目标角色', 'error')
    return
  }
  const source = characters.value.find(char => char.id === mergingCharacterId.value)
  const target = characters.value.find(char => char.id === mergeTargetCharacterId.value)
  if (!source || !target) return
  if (!window.confirm(`确定把「${source.name}」合并到「${target.name}」吗？角色事件会迁移到目标角色。`)) return

  savingMerge.value = true
  try {
    await characterStore.mergeCharacter(props.projectId, source.id, { target_character_id: target.id })
    await characterEventStore.loadCharacterEvents(props.projectId)
    cancelMergeCharacter()
    ui.showToast('角色已合并', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '合并角色失败'), 'error')
  } finally {
    savingMerge.value = false
  }
}

onMounted(async () => {
  if (!characterEventStore.events.length) {
    loadingEvents.value = true
    try {
      await characterEventStore.loadCharacterEvents(props.projectId)
    } catch {
      // silently fail
    } finally {
      loadingEvents.value = false
    }
  }
})
</script>

<template>
  <div class="novel-config">
    <header class="config-header">
      <h2 class="config-title">小说全局配置</h2>
      <button class="btn-close" @click="emit('close')">关闭</button>
    </header>

    <div class="config-body">
      <!-- 整体大纲 -->
      <section class="config-section">
        <div class="section-head">
          <h3>整体大纲</h3>
          <button class="btn-add-inline" :disabled="savingOverview" @click="saveOverview">
            {{ savingOverview ? '保存中...' : '保存' }}
          </button>
        </div>
        <textarea v-model="overviewOutline" class="form-textarea overview-textarea" rows="8"
          placeholder="全书主线、阶段目标、最终结局、核心矛盾"></textarea>
      </section>

      <!-- 概览统计 -->
      <section class="config-section">
        <h3>概览统计</h3>
        <div class="stats-grid">
          <div class="stat-card">
            <span class="stat-num">{{ outline.length }}</span>
            <span class="stat-label">章节大纲</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ characters.length }}</span>
            <span class="stat-label">角色总数</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ allCharacterEvents.length }}</span>
            <span class="stat-label">角色事件</span>
          </div>
        </div>
      </section>

      <!-- 角色总览 -->
      <section class="config-section">
        <div class="section-head">
          <h3>角色总览</h3>
          <button class="btn-add-inline" @click="openNewCharacterForm">+ 新增角色</button>
        </div>
        <div v-if="showCharacterForm" class="inline-form">
          <div class="form-grid">
            <div class="form-row">
              <label>名称 <span class="required">*</span></label>
              <input v-model="characterForm.name" class="form-input" type="text" placeholder="角色名称" />
            </div>
            <div class="form-row">
              <label>类型</label>
              <select v-model="characterForm.role_type" class="form-input">
                <option v-for="option in roleOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
            </div>
            <div class="form-row">
              <label>层级</label>
              <select v-model="characterForm.scope_type" class="form-input">
                <option v-for="option in scopeOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
            </div>
            <div class="form-row">
              <label>阵营</label>
              <input v-model="characterForm.faction" class="form-input" type="text" placeholder="可选" />
            </div>
          </div>
          <div class="form-row">
            <label>简介</label>
            <textarea v-model="characterForm.profile" class="form-textarea" rows="4" placeholder="角色设定、目标、能力、关系"></textarea>
          </div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="savingCharacter" @click="saveCharacter">{{ savingCharacter ? '保存中...' : '保存' }}</button>
            <button class="btn-cancel" @click="cancelCharacterForm">取消</button>
          </div>
        </div>
        <div v-if="mergingCharacterId" class="inline-form merge-form">
          <div class="form-row">
            <label>合并到目标角色</label>
            <select v-model="mergeTargetCharacterId" class="form-input">
              <option value="" disabled>选择目标角色</option>
              <option
                v-for="option in mergeTargetOptions(mergingCharacterId)"
                :key="option.id"
                :value="option.id"
              >
                {{ option.name }} · {{ scopeLabel(option.scope_type) }} · {{ roleLabel(option.role_type) }}
              </option>
            </select>
          </div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="savingMerge" @click="mergeCharacter">
              {{ savingMerge ? '合并中...' : '确认合并' }}
            </button>
            <button class="btn-cancel" @click="cancelMergeCharacter">取消</button>
          </div>
        </div>
        <div v-for="(chars, scope) in charactersByScope" :key="scope" class="char-group">
          <div v-if="chars.length" class="char-group-label">{{ scopeLabel(scope) }}（{{ chars.length }}）</div>
          <div v-if="chars.length" class="char-group-list">
            <div v-for="char in chars" :key="char.id" class="char-mini-card">
              <span class="char-mini-name">{{ char.name }}</span>
              <span class="badge badge-role" :class="char.role_type">{{ roleLabel(char.role_type) }}</span>
              <span v-if="char.faction" class="char-mini-faction">{{ char.faction }}</span>
              <span v-if="char.appearance_count" class="char-mini-appear">{{ char.appearance_count }}次出场</span>
              <span class="mini-actions">
                <button class="mini-btn" title="编辑" @click="editCharacter(char)">编辑</button>
                <button class="mini-btn" title="合并重复角色" @click="openMergeCharacter(char)">合并</button>
                <button class="mini-btn danger" title="删除" @click="deleteCharacter(char)">删除</button>
              </span>
            </div>
          </div>
        </div>
        <div v-if="!characters.length" class="empty-hint">暂无角色，可在章节资料中添加事件或通过「更新记忆」提炼</div>
      </section>

      <!-- 重大事件时间线 -->
      <section class="config-section">
        <div class="section-head">
          <h3>重大事件时间线</h3>
          <span v-if="loadingEvents" class="loading-tip">加载中...</span>
        </div>
        <div v-if="eventsByChapter.size" class="events-timeline">
          <div v-for="[seq, events] in eventsByChapter" :key="seq" class="event-chapter-group">
            <div class="event-chapter-label">第 {{ seq }} 章</div>
            <div class="event-chapter-list">
              <div v-for="event in events" :key="event.id" class="event-card" :class="event.appearance_type">
                <div class="event-card-head">
                  <span class="event-char-name">{{ characterName(event.character_id) }}</span>
                  <span class="badge badge-appearance" :class="event.appearance_type">{{ appearanceLabel(event.appearance_type) }}</span>
                  <span v-if="event.importance" class="event-importance">重要度: {{ event.importance }}/5</span>
                </div>
                <p v-if="event.event_summary" class="event-summary">{{ event.event_summary }}</p>
                <div v-if="event.actions?.length" class="event-actions">
                  <span v-for="(action, i) in event.actions" :key="i" class="action-tag">{{ action }}</span>
                </div>
                <div v-if="event.location || event.emotion" class="event-meta">
                  <span v-if="event.location" class="event-location">📍 {{ event.location }}</span>
                  <span v-if="event.emotion" class="event-emotion">{{ event.emotion }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-else-if="!loadingEvents" class="empty-hint">暂无重大事件记录，可在章节中通过「更新记忆」提炼</div>
      </section>

      <!-- 大纲总览 -->
      <section class="config-section">
        <div class="section-head">
          <h3>大纲总览</h3>
        </div>
        <div v-if="outline.length" class="outline-list">
          <div v-for="item in outline" :key="item.id" class="outline-mini-item">
            <span class="outline-mini-num">{{ item.chapter_num }}</span>
            <div class="outline-mini-body">
              <span class="outline-mini-title">{{ item.title }}</span>
              <p v-if="item.summary" class="outline-mini-summary">{{ item.summary }}</p>
              <div v-if="item.turning_point" class="turning-point">↯ {{ item.turning_point }}</div>
            </div>
          </div>
        </div>
        <div v-else class="empty-hint">暂无大纲，可进入章节资料维护，或通过「更新记忆」提炼</div>
      </section>
    </div>
  </div>
</template>

<script lang="ts">
function characterName(characterId: string): string {
  const charStore = useCharacterStore()
  const char = charStore.characters.find(c => c.id === characterId)
  return char?.name ?? characterId.slice(0, 8)
}
</script>

<style scoped>
.novel-config {
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
  overflow-x: hidden;
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
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}
.section-head h3 {
  margin: 0;
}
.btn-add-inline,
.btn-submit,
.btn-cancel,
.mini-btn {
  border-radius: 8px;
  font-size: var(--text-xs);
  font-weight: 650;
  cursor: pointer;
}
.btn-add-inline {
  border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
  background: var(--accent-subtle);
  color: var(--accent);
  padding: 5px 10px;
}
.btn-add-inline:hover {
  border-color: var(--accent);
}
.inline-form {
  padding: var(--sp-3);
  margin-bottom: var(--sp-4);
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
  border-radius: 10px;
  background: var(--bg-hover);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--sp-3);
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-bottom: var(--sp-3);
}
.form-row label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  font-weight: 600;
}
.required {
  color: var(--status-reviewing);
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
  justify-content: flex-end;
  gap: var(--sp-2);
}
.btn-submit {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--text-inverse);
  padding: 6px 14px;
}
.btn-cancel {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--text-secondary);
  padding: 6px 12px;
}
.btn-submit:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.overview-textarea {
  min-height: 180px;
  resize: vertical;
}

/* Stats */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--sp-3);
}
.stat-card {
  padding: var(--sp-3);
  background: var(--bg-hover);
  border-radius: 10px;
  text-align: center;
}
.stat-num {
  display: block;
  font-size: 24px;
  font-weight: 700;
  color: var(--text);
}
.stat-label {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin-top: 2px;
}

.mini-actions {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mini-btn {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--text-secondary);
  padding: 2px 7px;
}
.mini-btn:hover {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.mini-btn.danger:hover {
  color: var(--status-error);
  border-color: color-mix(in srgb, var(--status-error) 45%, var(--border));
}
/* Characters */
.char-group + .char-group {
  margin-top: var(--sp-3);
}
.char-group-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: var(--sp-2);
}
.char-group-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.char-mini-card {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--bg-hover);
  border-radius: 8px;
  font-size: var(--text-sm);
}
.char-mini-name {
  font-weight: 600;
  color: var(--text);
}
.char-mini-faction {
  font-size: 11px;
  color: var(--text-tertiary);
}
.char-mini-appear {
  font-size: 10px;
  color: var(--text-tertiary);
}

/* Events timeline */
.events-timeline {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.event-chapter-group {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.event-chapter-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--accent);
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
}
.event-chapter-list {
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
.event-card.appeared {
  border-left: 3px solid var(--accent);
}
.event-card.mentioned {
  border-left: 3px solid var(--status-reviewing);
}
.event-card.absent {
  border-left: 3px solid var(--text-tertiary);
  opacity: 0.7;
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
  margin-left: auto;
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
.event-meta {
  display: flex;
  gap: var(--sp-3);
  margin-top: 4px;
  font-size: 11px;
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

/* Outline */
.outline-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.outline-mini-item {
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}
.outline-mini-num {
  font-size: var(--text-sm);
  font-weight: 700;
  color: var(--accent);
  min-width: 28px;
}
.outline-mini-body {
  min-width: 0;
  flex: 1;
}
.outline-mini-title {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--text);
}
.outline-mini-summary {
  margin: 2px 0 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.5;
}
.turning-point {
  font-size: 11px;
  color: var(--accent);
  margin-top: 2px;
}

.loading-tip {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

@media (max-width: 768px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
