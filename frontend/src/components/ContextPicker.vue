<script setup lang="ts">
import { ref, computed } from 'vue'
import type { OutlineItem, Character, WorldEntry, HiddenThread, ProjectMode } from '../api/types'

const props = defineProps<{
  projectId: string
  outlines: OutlineItem[]
  characters: Character[]
  worldEntries: WorldEntry[]
  hiddenThreads: HiddenThread[]
  currentChapterNum?: number
  mode?: ProjectMode
}>()

const emit = defineEmits<{
  confirm: [outlineIds: string[], characterIds: string[], worldEntryIds: string[], hiddenThreadIds: string[], targetWords: number]
  cancel: []
}>()

const isNovel = computed(() => props.mode !== 'article')

const sectionLabels = computed(() => ({
  outlines: isNovel.value ? '大纲' : '内容结构',
  characters: isNovel.value ? '角色' : '受众画像',
  worldEntries: isNovel.value ? '世界观' : '品牌/产品资料',
  hiddenThreads: isNovel.value ? '暗线' : '内容策略',
}))

const ROLE_LABELS: Record<string, string> = {
  protagonist: isNovel.value ? '主角' : '核心受众',
  antagonist: isNovel.value ? '反派' : '反对人群',
  supporting: isNovel.value ? '配角' : '影响者',
  minor: isNovel.value ? '路人' : '泛受众',
}

const CONFIDENCE_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

const pickerTitle = computed(() => isNovel.value ? '选择创作素材' : '选择内容素材')
const pickerSubtitle = computed(() =>
  isNovel.value
    ? '选择本次生成要用的大纲、角色和世界观设定'
    : '选择本次生成要用的内容结构、受众画像和品牌资料'
)
const confirmLabel = computed(() => isNovel.value ? '确认生成' : '确认生成内容')

// Default: all selected
const selectedOutlines = ref<string[]>(props.outlines.map(o => o.id))
const selectedCharacters = ref<string[]>(props.characters.map(c => c.id))
const selectedWorldEntries = ref<string[]>(props.worldEntries.map(w => w.id))
// Hidden threads: default-check those matching current chapter
const selectedHiddenThreads = ref<string[]>(
  props.currentChapterNum
    ? props.hiddenThreads.filter(ht => ht.chapter_nums.includes(props.currentChapterNum!)).map(ht => ht.id)
    : props.hiddenThreads.map(ht => ht.id),
)
const targetWords = ref(2000)

const openSections = ref({ outlines: true, characters: false, worldEntries: false, hiddenThreads: false })

function toggleSection(key: 'outlines' | 'characters' | 'worldEntries' | 'hiddenThreads') {
  openSections.value[key] = !openSections.value[key]
}

function toggleAllOutlines() {
  selectedOutlines.value = selectedOutlines.value.length === props.outlines.length ? [] : props.outlines.map(o => o.id)
}
function toggleAllCharacters() {
  selectedCharacters.value = selectedCharacters.value.length === props.characters.length ? [] : props.characters.map(c => c.id)
}
function toggleAllWorldEntries() {
  selectedWorldEntries.value = selectedWorldEntries.value.length === props.worldEntries.length ? [] : props.worldEntries.map(w => w.id)
}
function toggleAllHiddenThreads() {
  selectedHiddenThreads.value = selectedHiddenThreads.value.length === props.hiddenThreads.length ? [] : props.hiddenThreads.map(ht => ht.id)
}
function toggleHiddenThread(id: string) {
  const idx = selectedHiddenThreads.value.indexOf(id)
  if (idx >= 0) selectedHiddenThreads.value.splice(idx, 1)
  else selectedHiddenThreads.value.push(id)
}

function toggleOutline(id: string) {
  const idx = selectedOutlines.value.indexOf(id)
  if (idx >= 0) selectedOutlines.value.splice(idx, 1)
  else selectedOutlines.value.push(id)
}

function toggleCharacter(id: string) {
  const idx = selectedCharacters.value.indexOf(id)
  if (idx >= 0) selectedCharacters.value.splice(idx, 1)
  else selectedCharacters.value.push(id)
}

