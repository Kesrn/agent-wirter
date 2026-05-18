<script setup lang="ts">
import { useExpertStore } from '../stores'

const store = useExpertStore()

const WORKFLOW_NODES = [
  { id: 'context', label: '上下文加载' },
  { id: 'writer', label: '创作' },
  { id: 'critic', label: '审校' },
  { id: 'consistency', label: '一致性检查' },
  { id: 'review', label: '人工审批' },
]

function nodeStatus(id: string): 'pending' | 'running' | 'success' | 'error' {
  const steps = store.workflowSteps
  if (!steps.length) return 'pending'
  return steps.find(s => s.id === id)?.status ?? 'pending'
}

function connectorStatus(index: number): 'pending' | 'done' {
  const cur = nodeStatus(WORKFLOW_NODES[index].id)
  const next = nodeStatus(WORKFLOW_NODES[index + 1].id)
  if (cur === 'success' || cur === 'running' || next !== 'pending') return 'done'
  return 'pending'
}
</script>

<template>
  <div v-if="store.workflowSteps.length" class="wf-container">
    <div v-for="(node, i) in WORKFLOW_NODES" :key="node.id" class="wf-step">
      <div class="wf-row" :class="nodeStatus(node.id)">
        <!-- Indicator -->
        <div class="wf-indicator">
          <span v-if="nodeStatus(node.id) === 'success'" class="wf-check">✓</span>
          <span v-else-if="nodeStatus(node.id) === 'running'" class="wf-spinner"></span>
          <span v-else-if="nodeStatus(node.id) === 'error'" class="wf-dot error"></span>
          <span v-else class="wf-dot"></span>
        </div>
        <!-- Label -->
        <span class="wf-label">{{ node.label }}</span>
        <!-- Running hint -->
        <span v-if="nodeStatus(node.id) === 'running'" class="wf-hint">进行中</span>
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
