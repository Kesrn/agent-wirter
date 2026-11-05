<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, ApiError } from '../api/client'
import type { RunTraceResponse, RunTraceStep } from '../api/types'
import {
  formatCost,
  formatDurationMs,
  formatQualityRuleCode,
} from '../utils/runTrace'

const props = defineProps<{
  runId: string
}>()

const emit = defineEmits<{
  close: []
}>()

const trace = ref<RunTraceResponse | null>(null)
const loading = ref(false)
const error = ref('')
const is404 = ref(false)

const includePrompts = ref(false)
const showPromptConfirm = ref(false)
type CopyStatus = 'idle' | 'copied' | 'failed'
const copyStatus = ref<CopyStatus>('idle')
let copyTimeout: ReturnType<typeof setTimeout> | null = null
const expandedSteps = ref<Set<number>>(new Set())

const closeBtnRef = ref<HTMLButtonElement | null>(null)
let previousFocusedElement: HTMLElement | null = null
let latestRequestId = 0

async function loadTrace(withPrompts: boolean, rollbackPromptState?: boolean) {
  if (!props.runId) return
  const currentRequestId = ++latestRequestId
  loading.value = true
  error.value = ''
  is404.value = false
  try {
    const data = await api.getRunTrace(props.runId, { includePrompts: withPrompts })
    if (currentRequestId !== latestRequestId) return
    trace.value = data
    if (data.failed_step) {
      expandedSteps.value.add(data.failed_step.step_order)
    }
  } catch (e: unknown) {
    if (currentRequestId !== latestRequestId) return
    if (rollbackPromptState !== undefined) {
      includePrompts.value = rollbackPromptState
    }
    if (e instanceof ApiError && e.status === 404) {
      is404.value = true
      error.value = '找不到这次运行的追踪'
    } else if (e instanceof Error) {
      error.value = e.message
    } else {
      error.value = '加载追踪数据失败'
    }
  } finally {
    if (currentRequestId === latestRequestId) {
      loading.value = false
    }
  }
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

function onOverlayClick(event: MouseEvent) {
  if (event.target === event.currentTarget) {
    emit('close')
  }
}

function toggleStep(stepOrder: number) {
  if (expandedSteps.value.has(stepOrder)) {
    expandedSteps.value.delete(stepOrder)
  } else {
    expandedSteps.value.add(stepOrder)
  }
}

function handlePromptToggleClick() {
  if (includePrompts.value) {
    const previous = includePrompts.value
    includePrompts.value = false
    loadTrace(false, previous)
  } else {
    showPromptConfirm.value = true
  }
}

function confirmIncludePrompts() {
  showPromptConfirm.value = false
  const previous = includePrompts.value
  includePrompts.value = true
  loadTrace(true, previous)
}

function cancelIncludePrompts() {
  showPromptConfirm.value = false
}

async function copyRequestId() {
  if (!trace.value?.run.request_id) return
  if (copyTimeout) clearTimeout(copyTimeout)
  try {
    if (!navigator?.clipboard?.writeText) {
      throw new Error('Clipboard API unavailable')
    }
    await navigator.clipboard.writeText(trace.value.run.request_id)
    copyStatus.value = 'copied'
  } catch {
    copyStatus.value = 'failed'
  }
  copyTimeout = setTimeout(() => {
    copyStatus.value = 'idle'
  }, 2500)
}

const totalDurationMs = computed(() => {
  if (trace.value?.run.duration_ms != null && trace.value.run.duration_ms > 0) {
    return trace.value.run.duration_ms
  }
  const sum = (trace.value?.steps || []).reduce((acc, s) => acc + (s.duration_ms || 0), 0)
  return sum > 0 ? sum : 0
})

function getStepDurationWidth(stepDuration: number | null | undefined): string {
  if (!stepDuration || stepDuration <= 0) return '6%'
  const total = totalDurationMs.value
  if (!total || total <= 0) return '10%'
  const pct = Math.max(6, Math.min(100, Math.round((stepDuration / total) * 100)))
  return `${pct}%`
}

function getStepTokensTotal(step: RunTraceStep): number {
  if (!step.llm_calls || !step.llm_calls.length) return 0
  return step.llm_calls.reduce((sum, call) => {
    return sum + (call.input_tokens || 0) + (call.output_tokens || 0)
  }, 0)
}

function isStepFailed(step: RunTraceStep): boolean {
  if (trace.value?.failed_step?.step_order === step.step_order) return true
  return step.status === 'FAILED'
}

watch(() => props.runId, () => {
  includePrompts.value = false
  showPromptConfirm.value = false
  expandedSteps.value.clear()
  loadTrace(false)
})

onMounted(() => {
  previousFocusedElement = (document.activeElement as HTMLElement) || null
  window.addEventListener('keydown', handleKeydown)
  loadTrace(false)
  nextTick(() => {
    closeBtnRef.value?.focus()
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
  previousFocusedElement?.focus()
})
</script>

<template>
  <div class="run-trace-overlay" @click="onOverlayClick">
    <div
      class="run-trace-drawer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="run-trace-title"
    >
      <!-- Drawer Header -->
      <div class="trace-header">
        <div class="trace-header-title-row">
          <h2 id="run-trace-title" class="trace-title">运行追踪</h2>
          <button
            ref="closeBtnRef"
            type="button"
            class="trace-close-btn"
            aria-label="关闭"
            @click="emit('close')"
          >
            &times;
          </button>
        </div>
        <p class="trace-subtitle">单次生成全流程执行耗时、Token 用量与节点状态</p>
      </div>

      <!-- Loading State -->
      <div v-if="loading && !trace" class="trace-state trace-loading">
        <span class="spinner" />
        <span>加载追踪数据中...</span>
      </div>

      <!-- Error State -->
      <div v-else-if="error && !trace" class="trace-state trace-error" role="alert">
        <span class="error-msg">{{ error }}</span>
        <button v-if="!is404" type="button" class="btn-retry" @click="loadTrace(includePrompts)">
          重试
        </button>
      </div>

      <!-- Loaded Content -->
      <div v-else-if="trace" class="trace-body">
        <!-- Reload Error Banner (visible when data already exists) -->
        <div v-if="error" class="trace-alert trace-alert-danger trace-reload-error" role="alert">
          <div class="reload-error-content">
            <span class="error-msg">{{ error }}</span>
            <button type="button" class="btn-retry" @click="loadTrace(includePrompts)">重试</button>
          </div>
        </div>

        <!-- Truncated Banner -->
        <div v-if="trace.truncated" class="trace-alert trace-alert-warning" role="alert">
          <span class="alert-icon">⚠️</span>
          <span>步骤或调用过多，已截断显示</span>
        </div>

        <!-- Failed Step Banner -->
        <div v-if="trace.failed_step" class="trace-alert trace-alert-danger" role="alert">
          <div class="failed-banner-content">
            <span class="failed-banner-title">
              失败节点：{{ trace.failed_step.step_name }}（{{ trace.failed_step.agent_name || '未知 agent' }}）
            </span>
            <span v-if="trace.failed_step.error_message" class="failed-banner-msg">
              ：{{ trace.failed_step.error_message }}
            </span>
          </div>
        </div>

        <!-- Overview Cards -->
        <div class="overview-section">
          <div class="overview-grid">
            <div class="overview-card">
              <span class="card-label">运行状态</span>
              <span class="card-value status-tag" :class="trace.run.status.toLowerCase()">
                {{ trace.run.status }}
              </span>
            </div>
            <div class="overview-card">
              <span class="card-label">总耗时</span>
              <span class="card-value">{{ formatDurationMs(trace.run.duration_ms) }}</span>
            </div>
            <div class="overview-card">
              <span class="card-label">LLM 调用次数</span>
              <span class="card-value">{{ trace.totals?.llm_calls ?? 0 }}</span>
            </div>
            <div class="overview-card">
              <span class="card-label">总 Token</span>
              <span class="card-value">
                {{ trace.totals?.total_tokens ?? 0 }}
                <span class="token-breakdown">
                  (入 {{ trace.totals?.input_tokens ?? 0 }} / 出 {{ trace.totals?.output_tokens ?? 0 }})
                </span>
              </span>
            </div>
            <div class="overview-card">
              <span class="card-label">成本</span>
              <span class="card-value">{{ formatCost(trace.totals?.cost) }}</span>
            </div>
            <div class="overview-card">
              <span class="card-label">重试总次数</span>
              <span class="card-value">{{ trace.totals?.retries ?? 0 }}</span>
            </div>
          </div>

          <!-- Request ID row -->
          <div class="request-id-row">
            <span class="request-id-label">请求号 (Request ID):</span>
            <code class="request-id-code">{{ trace.run.request_id || '-' }}</code>
            <button
              v-if="trace.run.request_id"
              type="button"
              class="btn-copy-req"
              @click="copyRequestId"
            >
              {{ copyStatus === 'copied' ? '已复制' : copyStatus === 'failed' ? '复制失败，请手动选择' : '复制' }}
            </button>
          </div>
        </div>

        <!-- Prompts Toggle Toolbar -->
        <div class="prompts-toolbar">
          <div class="prompts-toolbar-left">
            <span class="toolbar-title">步骤时间线</span>
            <span class="step-count">({{ trace.steps.length }} 步)</span>
          </div>
          <div class="prompts-toolbar-right">
            <button
              type="button"
              class="btn-toggle-prompts"
              :class="{ active: includePrompts }"
              :disabled="loading"
              @click="handlePromptToggleClick"
            >
              {{ includePrompts ? '关闭提示词' : '显示提示词' }}
            </button>
          </div>
        </div>

        <!-- Prompt confirmation prompt box -->
        <div v-if="showPromptConfirm" class="prompt-confirm-dialog" role="alert">
          <span class="confirm-text">提示词可能包含你的设定和正文，仅在本机查看</span>
          <div class="confirm-actions">
            <button type="button" class="btn-confirm-ok" @click="confirmIncludePrompts">确认</button>
            <button type="button" class="btn-confirm-cancel" @click="cancelIncludePrompts">取消</button>
          </div>
        </div>

        <!-- Empty Steps -->
        <div v-if="!trace.steps || trace.steps.length === 0" class="trace-state trace-empty">
          未记录任何步骤
        </div>

        <!-- Steps Timeline -->
        <div v-else class="steps-timeline">
          <div
            v-for="step in trace.steps"
            :key="step.step_order"
            class="timeline-step"
            :class="{
              'is-failed': isStepFailed(step),
              'is-expanded': expandedSteps.has(step.step_order),
            }"
          >
            <!-- Step Header Bar -->
            <div
              class="step-header"
              tabindex="0"
              role="button"
              :aria-expanded="expandedSteps.has(step.step_order)"
              @click="toggleStep(step.step_order)"
              @keydown.enter.prevent="toggleStep(step.step_order)"
              @keydown.space.prevent="toggleStep(step.step_order)"
            >
              <div class="step-header-left">
                <span class="step-order">#{{ step.step_order }}</span>
                <span class="step-name">{{ step.step_name }}</span>
                <span v-if="step.agent_name" class="step-agent">{{ step.agent_name }}</span>
                <span v-if="step.node_key && step.node_key !== step.step_name" class="step-node-key">
                  ({{ step.node_key }})
                </span>
                <span class="step-status-tag" :class="step.status.toLowerCase()">
                  {{ step.status }}
                </span>
              </div>

              <div class="step-header-right">
                <!-- Quality Gate Pill -->
                <div v-if="step.quality" class="step-quality-pill" :class="{ passed: step.quality.passed }">
                  <span class="quality-label">{{ step.quality.passed ? '质量门通过' : '质量门未通过' }}</span>
                  <span
                    v-for="code in (step.quality.error_codes || [])"
                    :key="code"
                    class="quality-code"
                  >
                    {{ formatQualityRuleCode(code) }}
                  </span>
                </div>

                <!-- Retries -->
                <span class="step-retries">
                  重试 {{ step.retry_count ?? 0 }}/{{ step.max_retry ?? 0 }}
                </span>

                <!-- Token sum -->
                <span class="step-tokens">
                  {{ getStepTokensTotal(step) }} tokens
                </span>

                <!-- Expand Indicator -->
                <span class="expand-arrow">
                  {{ expandedSteps.has(step.step_order) ? '▲' : '▼' }}
                </span>
              </div>
            </div>

            <!-- Duration bar -->
            <div class="step-duration-track">
              <div
                class="step-duration-bar"
                :class="step.status.toLowerCase()"
                :style="{ width: getStepDurationWidth(step.duration_ms) }"
              >
                <span class="duration-text">{{ formatDurationMs(step.duration_ms) }}</span>
              </div>
            </div>

            <!-- Step Error Message -->
            <div v-if="step.error_message" class="step-error-banner">
              {{ step.error_message }}
            </div>

            <!-- Expanded LLM Calls -->
            <div v-if="expandedSteps.has(step.step_order)" class="step-details">
              <div v-if="!step.llm_calls || step.llm_calls.length === 0" class="no-calls-msg">
                该步骤没有 LLM 调用记录
              </div>
              <div v-else class="llm-calls-list">
                <div
                  v-for="(call, idx) in step.llm_calls"
                  :key="call.id || idx"
                  class="llm-call-item"
                >
                  <div class="call-row-main">
                    <span class="call-index">调用 #{{ idx + 1 }}</span>
                    <span class="call-model">{{ call.model || '默认模型' }}</span>
                    <span class="call-latency">{{ formatDurationMs(call.latency_ms) }}</span>
                    <span class="call-tokens">
                      入 {{ call.input_tokens ?? 0 }} / 出 {{ call.output_tokens ?? 0 }}
                    </span>
                    <span v-if="call.usage_estimated" class="call-estimated">估算用量</span>
                    <span v-if="call.retries" class="call-retry">重试 {{ call.retries }} 次</span>
                    <span v-if="call.structured" class="call-structured">
                      结构化修复 {{ call.structured.repairs ?? 0 }} 次
                      <span v-if="call.structured.ok != null">({{ call.structured.ok ? '成功' : '失败' }})</span>
                    </span>
                  </div>

                  <div v-if="call.error_message" class="call-error">
                    {{ call.error_message }}
                  </div>

                  <!-- Prompt Snapshot block -->
                  <div
                    v-if="call.rendered_prompt_snapshot"
                    class="call-prompt-snapshot"
                  >
                    <div class="snapshot-header">
                      <span class="snapshot-title">Prompt 快照</span>
                      <span v-if="call.prompt_truncated" class="snapshot-truncated">已截断</span>
                    </div>
                    <pre class="snapshot-content">{{ call.rendered_prompt_snapshot }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-trace-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  backdrop-filter: blur(8px);
  z-index: 2500;
  display: flex;
  justify-content: flex-end;
  animation: fadeIn 0.15s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.run-trace-drawer {
  background: var(--bg-panel);
  width: min(760px, 94vw);
  height: 100%;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  border-left: 1px solid var(--border);
  overflow: hidden;
  animation: slideIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes slideIn {
  from { transform: translateX(100%); }
  to { transform: translateX(0); }
}

.trace-header {
  padding: var(--sp-4, 16px) var(--sp-5, 20px);
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg-surface, var(--bg-panel)) 95%, transparent);
}

.trace-header-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.trace-title {
  font-size: var(--text-lg, 18px);
  font-weight: 700;
  color: var(--text);
  margin: 0;
}

.trace-subtitle {
  font-size: var(--text-xs, 12px);
  color: var(--text-tertiary);
  margin: 4px 0 0 0;
}

.trace-close-btn {
  background: transparent;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm, 4px);
  transition: color 0.15s, background 0.15s;
}

.trace-close-btn:hover {
  color: var(--text);
  background: var(--bg-hover);
}

.trace-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-4) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.trace-state {
  padding: 40px;
  text-align: center;
  color: var(--text-tertiary);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.trace-error {
  color: var(--status-error);
}

.btn-retry {
  padding: 4px 14px;
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius-sm);
  font-size: 12px;
  cursor: pointer;
}

