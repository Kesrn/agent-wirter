<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  useCharacterStore,
  useWorldEntryStore,
  useOutlineStore,
  useHiddenThreadStore,
  useUiStore,
} from '../stores'
import type { Character, WorldEntry, ProjectMode } from '../api/types'
import ProjectWorldLibrary from './ProjectWorldLibrary.vue'
import MemoryStagingPanel from './MemoryStagingPanel.vue'

/**
 * 左侧资料库弹窗：人物档案 / 世界设定 / 故事大纲 / 创作记忆。
 *
 * - 世界设定：嵌入 ProjectWorldLibrary（完整管理：增删改查）；
 * - 创作记忆：内部分「暗线 / 写作记忆」两个子 tab，写作记忆嵌入 MemoryStagingPanel；
 * - 人物档案 / 故事大纲 / 暗线：直接用对应 store 的 CRUD（新增 / 编辑 / 删除）。
 *
 * 每个数据源都展示加载中 / 加载失败（loadError）/ 空态，失败时不允许静默
 * 回退到旧缓存或 Mock 数据（请求失败会明确提示，不会把假数据当真实资料）。
 */

export type LibraryKind = 'characters' | 'world' | 'outlines' | 'threads'

const props = defineProps<{
  projectId: string
  projectMode: ProjectMode
  active: LibraryKind | null
}>()

const emit = defineEmits<{ close: [] }>()

const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const outlineStore = useOutlineStore()
const hiddenThreadStore = useHiddenThreadStore()
const ui = useUiStore()

const kind = ref<LibraryKind>(props.active ?? 'characters')
watch(() => props.active, (v) => { if (v) kind.value = v })

// 创作记忆子 tab：暗线 / 写作记忆
const threadTab = ref<'threads' | 'memory'>('threads')

const labels: Record<LibraryKind, string> = {
  characters: '人物档案',
  world: '世界设定',
  outlines: '故事大纲',
  threads: '创作记忆',
}

const roleTypeLabels: Record<Character['role_type'], string> = {
  protagonist: '主角',
  antagonist: '反派',
  supporting: '配角',
  minor: '龙套',
}

const confidenceLabels: Record<WorldEntry['confidence'], string> = {
  low: '低置信',
  medium: '中置信',
  high: '高置信',
}

// ─── 数据（含加载态 / 错误态） ───

const characters = computed(() => characterStore.charactersForProject(props.projectId))
const worldEntries = computed(() => worldEntryStore.entriesForProject(props.projectId))
const outlines = computed(() => outlineStore.entriesForProject(props.projectId))
const threads = computed(() => hiddenThreadStore.threadsForProject(props.projectId))

function openKind(next: LibraryKind): void {
  kind.value = next
  if (next === 'characters') void characterStore.loadCharacters(props.projectId)
  if (next === 'world') void worldEntryStore.loadWorldEntries(props.projectId)
  if (next === 'outlines') void outlineStore.loadOutlines(props.projectId)
  if (next === 'threads') {
    void hiddenThreadStore.loadHiddenThreads(props.projectId)
    threadTab.value = 'threads'
  }
}

// ─── 人物档案 CRUD ───

const newCharacterName = ref('')
const newCharacterRole = ref<Character['role_type']>('supporting')
const editingCharacterId = ref<string | null>(null)
const editingCharacterName = ref('')
const editingCharacterProfile = ref('')

function startAddCharacter(): void {
  newCharacterName.value = ''
  newCharacterRole.value = 'supporting'
}

async function addCharacter(): Promise<void> {
  const name = newCharacterName.value.trim()
  if (!name) return
  const ok = await characterStore.addCharacter(props.projectId, { name, role_type: newCharacterRole.value })
  if (ok) {
    ui.showToast('角色已新增', 'success')
    newCharacterName.value = ''
  } else {
    ui.showToast('新增角色失败', 'error')
  }
}

function startEditCharacter(c: Character): void {
  editingCharacterId.value = c.id
  editingCharacterName.value = c.name
  editingCharacterProfile.value = c.profile ?? ''
}

