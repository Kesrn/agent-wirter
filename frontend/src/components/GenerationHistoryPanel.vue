<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useGenerationHistoryStore } from '../stores'
import { api } from '../api/client'
import type { ApiRunContext, ApiRunContextCall, GenerationRecord, ProjectMode } from '../api/types'
import SkillPackDetail from './SkillPackDetail.vue'

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
const contextSnapshot = ref<ApiRunContext | null>(null)
const contextLoading = ref(false)
const contextError = ref('')

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
  const refreshed = store.records.find(item => item.id === record.id)
  if (content !== null || (refreshed?.skillPacks.length ?? 0) > 0) expandedId.value = record.id
}

async function handleApply(record: GenerationRecord) {
  const confirmed = window.confirm('确定将这次 AI 生成内容应用到编辑器吗？当前草稿会被替换。')
  if (!confirmed) return
  const content = await contentFor(record)
  if (content) emit('apply', record.id, content)
}

function formatJson(value: Record<string, unknown> | null): string {
  if (!value) return ''
  return JSON.stringify(value, null, 2)
}

function callLabel(call: ApiRunContextCall): string {
  return call.step_name || call.agent_name || 'LLM 调用'
}

function callMeta(call: ApiRunContextCall): string {
  const parts = [call.agent_name, call.provider, call.model].filter(Boolean)
  return parts.join(' · ') || '无模型信息'
}

async function openContextSnapshot(record: GenerationRecord) {
  if (!record.runId) {
    contextSnapshot.value = null
    contextError.value = '这条旧记录没有关联 run，无法查看上下文快照。'
    return
  }
  contextLoading.value = true
  contextError.value = ''
  contextSnapshot.value = null
  try {
    contextSnapshot.value = await api.getRunContext(record.runId)
  } catch (e: unknown) {
    contextError.value = e instanceof Error ? e.message : '上下文快照加载失败'
  } finally {
    contextLoading.value = false
  }
}

function closeContextSnapshot() {
  contextSnapshot.value = null
  contextError.value = ''
  contextLoading.value = false
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
          <button class="btn-action" :disabled="!record.runId" @click="openContextSnapshot(record)">
            上下文
          </button>
          <button class="btn-action" @click="emit('compare-current', record.id)">与当前对比</button>
          <button class="btn-action btn-apply" @click="handleApply(record)">应用</button>
        </div>
        <div v-if="expandedId === record.id" class="generation-expanded">
          <div v-if="record.skillPacks.length" class="generation-skill-packs">
            <SkillPackDetail
              v-for="pack in record.skillPacks"
              :key="`${pack.expert}:${pack.skill_dir}`"
              :pack="pack"
            />
          </div>
          <pre class="generation-preview">{{ previewContent[record.id] }}</pre>
        </div>
      </li>
    </ul>

    <div v-if="contextLoading || contextError || contextSnapshot" class="context-modal-overlay">
      <div class="context-modal">
        <div class="context-modal-header">
          <div>
            <h3>生成上下文快照</h3>
            <p v-if="contextSnapshot">Run {{ contextSnapshot.run_id.slice(0, 8) }} · {{ contextSnapshot.workflow_key || contextSnapshot.mode }}</p>
          </div>
          <button class="context-close" type="button" @click="closeContextSnapshot">关闭</button>
        </div>

        <div class="context-modal-body">
          <div v-if="contextLoading" class="context-state">
            <span class="spinner" />
            <span>加载上下文...</span>
          </div>
          <div v-else-if="contextError" class="context-state context-error-text">
            {{ contextError }}
          </div>
          <div v-else-if="contextSnapshot && contextSnapshot.calls.length === 0" class="context-state">
            这次生成没有记录到 LLM 上下文快照
          </div>
          <div v-else-if="contextSnapshot" class="context-call-list">
            <section v-for="call in contextSnapshot.calls" :key="call.id" class="context-call">
              <div class="context-call-header">
                <strong>{{ callLabel(call) }}</strong>
                <span>{{ callMeta(call) }}</span>
              </div>
              <pre v-if="call.context_text" class="context-text">{{ call.context_text }}</pre>
              <div v-else class="context-empty-text">
                未抽取到标准上下文段，可展开 Prompt 快照查看原始记录。
              </div>

              <details v-if="call.context_snapshot" class="context-details">
                <summary>上下文摘要</summary>
                <pre>{{ formatJson(call.context_snapshot) }}</pre>
              </details>

              <details v-if="call.prompt_snapshot" class="context-details">
                <summary>Prompt 快照{{ call.prompt_truncated ? '（已截断）' : '' }}</summary>
                <pre>{{ call.prompt_snapshot }}</pre>
              </details>
            </section>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.generation-panel {
  flex: 0 0 clamp(340px, 28vw, 440px);
  width: clamp(340px, 28vw, 440px);
  max-width: 45vw;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
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
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  flex: 1;
  min-height: 0;
}
.generation-item {
  min-width: 0;
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
  overflow-wrap: anywhere;
}
.generation-time {
  margin-bottom: var(--sp-2);
}
.generation-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  min-width: 0;
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
.btn-action:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.btn-action:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--accent);
}
.btn-action:disabled:hover {
  background: var(--bg);
  border-color: var(--border);
  color: var(--text-secondary);
}
.btn-apply:hover {
  color: var(--status-reviewing);
}
.generation-expanded {
  margin: var(--sp-3) 0 0;
}

.generation-skill-packs {
  display: grid;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}

.generation-preview {
  margin: 0;
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

.context-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-5);
  background: rgba(0, 0, 0, 0.55);
}
.context-modal {
  width: min(960px, calc(100vw - 48px));
  max-height: min(820px, calc(100vh - 48px));
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.context-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-4);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.context-modal-header h3 {
  margin: 0;
  font-size: var(--text-lg);
  color: var(--text);
}
.context-modal-header p {
  margin: var(--sp-1) 0 0;
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}
.context-close {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-secondary);
  padding: var(--sp-2) var(--sp-3);
  cursor: pointer;
}
.context-close:hover {
  border-color: var(--border-focus);
  color: var(--accent);
}
.context-modal-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: var(--sp-4);
}
.context-state {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}
.context-error-text {
  color: var(--status-reviewing);
}
.context-call-list {
  display: grid;
  gap: var(--sp-3);
}
.context-call {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  overflow: hidden;
}
.context-call-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border-bottom: 1px solid var(--border);
}
.context-call-header strong {
  color: var(--text);
  font-size: var(--text-sm);
}
.context-call-header span {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}
.context-text,
.context-details pre {
  margin: 0;
  padding: var(--sp-3);
  white-space: pre-wrap;
  color: var(--text);
  font-size: var(--text-xs);
  line-height: 1.7;
}
.context-text {
  max-height: 360px;
  overflow-y: auto;
  border-bottom: 1px solid var(--border);
}
.context-empty-text {
  padding: var(--sp-3);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  border-bottom: 1px solid var(--border);
}
.context-details {
  border-top: 1px solid var(--border);
}
.context-details:first-of-type {
  border-top: 0;
}
.context-details summary {
  cursor: pointer;
  padding: var(--sp-2) var(--sp-3);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  background: var(--bg-hover);
}
.context-details pre {
  max-height: 300px;
  overflow-y: auto;
  background: color-mix(in srgb, var(--bg) 86%, black);
}

@media (max-width: 760px) {
  .generation-panel {
    flex: 0 0 100%;
    width: 100%;
    max-width: none;
    border-left: 0;
  }
}
</style>