/* Alerts */
.trace-alert {
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.trace-alert-warning {
  background: var(--sev-warning-bg);
  border: 1px solid color-mix(in srgb, var(--sev-warning) 35%, transparent);
  color: var(--sev-warning);
}

.trace-alert-danger {
  background: var(--sev-critical-bg);
  border: 1px solid color-mix(in srgb, var(--status-error) 35%, transparent);
  color: var(--status-error);
}

.trace-reload-error {
  margin-bottom: 2px;
}

.reload-error-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 12px;
}

.failed-banner-content {
  display: flex;
  flex-wrap: wrap;
  line-height: 1.5;
}

.failed-banner-title {
  font-weight: 700;
}

.failed-banner-msg {
  word-break: break-word;
}

/* Overview section */
.overview-section {
  background: color-mix(in srgb, var(--bg-surface, var(--bg-panel)) 60%, transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  padding: 12px 14px;
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 10px;
}

.overview-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.card-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.card-value {
  font-size: 13px;
  font-weight: 650;
  color: var(--text);
}

.token-breakdown {
  font-size: 11px;
  font-weight: normal;
  color: var(--text-secondary);
}

.status-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  width: fit-content;
}

.status-tag.completed {
  background: var(--status-final-bg);
  color: var(--status-final);
}

