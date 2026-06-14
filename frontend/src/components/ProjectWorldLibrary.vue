<script setup lang="ts">
import { computed, ref } from 'vue'
import { useUiStore, useWorldEntryStore, friendlyError } from '../stores'
import type { ProjectMode, WorldEntry } from '../api/types'

type EntryForm = {
  title: string
  category: string
  scope_type: WorldEntry['scope_type']
  content: string
  confidence: WorldEntry['confidence']
}

const props = defineProps<{
  projectId: string
  projectMode: ProjectMode
}>()

const emit = defineEmits<{
  close: []
}>()

const worldEntryStore = useWorldEntryStore()
const ui = useUiStore()

const labels = computed(() => {
  if (props.projectMode === 'article') {
    return {
      title: '品牌/产品资料库',
      entryName: '资料',
      addLabel: '+ 新增资料',
      titlePlaceholder: '资料标题',
      categoryPlaceholder: '如：品牌、产品、受众',
      contentPlaceholder: '资料内容、口径或限制',
      empty: '暂无资料',
      saveEmptyTitle: '资料标题不能为空',
      saveEmptyContent: '资料内容不能为空',
      created: '资料已新增',
      updated: '资料已更新',
      deleted: '资料已删除',
      saveFailed: '保存资料失败',
      deleteFailed: '删除资料失败',
    }
  }
  return {
    title: '设定资料',
    entryName: '设定',
    addLabel: '+ 新增设定',
    titlePlaceholder: '设定标题',
    categoryPlaceholder: '如：地理、历史、魔法',
    contentPlaceholder: '设定内容、规则或限制',
    empty: '暂无设定条目',
    saveEmptyTitle: '设定标题不能为空',
    saveEmptyContent: '设定内容不能为空',
    created: '设定已新增',
    updated: '设定已更新',
    deleted: '设定已删除',
    saveFailed: '保存设定失败',
    deleteFailed: '删除设定失败',
  }
})

const entries = computed(() => {
  const order: Record<WorldEntry['scope_type'], number> = { global: 0, chapter: 1 }
  return [...worldEntryStore.entriesForProject(props.projectId)]
    .sort((a, b) => order[a.scope_type] - order[b.scope_type] || a.title.localeCompare(b.title))
})

const scopeOptions = computed(() => {
  if (props.projectMode === 'article') {
    return [
      { value: 'global', label: '项目资料' },
      { value: 'chapter', label: '局部资料' },
    ] as const
  }
  return [
    { value: 'global', label: '全局设定' },
    { value: 'chapter', label: '章节设定' },
  ] as const
})

const confidenceOptions = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
] as const

const confidenceLabel = (level: string) =>
  ({ low: '低', medium: '中', high: '高' } as Record<string, string>)[level] ?? level

const scopeLabel = (scope: WorldEntry['scope_type']) =>
  scopeOptions.value.find(option => option.value === scope)?.label ?? scope

const showForm = ref(false)
const editingEntryId = ref<string | null>(null)
const savingEntry = ref(false)

const defaultForm = (): EntryForm => ({
  title: '',
  category: '',
  scope_type: 'global',
  content: '',
  confidence: 'medium',
})

const entryForm = ref<EntryForm>(defaultForm())

function openNewEntryForm() {
  editingEntryId.value = null
  entryForm.value = defaultForm()
  showForm.value = true
}

function editEntry(entry: WorldEntry) {
  editingEntryId.value = entry.id
  entryForm.value = {
    title: entry.title,
    category: entry.category ?? '',
    scope_type: entry.scope_type,
    content: entry.content,
    confidence: entry.confidence,
  }
  showForm.value = true
}

function cancelEntryForm() {
  showForm.value = false
  editingEntryId.value = null
}

async function saveEntry() {
  if (!entryForm.value.title.trim()) {
    ui.showToast(labels.value.saveEmptyTitle, 'error')
    return
  }
  if (!entryForm.value.content.trim()) {
    ui.showToast(labels.value.saveEmptyContent, 'error')
    return
  }

  savingEntry.value = true
  try {
    const payload = {
      title: entryForm.value.title.trim(),
      category: entryForm.value.category.trim(),
      scope_type: entryForm.value.scope_type,
      content: entryForm.value.content.trim(),
      confidence: entryForm.value.confidence,
    }

    if (editingEntryId.value) {
      await worldEntryStore.updateWorldEntry(props.projectId, editingEntryId.value, payload)
      ui.showToast(labels.value.updated, 'success')
    } else {
      await worldEntryStore.addWorldEntry(props.projectId, payload)
      ui.showToast(labels.value.created, 'success')
    }
    cancelEntryForm()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, labels.value.saveFailed), 'error')
  } finally {
    savingEntry.value = false
  }
}