async function saveEditCharacter(): Promise<void> {
  if (!editingCharacterId.value) return
  await characterStore.updateCharacter(props.projectId, editingCharacterId.value, {
    name: editingCharacterName.value.trim() || undefined,
    profile: editingCharacterProfile.value.trim() || undefined,
  })
  ui.showToast('角色已更新', 'success')
  editingCharacterId.value = null
}

async function removeCharacter(c: Character): Promise<void> {
  await characterStore.removeCharacter(props.projectId, c.id)
  ui.showToast('角色已删除', 'success')
}

// ─── 故事大纲 CRUD ───

const newOutlineTitle = ref('')
const newOutlineSummary = ref('')
const editingOutlineId = ref<string | null>(null)
const editingOutlineTitle = ref('')
const editingOutlineSummary = ref('')

async function addOutline(): Promise<void> {
  const title = newOutlineTitle.value.trim()
  if (!title) return
  const nextNum = outlines.value.length
    ? Math.max(...outlines.value.map(o => o.chapter_num)) + 1
    : 1
  const ok = await outlineStore.createOutlineItem(props.projectId, {
    sequence_number: nextNum,
    title,
    summary: newOutlineSummary.value.trim() || undefined,
  })
  if (ok) {
    ui.showToast('大纲已新增', 'success')
    newOutlineTitle.value = ''
    newOutlineSummary.value = ''
  } else {
    ui.showToast('新增大纲失败', 'error')
  }
}

function startEditOutline(o: { id: string; title: string; summary: string }): void {
  editingOutlineId.value = o.id
  editingOutlineTitle.value = o.title
  editingOutlineSummary.value = o.summary
}

async function saveEditOutline(): Promise<void> {
  if (!editingOutlineId.value) return
  await outlineStore.updateOutlineItem(props.projectId, editingOutlineId.value, {
    title: editingOutlineTitle.value.trim() || undefined,
    summary: editingOutlineSummary.value.trim() || undefined,
  })
  ui.showToast('大纲已更新', 'success')
  editingOutlineId.value = null
}

async function removeOutline(o: { id: string }): Promise<void> {
  await outlineStore.deleteOutlineItem(props.projectId, o.id)
  ui.showToast('大纲已删除', 'success')
}

// ─── 暗线 CRUD ───

const newThreadName = ref('')
const newThreadDescription = ref('')
const editingThreadId = ref<string | null>(null)
const editingThreadName = ref('')
const editingThreadDescription = ref('')

async function addThread(): Promise<void> {
  const name = newThreadName.value.trim()
  if (!name) return
  const ok = await hiddenThreadStore.createHiddenThreadItem(props.projectId, {
    name,
    description: newThreadDescription.value.trim() || undefined,
  })
  if (ok) {
    ui.showToast('暗线已新增', 'success')
    newThreadName.value = ''
    newThreadDescription.value = ''
  } else {
    ui.showToast('新增暗线失败', 'error')
  }
}

function startEditThread(t: { id: string; name: string; description: string }): void {
  editingThreadId.value = t.id
  editingThreadName.value = t.name
  editingThreadDescription.value = t.description ?? ''
}

async function saveEditThread(): Promise<void> {
  if (!editingThreadId.value) return
  await hiddenThreadStore.updateHiddenThreadItem(props.projectId, editingThreadId.value, {
    name: editingThreadName.value.trim() || undefined,
    description: editingThreadDescription.value.trim() || undefined,
  })
  ui.showToast('暗线已更新', 'success')
  editingThreadId.value = null
}

async function removeThread(t: { id: string }): Promise<void> {
  await hiddenThreadStore.deleteHiddenThreadItem(props.projectId, t.id)
  ui.showToast('暗线已删除', 'success')
}

// ─── 错误兜底：失败时明确提示，不静默回退旧数据/Mock ───

function errorText(loadError: string, label: string): string {
  if (!loadError) return ''
  return `${label}加载失败：${loadError}`
}
</script>

