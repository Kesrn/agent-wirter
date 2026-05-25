<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useGenerationHistoryStore } from '../stores'
import type { GenerationRecord, ProjectMode } from '../api/types'

const props = defineProps<{
  projectId: string
  mode: ProjectMode
  sequenceNumber?: number
  documentId?: string
}>()

const emit = defineEmits<{
  'compare-current': [recordId: string]
  apply: [recordId: string, content: string]
}>()

const store = useGenerationHistoryStore()
const expandedId = ref<string | null>(null)
const previewContent = ref<Record<string, string>>({})

const sortedRecords = computed(() =>
  [...store.records].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()),
)

const MODE_MAP: Record<string, string> = {
  full_pipeline: '完整生成',
  enhance: '改写优化',
  continue: '继续生成',
  summarize: '反馈分析',
}

const STATUS_MAP: Record<string, { label: string; cls: string }> = {
  candidate: { label: '候选', cls: 'status-candidate' },
  applied: { label: '已应用', cls: 'status-applied' },
  discarded: { label: '已丢弃', cls: 'status-discarded' },
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function modeLabel(mode: string): string {
  return MODE_MAP[mode] ?? mode
}

function statusInfo(status: string) {
  return STATUS_MAP[status] ?? { label: status, cls: 'status-candidate' }
}

async function loadRecords() {
  if (props.mode === 'article') {
    if (!props.documentId) return
    await store.loadDocumentRecords(props.projectId, props.documentId)
  } else {
    if (!props.sequenceNumber) return
    await store.loadChapterRecords(props.projectId, props.sequenceNumber)
  }
}

async function contentFor(record: GenerationRecord): Promise<string | null> {
  if (previewContent.value[record.id]) return previewContent.value[record.id]
  const content = record.content ?? await store.getRecordContent(props.projectId, record.id)
  if (content) previewContent.value[record.id] = content
  return content
}

async function togglePreview(record: GenerationRecord) {
  if (expandedId.value === record.id) {
    expandedId.value = null
    return
  }
  const content = await contentFor(record)
  if (content) expandedId.value = record.id
}

async function handleApply(record: GenerationRecord) {
  const confirmed = window.confirm('确定将这次 AI 生成内容应用到编辑器吗？当前草稿会被替换。')
  if (!confirmed) return
  const content = await contentFor(record)
  if (content) emit('apply', record.id, content)
}

watch(
  () => [props.mode, props.sequenceNumber, props.documentId] as const,
  () => {
    expandedId.value = null
    previewContent.value = {}
    loadRecords()
  },
  { immediate: true },
)
</script>

<template>
  <div class="generation-panel">
    <div class="generation-panel-header">
      <span class="panel-title">AI生成历史</span>
    </div>

    <div v-if="store.loading" class="generation-loading">
      <span class="spinner" />
      <span>加载中...</span>
    </div>

    <div v-else-if="store.loadError" class="generation-error">
      {{ store.loadError }}
    </div>

    <div v-else-if="sortedRecords.length === 0" class="generation-empty">
      暂无 AI 生成记录
    </div>

    <ul v-else class="generation-list">
      <li v-for="record in sortedRecords" :key="record.id" class="generation-item">
        <div class="generation-main">
          <span class="generation-mode">{{ modeLabel(record.mode) }}</span>
          <span class="generation-status" :class="statusInfo(record.status).cls">
            {{ statusInfo(record.status).label }}
          </span>
          <span class="generation-words">{{ record.wordCount }}字</span>
        </div>
        <div v-if="record.direction" class="generation-direction">{{ record.direction }}</div>
        <div class="generation-time">{{ formatTime(record.createdAt) }}</div>
        <div class="generation-actions">
          <button class="btn-action" @click="togglePreview(record)">
            {{ expandedId === record.id ? '收起' : '查看' }}
          </button>
          <button class="btn-action" @click="emit('compare-current', record.id)">与当前对比</button>
          <button class="btn-action btn-apply" @click="handleApply(record)">应用</button>
        </div>
        <pre v-if="expandedId === record.id" class="generation-preview">{{ previewContent[record.id] }}</pre>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.generation-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-panel);
  border-left: 1px solid var(--border);
  overflow: hidden;
}
.generation-panel-header {
  display: flex;
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.panel-title {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--text);
}
.generation-loading,
.generation-error,
.generation-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-8);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  gap: var(--sp-2);
}
.generation-error {
  color: var(--status-reviewing);
}
.spinner {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.generation-list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
}
.generation-item {
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
}
.generation-item:hover {
  background: var(--bg-hover);
}
.generation-main {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}
.generation-mode {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--text);
}
.generation-status {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 6px;
}
.status-candidate { background: #e8f0fe; color: #1a73e8; }
.status-applied { background: #e6f4ea; color: #1e8e3e; }
.status-discarded { background: #f1f3f4; color: #5f6368; }
.generation-words,
.generation-time,
.generation-direction {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.generation-direction {
  margin-bottom: var(--sp-1);
  line-height: 1.5;
}
.generation-time {
  margin-bottom: var(--sp-2);
}
.generation-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.btn-action {
  padding: 2px 8px;
  font-size: 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--transition);
}
.btn-action:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--accent);
}
.btn-apply:hover {
  color: var(--status-reviewing);
}
.generation-preview {
  margin: var(--sp-3) 0 0;
  padding: var(--sp-3);
  max-height: 260px;
  overflow-y: auto;
  white-space: pre-wrap;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font-size: var(--text-xs);
  line-height: 1.7;
}
</style>