function toggleWorldEntry(id: string) {
  const idx = selectedWorldEntries.value.indexOf(id)
  if (idx >= 0) selectedWorldEntries.value.splice(idx, 1)
  else selectedWorldEntries.value.push(id)
}

function truncate(text: string, max: number): string {
  if (!text) return ''
  return text.length > max ? text.slice(0, max) + '…' : text
}

function handleConfirm() {
  emit('confirm', selectedOutlines.value, selectedCharacters.value, selectedWorldEntries.value, selectedHiddenThreads.value, targetWords.value)
}
</script>

<template>
  <div class="picker-overlay" @click.self="emit('cancel')">
    <div class="picker-card">
      <div class="picker-header">
        <h3>{{ pickerTitle }}</h3>
        <p class="picker-subtitle">{{ pickerSubtitle }}</p>
      </div>

      <div class="picker-body">
        <!-- Outlines -->
        <div class="section">
          <div class="section-header" @click="toggleSection('outlines')">
            <span class="section-chevron" :class="{ open: openSections.outlines }">▾</span>
            <span class="section-title">{{ sectionLabels.outlines }}</span>
            <label class="select-all" @click.stop>
              <input type="checkbox" :checked="selectedOutlines.length === outlines.length && outlines.length > 0" @change="toggleAllOutlines" />
              <span>全选</span>
            </label>
          </div>
          <div v-if="openSections.outlines" class="section-list">
            <div v-if="!outlines.length" class="empty-hint">暂无{{ sectionLabels.outlines }}</div>
            <div v-for="o in outlines" :key="o.id" class="item" @click="toggleOutline(o.id)">
              <input type="checkbox" :checked="selectedOutlines.includes(o.id)" class="item-checkbox" />
              <span class="item-main">
                <span class="item-title">{{ isNovel ? `第${o.chapter_num}章 ${o.title}` : o.title }}</span>
                <span v-if="o.summary" class="item-desc">{{ truncate(o.summary, 80) }}</span>
              </span>
            </div>
          </div>
        </div>

        <!-- Characters -->
        <div class="section">
          <div class="section-header" @click="toggleSection('characters')">
            <span class="section-chevron" :class="{ open: openSections.characters }">▾</span>
            <span class="section-title">{{ sectionLabels.characters }}</span>
            <label class="select-all" @click.stop>
              <input type="checkbox" :checked="selectedCharacters.length === characters.length && characters.length > 0" @change="toggleAllCharacters" />
              <span>全选</span>
            </label>
          </div>
          <div v-if="openSections.characters" class="section-list">
            <div v-if="!characters.length" class="empty-hint">暂无{{ sectionLabels.characters }}</div>
            <div v-for="c in characters" :key="c.id" class="item" @click="toggleCharacter(c.id)">
              <input type="checkbox" :checked="selectedCharacters.includes(c.id)" class="item-checkbox" />
              <span class="item-main">
                <span class="item-title">{{ c.name }} <span class="badge role-badge">{{ ROLE_LABELS[c.role_type] ?? c.role_type }}</span></span>
                <span v-if="c.profile" class="item-desc">{{ truncate(c.profile, 60) }}</span>
              </span>
            </div>
          </div>
        </div>

        <!-- World Entries -->
        <div class="section">
          <div class="section-header" @click="toggleSection('worldEntries')">
            <span class="section-chevron" :class="{ open: openSections.worldEntries }">▾</span>
            <span class="section-title">{{ sectionLabels.worldEntries }}</span>
            <label class="select-all" @click.stop>
              <input type="checkbox" :checked="selectedWorldEntries.length === worldEntries.length && worldEntries.length > 0" @change="toggleAllWorldEntries" />
              <span>全选</span>
            </label>
          </div>
          <div v-if="openSections.worldEntries" class="section-list">
            <div v-if="!worldEntries.length" class="empty-hint">暂无{{ sectionLabels.worldEntries }}</div>
            <div v-for="w in worldEntries" :key="w.id" class="item" @click="toggleWorldEntry(w.id)">
              <input type="checkbox" :checked="selectedWorldEntries.includes(w.id)" class="item-checkbox" />
              <span class="item-main">
                <span class="item-title">{{ w.title }} <span v-if="w.category" class="badge cat-badge">[{{ w.category }}]</span> <span class="badge conf-badge" :class="w.confidence">{{ CONFIDENCE_LABELS[w.confidence] ?? w.confidence }}</span></span>
                <span v-if="w.content" class="item-desc">{{ truncate(w.content, 60) }}</span>
              </span>
            </div>
          </div>
        </div>

        <!-- Hidden Threads -->
        <div class="section">
          <div class="section-header" @click="toggleSection('hiddenThreads')">
            <span class="section-chevron" :class="{ open: openSections.hiddenThreads }">▾</span>
            <span class="section-title">{{ sectionLabels.hiddenThreads }}</span>
            <label class="select-all" @click.stop>
              <input type="checkbox" :checked="selectedHiddenThreads.length === hiddenThreads.length && hiddenThreads.length > 0" @change="toggleAllHiddenThreads" />
              <span>全选</span>
            </label>
          </div>
          <div v-if="openSections.hiddenThreads" class="section-list">
            <div v-if="!hiddenThreads.length" class="empty-hint">暂无{{ sectionLabels.hiddenThreads }}</div>
            <div v-for="ht in hiddenThreads" :key="ht.id" class="item" @click="toggleHiddenThread(ht.id)">
              <input type="checkbox" :checked="selectedHiddenThreads.includes(ht.id)" class="item-checkbox" />
              <span class="item-main">
                <span class="item-title">{{ ht.name }} <span v-if="currentChapterNum && ht.chapter_nums.includes(currentChapterNum)" class="badge auto-badge">自动匹配</span></span>
                <span v-if="ht.description" class="item-desc">{{ truncate(ht.description, 60) }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      <div class="picker-footer">
        <div class="target-words-group">
          <label class="target-label">目标字数</label>
          <input v-model.number="targetWords" type="number" min="500" max="10000" class="target-input" />
        </div>
        <button class="btn-cancel" @click="emit('cancel')">取消</button>
        <button class="btn-confirm" @click="handleConfirm">{{ confirmLabel }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.picker-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 90vw;
  max-width: 680px;
  height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.picker-header {
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.picker-header h3 {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text);
  margin: 0;
}
.picker-subtitle {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: var(--sp-1) 0 0;
}
.picker-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-height: 0;
}

/* Sections */
.section {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  flex-shrink: 0;
}
.section-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg);
  cursor: pointer;
  user-select: none;
  transition: background var(--transition);
}
.section-header:hover { background: var(--bg-hover); }
.section-chevron {
  font-size: 10px;
  color: var(--text-tertiary);
  transition: transform var(--transition);
}
.section-chevron.open { transform: rotate(180deg); }
.section-title {
  flex: 1;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text);
}
.select-all {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
}
.section-list {
  padding: var(--sp-1) 0;
  max-height: 180px;
  overflow-y: auto;
}