.status-tag.failed {
  background: var(--status-error-bg);
  color: var(--status-error);
}

.status-tag.running {
  background: var(--status-revision-bg);
  color: var(--status-revision);
}

.request-id-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px dashed color-mix(in srgb, var(--border) 60%, transparent);
  font-size: 11px;
}

.request-id-label {
  color: var(--text-tertiary);
}

.request-id-code {
  font-family: monospace;
  background: color-mix(in srgb, var(--bg-panel) 80%, black 5%);
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
  color: var(--text);
}

.btn-copy-req {
  font-size: 11px;
  padding: 2px 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
}

.btn-copy-req:hover {
  background: var(--bg-hover);
  color: var(--text);
}

/* Toolbar */
.prompts-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.prompts-toolbar-left {
  display: flex;
  align-items: center;
  gap: 6px;
}

.toolbar-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
}

.step-count {
  font-size: 12px;
  color: var(--text-tertiary);
}

.btn-toggle-prompts {
  font-size: 12px;
  padding: 4px 10px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}

.btn-toggle-prompts:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.btn-toggle-prompts.active {
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}

.prompt-confirm-dialog {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--sev-info-bg);
  border: 1px solid color-mix(in srgb, var(--sev-info) 30%, transparent);
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--text);
}

.confirm-actions {
  display: flex;
  gap: 8px;
}

