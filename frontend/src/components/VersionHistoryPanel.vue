<script setup lang="ts">
import { watch, computed } from 'vue'
import { useChapterVersionStore } from '../stores'
import type { ChapterVersion } from '../api/types'

const props = defineProps<{
  projectId: string
  chapterId: string
  sequenceNumber: number
}>()

const emit = defineEmits<{
  'compare-current': [versionId: string, versionNumber: number]
  restore: [versionId: string, content: string]
}>()

const store = useChapterVersionStore()

const SOURCE_MAP: Record<string, { label: string; cls: string }> = {
  manual: { label: '手动', cls: 'source-manual' },
  ai_generate: { label: 'AI生成', cls: 'source-ai' },
  ai_enhance: { label: '增强', cls: 'source-enhance' },
  ai_continue: { label: '续写', cls: 'source-continue' },
}

const sortedVersions = computed(() =>
  [...store.versions].sort((a, b) => b.versionNumber - a.versionNumber),
)

function sourceInfo(source: string) {
  return SOURCE_MAP[source] ?? { label: source, cls: 'source-other' }
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function handleRestore(v: ChapterVersion) {
  const confirmed = window.confirm(`确定恢复到 v${v.versionNumber} 吗？当前内容将被替换。`)
  if (!confirmed) return
  const content = v.content ?? await store.getVersionContent(props.projectId, props.sequenceNumber, v.id)
  if (content !== null) {
    emit('restore', v.id, content)
  }
}

watch(
  () => [props.sequenceNumber, props.chapterId] as const,
  () => {
    store.loadVersions(props.projectId, props.sequenceNumber)
  },
  { immediate: true },
)
</script>

<template>
  <div class="version-panel">
    <div class="version-panel-header">
      <span class="panel-title">版本历史</span>
    </div>

    <div v-if="store.loading" class="version-loading">
      <span class="spinner" />
      <span>加载中...</span>
    </div>

    <div v-else-if="store.loadError" class="version-error">
      {{ store.loadError }}
    </div>

    <div v-else-if="sortedVersions.length === 0" class="version-empty">
      暂无版本记录
    </div>

    <ul v-else class="version-list">
      <li
        v-for="v in sortedVersions"
        :key="v.id"
        class="version-item"
      >
        <div class="version-main">
          <span class="version-num">v{{ v.versionNumber }}</span>
          <span class="version-source" :class="sourceInfo(v.source).cls">
            {{ sourceInfo(v.source).label }}
          </span>
          <span class="version-words">{{ v.wordCount }}字</span>
        </div>
        <div class="version-meta">
          <span class="version-time">{{ formatTime(v.createdAt) }}</span>
        </div>
        <div class="version-actions">
          <button class="btn-action btn-compare" @click="emit('compare-current', v.id, v.versionNumber)">
            与当前对比
          </button>
          <button class="btn-action btn-restore" @click="handleRestore(v)">恢复</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.version-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-panel);
  border-left: 1px solid var(--border);
  overflow: hidden;
}
.version-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.panel-title {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--text);
}
.compare-hint {
  font-size: var(--text-xs);
  color: var(--accent);
  cursor: pointer;
}
.compare-hint:hover {
  text-decoration: underline;
}

.version-loading,
.version-error,
.version-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-8);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  gap: var(--sp-2);
}
.version-error {
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

.version-list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
}
.version-item {
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  transition: background var(--transition);
}
.version-item:hover {
  background: var(--bg-hover);
}
.version-item.compare-selected {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-left: 3px solid var(--accent);
}

.version-main {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}
.version-num {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--text);
}
.version-source {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 6px;
}
.source-manual { background: #e8f0fe; color: #1a73e8; }
.source-ai { background: #fce8e6; color: #d93025; }
.source-enhance { background: #e6f4ea; color: #1e8e3e; }
.source-continue { background: #fef7e0; color: #b06000; }
.source-other { background: #f1f3f4; color: #5f6368; }

.version-words {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
.version-meta {
  margin-bottom: var(--sp-2);
}
.version-time {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.version-actions {
  display: flex;
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
}
.btn-compare:hover { color: var(--accent); }
.btn-restore:hover { color: var(--status-reviewing); }
</style>