async function deleteEntry(entry: WorldEntry) {
  if (!window.confirm(`确定删除${labels.value.entryName}「${entry.title}」吗？`)) return
  try {
    await worldEntryStore.removeWorldEntry(props.projectId, entry.id)
    ui.showToast(labels.value.deleted, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, labels.value.deleteFailed), 'error')
  }
}
</script>

<template>
  <div class="project-world-library">
    <header class="library-header">
      <div class="library-title-block">
        <h2 class="library-title">{{ labels.title }}</h2>
        <span class="library-count">{{ entries.length }} 条</span>
      </div>
      <button class="btn-close" @click="emit('close')">关闭</button>
    </header>

    <main class="library-body">
      <section class="library-section">
        <div class="section-head">
          <h3>{{ labels.entryName }}列表</h3>
          <button class="btn-add-inline" @click="openNewEntryForm">{{ labels.addLabel }}</button>
        </div>

        <div v-if="showForm" class="inline-form">
          <div class="form-grid">
            <div class="form-row">
              <label>标题 <span class="required">*</span></label>
              <input v-model="entryForm.title" class="form-input" type="text" :placeholder="labels.titlePlaceholder" />
            </div>
            <div class="form-row">
              <label>分类</label>
              <input v-model="entryForm.category" class="form-input" type="text" :placeholder="labels.categoryPlaceholder" />
            </div>
            <div class="form-row">
              <label>层级</label>
              <select v-model="entryForm.scope_type" class="form-input">
                <option v-for="option in scopeOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
            </div>
            <div class="form-row">
              <label>可信度</label>
              <select v-model="entryForm.confidence" class="form-input">
                <option v-for="option in confidenceOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
            </div>
          </div>
          <div class="form-row">
            <label>内容 <span class="required">*</span></label>
            <textarea v-model="entryForm.content" class="form-textarea" rows="6" :placeholder="labels.contentPlaceholder"></textarea>
          </div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="savingEntry" @click="saveEntry">{{ savingEntry ? '保存中...' : '保存' }}</button>
            <button class="btn-cancel" @click="cancelEntryForm">取消</button>
          </div>
        </div>

        <div v-if="entries.length" class="entry-grid">
          <article v-for="entry in entries" :key="entry.id" class="entry-card" :class="entry.scope_type">
            <div class="entry-head">
              <span class="entry-title">{{ entry.title }}</span>
              <span class="badge badge-scope" :class="entry.scope_type">{{ scopeLabel(entry.scope_type) }}</span>
              <span class="badge badge-confidence" :class="entry.confidence">{{ confidenceLabel(entry.confidence) }}</span>
              <span class="entry-actions">
                <button class="mini-btn" title="编辑" @click="editEntry(entry)">编辑</button>
                <button class="mini-btn danger" title="删除" @click="deleteEntry(entry)">删除</button>
              </span>
            </div>
            <div v-if="entry.category" class="entry-meta">{{ entry.category }}</div>
            <p class="entry-content">{{ entry.content }}</p>
          </article>
        </div>
        <div v-else class="empty-hint">{{ labels.empty }}</div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.project-world-library {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.library-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  background: var(--bg-panel);
}
.library-title-block {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  min-width: 0;
}
.library-title {
  margin: 0;
  color: var(--text);
  font-size: var(--text-lg);
  font-weight: 700;
}
.library-count {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  white-space: nowrap;
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
.library-body {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--sp-4) var(--sp-5);
}
.library-section {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: var(--sp-4);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.section-head h3 {
  margin: 0;
  color: var(--text);
  font-size: var(--text-base);
  font-weight: 600;
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
  color: var(--text-secondary);
  font-size: var(--text-xs);
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
.entry-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--sp-3);
}
.entry-card {
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
}
.entry-card.global {
  border-left: 3px solid var(--accent);
}
.entry-card.chapter {
  border-left: 3px solid var(--status-reviewing);
}
.entry-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.entry-title {
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 600;
}
.entry-actions {
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
.entry-meta {
  margin-top: 4px;
  color: var(--text-tertiary);
  font-size: 11px;
}
.entry-content {
  margin: 6px 0 0;
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.55;
  white-space: pre-wrap;
}
.badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 6px;
  white-space: nowrap;
}
.badge-scope.global {
  background: var(--accent-subtle);
  color: var(--accent);
}
.badge-scope.chapter {
  background: var(--status-reviewing-bg);
  color: var(--status-reviewing);
}
.badge-confidence.high {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.badge-confidence.medium {
  background: var(--accent-subtle);
  color: var(--accent);
}
.badge-confidence.low {
  background: var(--status-error-bg);
  color: var(--status-error);
}
.empty-hint {
  padding: var(--sp-5);
  border: 1px dashed var(--border);
  border-radius: 10px;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  text-align: center;
}

@media (max-width: 768px) {
  .form-grid,
  .entry-grid {
    grid-template-columns: 1fr;
  }
  .section-head,
  .library-title-block {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