.btn-confirm-ok {
  font-size: 11px;
  padding: 2px 10px;
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.btn-confirm-cancel {
  font-size: 11px;
  padding: 2px 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
}

/* Timeline */
.steps-timeline {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.timeline-step {
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  background: color-mix(in srgb, var(--bg-panel) 90%, transparent);
  overflow: hidden;
  transition: border-color 0.15s;
}

.timeline-step.is-failed {
  border-color: var(--status-error);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--status-error) 30%, transparent);
}

.step-header {
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  user-select: none;
  background: color-mix(in srgb, var(--bg-surface, var(--bg-panel)) 60%, transparent);
}

.step-header:hover {
  background: var(--bg-hover);
}

.step-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.step-order {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-tertiary);
}

.step-name {
  font-size: 13px;
  font-weight: 650;
  color: var(--text);
}

.step-agent {
  font-size: 11px;
  padding: 1px 6px;
  background: color-mix(in srgb, var(--border) 40%, transparent);
  border-radius: 4px;
  color: var(--text-secondary);
}

.step-node-key {
  font-size: 11px;
  color: var(--text-tertiary);
}

.step-status-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 3px;
}

.step-status-tag.completed {
  background: var(--status-final-bg);
  color: var(--status-final);
}

.step-status-tag.failed {
  background: var(--status-error-bg);
  color: var(--status-error);
}