<template>
  <Teleport to="body">
    <div class="plp-overlay" @click.self="emit('close')">
      <section class="plp-panel" role="dialog" aria-modal="true" :aria-label="labels[kind]">
        <header class="plp-head">
          <h3>资料库</h3>
          <button type="button" class="plp-close" aria-label="关闭" @click="emit('close')">×</button>
        </header>

        <nav class="plp-tabs" role="tablist" aria-label="资料库分类">
          <button
            v-for="(label, key) in labels" :key="key"
            type="button" role="tab" class="plp-tab"
            :class="{ active: kind === key }"
            :aria-selected="kind === key"
            @click="openKind(key)"
          >{{ label }}</button>
        </nav>

        <div class="plp-body">
          <!-- 人物档案：完整管理（新增 / 编辑 / 删除） -->
          <div v-if="kind === 'characters'">
            <div v-if="characterStore.loading" class="plp-state">加载中...</div>
            <div v-else-if="errorText(characterStore.loadError, '人物档案')" class="plp-state plp-error">
              {{ errorText(characterStore.loadError, '人物档案') }}
            </div>
            <template v-else>
              <p v-if="!characters.length" class="plp-state">暂无人物档案。可在下方新增，或在章节资料中通过「提炼」积累。</p>
              <div v-for="c in characters" :key="c.id" class="plp-item">
                <template v-if="editingCharacterId === c.id">
                  <input v-model="editingCharacterName" class="plp-input" placeholder="名称" />
                  <textarea v-model="editingCharacterProfile" class="plp-input" rows="2" placeholder="设定描述"></textarea>
                  <div class="plp-item-actions">
                    <button type="button" class="plp-btn plp-btn-primary" @click="saveEditCharacter">保存</button>
                    <button type="button" class="plp-btn" @click="editingCharacterId = null">取消</button>
                  </div>
                </template>
                <template v-else>
                  <div class="plp-item-head">
                    <strong>{{ c.name }}</strong>
                    <span class="plp-tag">{{ roleTypeLabels[c.role_type] ?? c.role_type }}</span>
                    <span v-if="c.appearance_count != null" class="plp-tag plp-tag-muted">出场 {{ c.appearance_count }}</span>
                  </div>
                  <p v-if="c.profile" class="plp-item-desc">{{ c.profile }}</p>
                  <div class="plp-item-actions">
                    <button type="button" class="plp-btn" @click="startEditCharacter(c)">编辑</button>
                    <button type="button" class="plp-btn plp-btn-danger" @click="removeCharacter(c)">删除</button>
                  </div>
                </template>
              </div>
              <div class="plp-add-form">
                <input v-model="newCharacterName" class="plp-input" placeholder="角色名称" @focus="startAddCharacter" />
                <select v-model="newCharacterRole" class="plp-input plp-select">
                  <option value="protagonist">主角</option>
                  <option value="antagonist">反派</option>
                  <option value="supporting">配角</option>
                  <option value="minor">龙套</option>
                </select>
                <button type="button" class="plp-btn plp-btn-primary" :disabled="!newCharacterName.trim()" @click="addCharacter">新增角色</button>
              </div>
            </template>
          </div>

          <!-- 世界设定：嵌入 ProjectWorldLibrary（完整管理） -->
          <div v-else-if="kind === 'world'">
            <div v-if="worldEntryStore.loading" class="plp-state">加载中...</div>
            <div v-else-if="errorText(worldEntryStore.loadError, '世界设定')" class="plp-state plp-error">
              {{ errorText(worldEntryStore.loadError, '世界设定') }}
            </div>
            <template v-else>
              <p v-if="!worldEntries.length" class="plp-state">暂无世界设定。</p>
              <div v-for="e in worldEntries" :key="e.id" class="plp-item">
                <div class="plp-item-head">
                  <strong>{{ e.title }}</strong>
                  <span v-if="e.category" class="plp-tag">{{ e.category }}</span>
                  <span class="plp-tag plp-tag-muted">{{ confidenceLabels[e.confidence] ?? e.confidence }}</span>
                </div>
                <p v-if="e.content" class="plp-item-desc">{{ e.content }}</p>
              </div>
              <ProjectWorldLibrary
                :project-id="projectId"
                :project-mode="projectMode"
                @close="() => {}"
              />
            </template>
          </div>

          <!-- 故事大纲：完整管理（新增 / 编辑 / 删除） -->
          <div v-else-if="kind === 'outlines'">
            <div v-if="outlineStore.loading" class="plp-state">加载中...</div>
            <div v-else-if="errorText(outlineStore.loadError, '故事大纲')" class="plp-state plp-error">
              {{ errorText(outlineStore.loadError, '故事大纲') }}
            </div>
            <template v-else>
              <p v-if="!outlines.length" class="plp-state">暂无章节大纲。可在下方新增，或在章节资料中维护。</p>
              <div v-for="o in outlines" :key="o.id" class="plp-item">
                <template v-if="editingOutlineId === o.id">
                  <input v-model="editingOutlineTitle" class="plp-input" placeholder="大纲标题" />
                  <textarea v-model="editingOutlineSummary" class="plp-input" rows="2" placeholder="摘要"></textarea>
                  <div class="plp-item-actions">
                    <button type="button" class="plp-btn plp-btn-primary" @click="saveEditOutline">保存</button>
                    <button type="button" class="plp-btn" @click="editingOutlineId = null">取消</button>
                  </div>
                </template>
                <template v-else>
                  <div class="plp-item-head">
                    <strong>{{ o.title }}</strong>
                    <span class="plp-tag plp-tag-muted">第 {{ o.chapter_num }} 章</span>
                  </div>
                  <p v-if="o.summary" class="plp-item-desc">{{ o.summary }}</p>
                  <p v-if="o.turning_point" class="plp-item-desc plp-turning">转折：{{ o.turning_point }}</p>
                  <div class="plp-item-actions">
                    <button type="button" class="plp-btn" @click="startEditOutline(o)">编辑</button>
                    <button type="button" class="plp-btn plp-btn-danger" @click="removeOutline(o)">删除</button>
                  </div>
                </template>
              </div>
              <div class="plp-add-form">
                <input v-model="newOutlineTitle" class="plp-input" placeholder="大纲标题" />
                <textarea v-model="newOutlineSummary" class="plp-input" rows="2" placeholder="摘要（可选）"></textarea>
                <button type="button" class="plp-btn plp-btn-primary" :disabled="!newOutlineTitle.trim()" @click="addOutline">新增大纲</button>
              </div>
            </template>
          </div>

          <!-- 创作记忆：暗线 / 写作记忆 双子 tab -->
          <div v-else-if="kind === 'threads'">
            <div class="plp-subtabs" role="tablist" aria-label="创作记忆分类">
              <button type="button" role="tab" class="plp-subtab" :class="{ active: threadTab === 'threads' }" @click="threadTab = 'threads'">暗线</button>
              <button type="button" role="tab" class="plp-subtab" :class="{ active: threadTab === 'memory' }" @click="threadTab = 'memory'">写作记忆</button>
            </div>

            <div v-if="threadTab === 'threads'">
              <div v-if="hiddenThreadStore.loading" class="plp-state">加载中...</div>
              <div v-else-if="errorText(hiddenThreadStore.loadError, '暗线')" class="plp-state plp-error">
                {{ errorText(hiddenThreadStore.loadError, '暗线') }}
              </div>
              <template v-else>
                <p v-if="!threads.length" class="plp-state">暂无暗线。可在下方新增，或在章节大纲中维护。</p>
                <div v-for="t in threads" :key="t.id" class="plp-item">
                  <template v-if="editingThreadId === t.id">
                    <input v-model="editingThreadName" class="plp-input" placeholder="暗线名称" />
                    <textarea v-model="editingThreadDescription" class="plp-input" rows="2" placeholder="描述"></textarea>
                    <div class="plp-item-actions">
                      <button type="button" class="plp-btn plp-btn-primary" @click="saveEditThread">保存</button>
                      <button type="button" class="plp-btn" @click="editingThreadId = null">取消</button>
                    </div>
                  </template>
                  <template v-else>
                    <div class="plp-item-head">
                      <strong>{{ t.name }}</strong>
                      <span class="plp-tag plp-tag-muted">{{ t.status }}</span>
                      <span v-if="t.chapter_nums?.length" class="plp-tag plp-tag-muted">章节 {{ t.chapter_nums.join('、') }}</span>
                    </div>
                    <p v-if="t.description" class="plp-item-desc">{{ t.description }}</p>
                    <div class="plp-item-actions">
                      <button type="button" class="plp-btn" @click="startEditThread(t)">编辑</button>
                      <button type="button" class="plp-btn plp-btn-danger" @click="removeThread(t)">删除</button>
                    </div>
                  </template>
                </div>
                <div class="plp-add-form">
                  <input v-model="newThreadName" class="plp-input" placeholder="暗线名称" />
                  <textarea v-model="newThreadDescription" class="plp-input" rows="2" placeholder="描述（可选）"></textarea>
                  <button type="button" class="plp-btn plp-btn-primary" :disabled="!newThreadName.trim()" @click="addThread">新增暗线</button>
                </div>
              </template>
            </div>

            <div v-else>
              <!-- 写作记忆：嵌入 MemoryStagingPanel（确认 / 拒绝） -->
              <MemoryStagingPanel :project-id="projectId" />
            </div>
          </div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.plp-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: flex-start;
  justify-content: flex-start;
  padding: 72px 0 0 208px;
  background: var(--bg-overlay);
  backdrop-filter: blur(8px);
}