/* Items */
.item {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  cursor: pointer;
  transition: background var(--transition);
}
.item:hover { background: var(--bg-hover); }
.item-checkbox {
  accent-color: var(--accent);
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  margin-top: 3px;
  cursor: pointer;
}
.item-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.item-title {
  font-size: var(--text-sm);
  color: var(--text);
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
  overflow-wrap: break-word;
}
.item-desc {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.4;
}

/* Badges */
.badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 6px;
  white-space: nowrap;
}
.role-badge { background: var(--status-final-bg); color: var(--status-final); }
.cat-badge { background: var(--bg); color: var(--text-secondary); }
.conf-badge.high { background: var(--status-final-bg); color: var(--status-final); }
.conf-badge.medium { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.conf-badge.low { background: var(--status-draft-bg); color: var(--status-draft); }
.auto-badge { background: var(--accent-subtle); color: var(--accent); }

.empty-hint {
  padding: var(--sp-3);
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  text-align: center;
}

/* Footer */
.picker-footer {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.target-words-group {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-right: auto;
}
.target-label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  white-space: nowrap;
}
.target-input {
  width: 80px;
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  transition: border-color var(--transition);
}
.target-input:focus { border-color: var(--border-focus); outline: none; }
.btn-cancel {
  padding: var(--sp-2) var(--sp-4);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-cancel:hover { background: var(--bg-hover); }
.btn-confirm {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition);
}
.btn-confirm:hover { background: var(--accent-hover); }
</style>