.step-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.step-quality-pill {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--status-error-bg);
  color: var(--status-error);
  border: 1px solid color-mix(in srgb, var(--status-error) 25%, transparent);
}

.step-quality-pill.passed {
  background: var(--status-final-bg);
  color: var(--status-final);
  border-color: color-mix(in srgb, var(--status-final) 25%, transparent);
}

.quality-code {
  font-weight: 600;
}

.step-retries,
.step-tokens {
  font-size: 11px;
  color: var(--text-tertiary);
}

.expand-arrow {
  font-size: 10px;
  color: var(--text-tertiary);
}

/* Duration bar */
.step-duration-track {
  height: 18px;
  background: color-mix(in srgb, var(--border) 30%, transparent);
  position: relative;
  overflow: hidden;
}

.step-duration-bar {
  height: 100%;
  background: var(--accent);
  display: flex;
  align-items: center;
  padding-left: 6px;
  color: var(--text-inverse);
  font-size: 10px;
  font-weight: 600;
  white-space: nowrap;
  transition: width 0.2s ease;
}

.step-duration-bar.completed {
  background: var(--status-final);
}

.step-duration-bar.failed {
  background: var(--status-error);
}

.duration-text {
  line-height: 1;
}

.step-error-banner {
  padding: 6px 12px;
  font-size: 12px;
  background: color-mix(in srgb, var(--status-error) 10%, transparent);
  color: var(--status-error);
  border-top: 1px solid color-mix(in srgb, var(--status-error) 20%, transparent);
  word-break: break-word;
}