.plp-panel {
  width: min(460px, calc(100vw - 240px));
  max-height: min(600px, calc(100vh - 120px));
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg, 12px);
  background: var(--bg-panel);
  box-shadow: var(--shadow-modal);
  overflow: hidden;
}

.plp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}

.plp-head h3 {
  margin: 0;
  font-size: 15px;
  color: var(--text);
}

.plp-close {
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--text-tertiary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}

.plp-close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.plp-tabs {
  display: flex;
  gap: 4px;
  padding: 10px 14px 0;
  border-bottom: 1px solid var(--border);
}

.plp-tab {
  padding: 7px 12px;
  border: 0;
  border-radius: var(--radius-sm, 6px) var(--radius-sm, 6px) 0 0;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 12.5px;
  cursor: pointer;
}

.plp-tab.active {
  background: var(--accent-subtle);
  color: var(--accent);
}

.plp-subtabs {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.plp-subtab {
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 12px;
  cursor: pointer;
}

.plp-subtab.active {
  border-color: var(--accent);
  background: var(--accent-subtle);
  color: var(--accent);
}

.plp-body {
  padding: 10px 14px 14px;
  overflow-y: auto;
}

.plp-state {
  margin: 12px 4px;
  color: var(--text-tertiary);
  font-size: 12.5px;
  line-height: 1.7;
}

.plp-error {
  color: var(--status-error);
}

.plp-item {
  padding: 10px 12px;
  margin: 6px 0;
  border: 1px solid var(--border);
  border-radius: var(--radius, 10px);
  background: var(--bg-hover);
}

.plp-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plp-item-head strong {
  font-size: 13.5px;
  color: var(--text);
}

.plp-tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: 11px;
}

.plp-tag-muted {
  background: var(--bg);
  color: var(--text-tertiary);
}

.plp-item-desc {
  margin: 7px 0 0;
  color: var(--text-secondary);
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.plp-item-desc.plp-turning {
  color: var(--status-reviewing);
}

.plp-item-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.plp-btn {
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.plp-btn:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.plp-btn-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--text-inverse);
}

.plp-btn-primary:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.plp-btn-danger {
  border-color: var(--status-error);
  color: var(--status-error);
  background: var(--status-error-bg);
}

.plp-add-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  padding: 12px;
  border: 1px dashed var(--border);
  border-radius: var(--radius, 10px);
}

.plp-input {
  width: 100%;
  box-sizing: border-box;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg);
  color: var(--text);
  font-size: 12.5px;
}

.plp-input:focus {
  outline: none;
  border-color: var(--accent);
}

.plp-select {
  width: auto;
}

@media (max-width: 1080px) {
  .plp-overlay {
    padding: 64px 12px 12px;
    justify-content: center;
  }
  .plp-panel {
    width: min(460px, 100%);
    max-height: calc(100vh - 96px);
  }
}
</style>
