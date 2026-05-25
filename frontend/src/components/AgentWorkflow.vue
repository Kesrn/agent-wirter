<script setup lang="ts">
import { computed } from 'vue'
import { useExpertStore } from '../stores'
import type { ProjectMode } from '../api/types'

const props = defineProps<{ projectId: string; mode: ProjectMode }>()

const store = useExpertStore()
const projectState = computed(() => store.getState(props.projectId))
const isNovel = computed(() => props.mode === 'novel')

const NOVEL_NODES = [
  { id: 'context', label: '上下文加载' },
  { id: 'writer', label: '创作' },
  { id: 'critic', label: '审校' },
  { id: 'consistency', label: '一致性检查' },
  { id: 'review', label: '人工审批' },
]

const ARTICLE_NODES = [
  { id: 'writer', label: '内容生成' },
  { id: 'critic', label: '结构检查' },
  { id: 'consistency', label: '受众匹配' },
  { id: 'platform', label: '平台/CTA 检查' },
  { id: 'review', label: '风险检查' },
]

const WORKFLOW_NODES = computed(() => isNovel.value ? NOVEL_NODES : ARTICLE_NODES)

const stepSkillMap: Record<string, string> = {
  writer: 'writer',
  critic: 'critic',
  consistency: 'consistency_checker',
  platform: 'platform_review',
}

const progressLabel = computed(() =>
  projectState.value.revisionCount > 0
    ? `第 ${projectState.value.revisionCount}/${projectState.value.maxRevisions} 次修改`
    : isNovel.value ? '生成进度' : '内容生成进度'
)

function nodeStatus(id: string): 'pending' | 'running' | 'success' | 'error' | 'cancelled' {
  const steps = projectState.value.workflowSteps
  if (!steps.length) return 'pending'
  return steps.find(s => s.id === id)?.status ?? 'pending'
}

function connectorStatus(index: number): 'pending' | 'done' {
  const nodes = WORKFLOW_NODES.value
  const cur = nodeStatus(nodes[index].id)
  const next = nodeStatus(nodes[index + 1].id)
  if (cur === 'success' || cur === 'running' || next !== 'pending') return 'done'
  return 'pending'
}

const progressPercent = computed(() => {
  const nodes = WORKFLOW_NODES.value
  const steps = nodes.map(n => nodeStatus(n.id))
  const completed = steps.filter(s => s === 'success').length
  const running = steps.some(s => s === 'running') ? 0.5 : 0
  return Math.min(100, Math.round(((completed + running) / nodes.length) * 100))
})
</script>

<template>
  <div v-if="projectState.workflowSteps.length" class="wf-container">
    <div class="wf-progress-head">
      <span>{{ progressLabel }}</span>
      <span>{{ progressPercent }}%</span>
    </div>
    <div class="wf-progress-track">
      <div class="wf-progress-bar" :style="{ width: `${progressPercent}%` }"></div>
    </div>
    <div v-for="(node, i) in WORKFLOW_NODES" :key="node.id" class="wf-step">
      <div class="wf-row" :class="nodeStatus(node.id)">
        <!-- Indicator -->
        <div class="wf-indicator">
          <span v-if="nodeStatus(node.id) === 'success'" class="wf-check">✓</span>
          <span v-else-if="nodeStatus(node.id) === 'running'" class="wf-spinner"></span>
          <span v-else-if="nodeStatus(node.id) === 'error'" class="wf-dot error"></span>
          <span v-else-if="nodeStatus(node.id) === 'cancelled'" class="wf-cancel">✕</span>
          <span v-else class="wf-dot"></span>
        </div>
        <!-- Label -->
        <span class="wf-label">{{ node.label }}</span>
        <!-- Skill name -->
        <span v-if="projectState.expertSkills[stepSkillMap[node.id]]" class="wf-skill">{{ projectState.expertSkills[stepSkillMap[node.id]] }}</span>
        <!-- Running hint -->
        <span v-if="nodeStatus(node.id) === 'running'" class="wf-hint">进行中</span>
        <span v-else-if="nodeStatus(node.id) === 'cancelled'" class="wf-hint cancelled">已取消</span>
      </div>
      <!-- Vertical connector -->
      <div v-if="i < WORKFLOW_NODES.length - 1" class="wf-connector" :class="connectorStatus(i)"></div>
    </div>
  </div>
</template>

<style scoped>
.wf-container {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.wf-progress-head {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-2);
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: var(--sp-2);
}

.wf-progress-track {
  height: 6px;
  overflow: hidden;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 999px;
  margin-bottom: var(--sp-3);
}

.wf-progress-bar {
  height: 100%;
  background: var(--accent);
  border-radius: inherit;
  transition: width 180ms ease;
}

.wf-step {
  display: flex;
  flex-direction: column;
}

.wf-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
}

/* Indicator */
.wf-indicator {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.wf-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-tertiary);
  opacity: 0.3;
}

.wf-dot.error {
  background: #ef4444;
  opacity: 1;
}

.wf-check {
  font-size: 13px;
  font-weight: 700;
  color: #22c55e;
  line-height: 1;
}

.wf-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* Label */
.wf-label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-tertiary);
  transition: color 0.3s ease;
}

.wf-hint {
  font-size: 10px;
  color: var(--accent);
  font-weight: 500;
  margin-left: auto;
}

.wf-hint.cancelled {
  color: var(--text-tertiary);
  opacity: 0.5;
}

.wf-skill {
  font-size: 10px;
  color: var(--text-tertiary);
  opacity: 0.6;
  margin-left: var(--sp-1);
}

/* Row states */
.wf-row.running .wf-label {
  color: var(--accent);
  font-weight: 600;
}

.wf-row.success .wf-label {
  color: #22c55e;
}

.wf-row.error .wf-label {
  color: #ef4444;
}

.wf-row.cancelled .wf-label {
  color: var(--text-tertiary);
  text-decoration: line-through;
  opacity: 0.5;
}

.wf-cancel {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-tertiary);
  opacity: 0.5;
  line-height: 1;
}

/* Vertical connector */
.wf-connector {
  width: 2px;
  height: 12px;
  margin-left: 9px; /* center under indicator (20/2 - 2/2 + 1) */
  background: var(--border);
  opacity: 0.3;
  border-radius: 1px;
  transition: background 0.3s ease, opacity 0.3s ease;
}

.wf-connector.done {
  background: #22c55e;
  opacity: 0.5;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