/* Details: LLM calls */
.step-details {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
  background: color-mix(in srgb, var(--bg-surface, var(--bg-panel)) 40%, transparent);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.no-calls-msg {
  font-size: 12px;
  color: var(--text-tertiary);
}

.llm-calls-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.llm-call-item {
  border: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-panel);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}

.call-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.call-index {
  font-weight: 700;
  color: var(--text-secondary);
}

.call-model {
  font-family: monospace;
  font-size: 11px;
  background: color-mix(in srgb, var(--border) 40%, transparent);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--text);
}

.call-latency {
  color: var(--text-secondary);
}

.call-tokens {
  color: var(--text-tertiary);
}

.call-estimated {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--sev-warning-bg);
  color: var(--sev-warning);
}

.call-retry {
  font-size: 10px;
  color: var(--sev-warning);
}

.call-structured {
  font-size: 10px;
  color: var(--accent);
}

.call-error {
  color: var(--status-error);
  font-size: 11px;
  word-break: break-word;
}

.call-prompt-snapshot {
  margin-top: 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  overflow: hidden;
}

.snapshot-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 8px;
  background: color-mix(in srgb, var(--border) 30%, transparent);
  font-size: 10px;
  color: var(--text-secondary);
  font-weight: 600;
}

.snapshot-truncated {
  color: var(--sev-warning);
  background: var(--sev-warning-bg);
  padding: 1px 4px;
  border-radius: 2px;
}

.snapshot-content {
  margin: 0;
  padding: 8px;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
  font-family: monospace;
  color: var(--text);
}
</style>
