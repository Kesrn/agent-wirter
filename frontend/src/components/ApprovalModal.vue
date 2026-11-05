<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { GenerateMode, ProjectMode, QualityReportPayload } from '../api/types'
import { buildTextComparisonDiff } from '../utils/textDiff'
import { estimateEditMetrics, type EditMetricsResult } from '../utils/editMetrics'

const props = defineProps<{
  show: boolean
  content: string
  originalContent?: string
  mode: GenerateMode
  hasHITL: boolean
  projectMode: ProjectMode
  canFinalize?: boolean
  finalizing?: boolean
  qualityReport?: QualityReportPayload | null
}>()

const emit = defineEmits<{
  (e: 'decision', value: 'accept' | 'accept_with_mods' | 'compress' | 'reject'): void
  (e: 'finalize'): void
  (e: 'update:content', value: string): void
}>()

// 对比视图两侧的复制反馈状态（短暂显示“已复制”）
const copiedSide = ref<'original' | 'candidate' | null>(null)
let copyResetTimer: ReturnType<typeof setTimeout> | null = null

// 编辑与实时指标状态
const isEditing = ref(false)
const editableContent = ref(props.content)
const metrics = ref<EditMetricsResult>(estimateEditMetrics(props.originalContent ?? '', props.content))
let debounceTimer: ReturnType<typeof setTimeout> | null = null

function updateMetricsNow(original: string, current: string) {
  metrics.value = estimateEditMetrics(original, current)
}

function onContentInput() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    updateMetricsNow(props.originalContent ?? '', editableContent.value)
    emit('update:content', editableContent.value)
  }, 200)
}

watch(() => props.content, (val) => {
  editableContent.value = val
  updateMetricsNow(props.originalContent ?? '', val)
})

watch(() => props.originalContent, (val) => {
  updateMetricsNow(val ?? '', editableContent.value)
})

onBeforeUnmount(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
  if (copyResetTimer) clearTimeout(copyResetTimer)
})

async function copySideContent(side: 'original' | 'candidate') {
  const text = side === 'original' ? (props.originalContent ?? '') : props.content
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 剪贴板 API 不可用时静默失败（Electron/现代浏览器均可用，此处兜底）
    return
  }
  copiedSide.value = side
  if (copyResetTimer) clearTimeout(copyResetTimer)
  copyResetTimer = setTimeout(() => {
    copiedSide.value = null
  }, 1600)
}

const NOVEL_TITLE_MAP: Record<GenerateMode, string> = {
  full_pipeline: '人工审核',
  enhance: '润色结果',
  continue: '续写结果',
  summarize: '读者视角分析',
}

const ARTICLE_TITLE_MAP: Record<GenerateMode, string> = {
  full_pipeline: '内容审核',
  enhance: '改写优化结果',
  continue: '内容结果',
  summarize: '受众视角分析',
}

const ACCEPT_LABEL = '采用为草稿'
const REJECT_LABEL = '丢弃'

const lengthIssue = computed(() =>
  props.qualityReport?.issues?.find(issue =>
    issue.code === 'CONTENT_TOO_LONG' || issue.code === 'CONTENT_TOO_SHORT',
  ),
)
const isTooLong = computed(() => lengthIssue.value?.code === 'CONTENT_TOO_LONG')
const hasQualityWarning = computed(() => props.qualityReport?.status === 'warning')
const qualityLabel = computed(() => {
  if (props.qualityReport?.status === 'warning') return '需要你确认'
  return props.qualityReport?.status === 'passed' ? '通过' : '未通过'
})
const hasComparison = computed(() =>
  !!props.originalContent && props.originalContent !== props.content,
)
const isEnhance = computed(() => props.mode === 'enhance')
const comparisonDiff = computed(() => buildTextComparisonDiff(
  props.originalContent ?? '',
  editableContent.value,
))

function titleLabel(): string {
  return props.projectMode === 'article'
    ? ARTICLE_TITLE_MAP[props.mode]
    : NOVEL_TITLE_MAP[props.mode]
}

function modsLabel(): string {
  return props.hasHITL ? '让 AI 修改' : '放入编辑器修改'
}

function originalTitle(): string {
  if (isEnhance.value) return props.projectMode === 'article' ? '改写前原文' : '润色前原文'
  return 'AI 初稿（写手原文）'
}

function originalHint(): string {
  if (isEnhance.value) return '本次改写前的原始内容，仅供对照'
  return '编辑前的原始版本，仅供对照'
}

function candidateTitle(): string {
  if (isEnhance.value) return props.projectMode === 'article' ? '改写后的候选内容' : '润色后的候选稿'
  return hasComparison.value ? '编辑后的候选稿' : 'AI 生成候选稿'
}
</script>

<template>
  <div v-if="show" class="modal-overlay">
    <div class="modal-card">
      <h3 class="modal-title">{{ titleLabel() }}</h3>
      <p class="modal-hint">
        <template v-if="canFinalize">确认"定稿本章"会保存当前版本，并作为下一章创作的可信上文。</template>
        <template v-else>以下为 AI 生成的{{ props.projectMode === 'article' ? '候选内容' : '候选稿' }}，确认后将保存为当前编辑器草稿。</template>
      </p>
      <div v-if="hasComparison" class="diff-legend" aria-label="差异标记说明">
        <span><i class="diff-legend-swatch diff-legend-removed"></i>原文删除或被替换</span>
        <span><i class="diff-legend-swatch diff-legend-added"></i>候选稿新增或替换</span>
      </div>
      <!-- 实时近似人工改动指标 -->
      <div
        v-if="props.originalContent && (metrics.changedChars > 0 || isEditing)"
        class="edit-metrics-banner"
        role="status"
        title="最终以保存后的统计为准"
      >
        <span class="metrics-icon" aria-hidden="true">✎</span>
        <span class="metrics-text">已修改约 {{ metrics.changedChars }} 字（约 {{ Math.round(metrics.ratio * 100) }}%）</span>
        <span class="metrics-hint">最终以保存后的统计为准</span>
      </div>
      <!-- Quality report summary -->
      <div
        v-if="qualityReport && qualityReport.stage === 'final'"
        class="quality-summary"
        :class="{ warning: hasQualityWarning }"
      >
        <span class="quality-label">质量验收：{{ qualityLabel }}</span>
        <span v-if="qualityReport.target_chars > 0" class="quality-metric">
          目标 {{ qualityReport.target_chars }} 字 / 实际 {{ qualityReport.actual_chars }} 字
        </span>
        <span v-if="qualityReport.similarity != null" class="quality-metric">
          编辑相似度 {{ (qualityReport.similarity * 100).toFixed(0) }}%
        </span>
        <span v-if="qualityReport.changed_chars != null && qualityReport.changed_chars > 0" class="quality-metric">
          修改 {{ qualityReport.changed_chars }} 字
        </span>
        <span v-if="qualityReport.issues && qualityReport.issues.length" class="quality-issues">
          {{ qualityReport.issues.map(i => i.message).join('；') }}
        </span>
      </div>
      <div class="modal-body">
        <div class="comparison-grid" :class="{ 'has-comparison': hasComparison }">
          <section v-if="hasComparison" class="draft-block draft-block-original">
            <div class="draft-block-title">
              <strong>{{ originalTitle() }}</strong>
              <span class="draft-block-title-right">
                <span>{{ originalHint() }}</span>
                <button
                  class="copy-btn"
                  :class="{ copied: copiedSide === 'original' }"
                  :disabled="!originalContent"
                  @click="copySideContent('original')"
                >
                  {{ copiedSide === 'original' ? '已复制' : '复制' }}
                </button>
              </span>
            </div>
            <pre class="modal-content"><span v-for="(segment, index) in comparisonDiff.original" :key="`original-${index}`" class="diff-segment" :class="`diff-${segment.type}`">{{ segment.text }}</span></pre>
          </section>
          <section class="draft-block">
            <div class="draft-block-title">
              <strong>{{ candidateTitle() }}</strong>
              <div class="draft-block-actions">
                <button
                  type="button"
                  class="edit-toggle-btn"
                  @click="isEditing = !isEditing"
                >
                  {{ isEditing ? '完成修改' : '手动修改' }}
                </button>
                <button
                  class="copy-btn"
                  :class="{ copied: copiedSide === 'candidate' }"
                  :disabled="!content"
                  @click="copySideContent('candidate')"
                >
                  {{ copiedSide === 'candidate' ? '已复制' : '复制' }}
                </button>
              </div>
            </div>
            <textarea
              v-if="isEditing"
              v-model="editableContent"
              class="modal-content modal-textarea"
              placeholder="在此修改候选稿..."
              @input="onContentInput"
            />
            <pre v-else class="modal-content"><span v-for="(segment, index) in comparisonDiff.candidate" :key="`candidate-${index}`" class="diff-segment" :class="`diff-${segment.type}`">{{ segment.text }}</span></pre>
          </section>
        </div>
      </div>
      <div class="modal-actions">
        <button class="modal-btn modal-btn-ghost" :disabled="finalizing" @click="emit('decision', 'reject')">{{ REJECT_LABEL }}</button>
        <div class="modal-action-group">
          <button
            v-if="isTooLong && hasHITL"
            class="modal-btn modal-btn-warning"
            :disabled="finalizing"
            @click="emit('decision', 'compress')"
          >
            让 AI 压缩
          </button>
          <button class="modal-btn modal-btn-secondary" :disabled="finalizing" @click="emit('decision', 'accept_with_mods')">{{ modsLabel() }}</button>
          <button class="modal-btn modal-btn-primary" :disabled="finalizing" @click="emit('decision', 'accept')">{{ ACCEPT_LABEL }}</button>
          <button v-if="canFinalize" class="modal-btn modal-btn-final" :disabled="finalizing" @click="emit('finalize')">
            {{ finalizing ? '定稿中…' : '定稿本章' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-card {
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: var(--radius, 8px);
  width: 90%;
  max-width: min(1180px, 94vw);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.12);
}

.modal-title {
  margin: 0;
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
  font-size: var(--text-base, 14px);
  font-weight: 600;
  border-bottom: 1px solid var(--border, #e5e7eb);
}

.modal-hint {
  margin: 0;
  padding: var(--sp-2, 8px) var(--sp-4, 16px);
  font-size: var(--text-xs, 12px);
  color: var(--text-tertiary, #9ca3af);
}

.diff-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3, 12px);
  padding: 0 var(--sp-4, 16px) var(--sp-2, 8px);
  color: var(--text-tertiary, #9ca3af);
  font-size: var(--text-xs, 12px);
}

.diff-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.diff-legend-swatch {
  width: 11px;
  height: 11px;
  border-radius: 3px;
  display: inline-block;
}

.diff-legend-removed { background: rgb(248 113 113 / 42%); }
.diff-legend-added { background: rgb(74 222 128 / 42%); }

.quality-summary {
  margin: 0 var(--sp-4, 16px);
  padding: var(--sp-2, 8px) var(--sp-3, 12px);
  font-size: var(--text-xs, 12px);
  background: var(--bg-hover, #f9fafb);
  border-radius: var(--radius, 6px);
  border: 1px solid var(--border, #e5e7eb);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2, 8px);
  align-items: center;
}

.quality-summary.warning {
  background: rgb(245 158 11 / 10%);
  border-color: rgb(245 158 11 / 45%);
}

.quality-summary.warning .quality-label,
.quality-summary.warning .quality-issues {
  color: #d97706;
}

.quality-label {
  font-weight: 600;
  color: var(--text, #111827);
}

.quality-metric {
  color: var(--text-secondary, #6b7280);
}

.quality-issues {
  color: var(--status-error, #dc2626);
  width: 100%;
}

.modal-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
}

.comparison-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--sp-3, 12px);
  height: 100%;
  min-height: 0;
}

.comparison-grid.has-comparison {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.modal-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--sp-3, 12px);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-sm, 13px);
  line-height: 1.7;
  margin: 0;
  font-family: inherit;
  color: var(--text);
}

.modal-textarea {
  border: none;
  outline: none;
  resize: none;
  background: transparent;
  box-sizing: border-box;
}

.modal-textarea:focus {
  background: color-mix(in srgb, var(--accent) 4%, transparent);
}

.edit-metrics-banner {
  display: flex;
  align-items: center;
  gap: var(--sp-2, 8px);
  padding: 6px 12px;
  margin: 0 var(--sp-4, 16px) var(--sp-2, 8px);
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-panel));
  border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-radius: var(--radius-sm, 6px);
  font-size: var(--text-xs, 12px);
  color: var(--text);
}

.metrics-icon {
  font-size: 13px;
  color: var(--accent);
}

.metrics-text {
  font-weight: 650;
  color: var(--accent);
}

.metrics-hint {
  color: var(--text-tertiary);
  font-size: 11px;
}

.draft-block-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2, 8px);
}

.edit-toggle-btn {
  padding: 2px 8px;
  font-size: 11px;
  border-radius: var(--radius-sm, 4px);
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--transition, 0.15s ease);
}

.edit-toggle-btn:hover {
  background: var(--bg-hover, #f3f4f6);
  color: var(--accent);
}

.diff-segment {
  border-radius: 3px;
  padding: 0 1px;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}

.diff-removed {
  background: rgb(248 113 113 / 24%);
  border-bottom: 2px solid rgb(248 113 113 / 72%);
}

.diff-added {
  background: rgb(74 222 128 / 22%);
  border-bottom: 2px solid rgb(74 222 128 / 72%);
}

.draft-block + .draft-block {
  margin-top: 0;
}

.draft-block {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: var(--radius, 8px);
}

.draft-block-original {
  border-color: color-mix(in srgb, var(--accent, #2563eb) 60%, var(--border, #e5e7eb));
}

.draft-block-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3, 12px);
  flex-shrink: 0;
  padding: var(--sp-3, 12px);
  border-bottom: 1px solid var(--border, #e5e7eb);
  margin-bottom: 0;
  color: var(--text, #111827);
  font-size: var(--text-sm, 13px);
}

.draft-block-title span {
  color: var(--text-tertiary, #9ca3af);
  font-size: var(--text-xs, 12px);
  font-weight: 400;
}

.draft-block-title-right {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2, 8px);
}

.copy-btn {
  flex-shrink: 0;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  background: var(--bg-panel, #fff);
  color: var(--text-secondary, #6b7280);
  font-size: var(--text-xs, 12px);
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  transition:
    background var(--transition, 120ms ease),
    border-color var(--transition, 120ms ease),
    color var(--transition, 120ms ease);
}

.copy-btn:hover:not(:disabled) {
  border-color: var(--border-focus, #a0a0a8);
  color: var(--text, #111827);
}

.copy-btn.copied {
  border-color: var(--status-final, #059669);
  color: var(--status-final, #059669);
  background: rgb(5 150 105 / 8%);
}

.copy-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.modal-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3, 12px);
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
  border-top: 1px solid var(--border, #e5e7eb);
  background: var(--bg-sidebar);
}

.modal-action-group {
  display: flex;
  gap: var(--sp-2, 8px);
  align-items: center;
  justify-content: flex-end;
}

.modal-btn {
  min-height: 36px;
  padding: 0 var(--sp-4, 16px);
  border-radius: var(--radius, 6px);
  border: 1px solid transparent;
  font-size: var(--text-sm, 13px);
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
  transition:
    background var(--transition, 120ms ease),
    border-color var(--transition, 120ms ease),
    color var(--transition, 120ms ease),
    box-shadow var(--transition, 120ms ease),
    transform var(--transition, 120ms ease);
}

.modal-btn:hover {
  transform: translateY(-1px);
}

.modal-btn:active {
  transform: translateY(0);
}

.modal-btn:disabled {
  cursor: wait;
  opacity: 0.58;
  transform: none;
}

.modal-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.16);
}

.modal-btn-ghost {
  color: var(--text-tertiary, #9ca3af);
  background: transparent;
  border-color: transparent;
  font-weight: 500;
}

.modal-btn-ghost:hover {
  color: var(--status-error, #dc2626);
  background: var(--sev-critical-bg, #fef2f2);
}

.modal-btn-secondary {
  color: var(--text-secondary, #6b7280);
  background: var(--bg-panel, #fff);
  border-color: var(--border, #e5e7eb);
  box-shadow: var(--shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.05));
}

.modal-btn-warning {
  color: #b45309;
  background: rgb(245 158 11 / 10%);
  border-color: rgb(245 158 11 / 45%);
}

.modal-btn-warning:hover {
  color: #92400e;
  background: rgb(245 158 11 / 18%);
  border-color: rgb(245 158 11 / 65%);
}

.modal-btn-secondary:hover {
  color: var(--text, #111827);
  border-color: var(--border-focus, #a0a0a8);
  background: var(--bg-hover, #f3f4f6);
}

.modal-btn-primary {
  color: var(--text-inverse, #fff);
  background: var(--accent, #2563eb);
  border-color: var(--accent, #2563eb);
  box-shadow: 0 8px 18px rgba(37, 99, 235, 0.22);
}

.modal-btn-primary:hover {
  background: var(--accent-hover, #1d4ed8);
  border-color: var(--accent-hover, #1d4ed8);
  box-shadow: 0 10px 22px rgba(37, 99, 235, 0.26);
}

.modal-btn-final {
  color: var(--text-inverse, #fff);
  background: var(--status-final, #059669);
  border-color: var(--status-final, #059669);
  box-shadow: 0 8px 18px rgba(5, 150, 105, 0.22);
}

.modal-btn-final:hover:not(:disabled) {
  background: #047857;
  border-color: #047857;
  box-shadow: 0 10px 22px rgba(5, 120, 87, 0.26);
}

@media (max-width: 560px) {
  .modal-card {
    max-width: calc(100vw - 24px);
    max-height: 92vh;
  }

  .modal-body {
    overflow-y: auto;
  }

  .comparison-grid,
  .comparison-grid.has-comparison {
    grid-template-columns: 1fr;
    height: auto;
  }

  .draft-block {
    min-height: 220px;
    max-height: 42vh;
  }

  .modal-actions {
    align-items: stretch;
    flex-direction: column-reverse;
  }

  .modal-action-group {
    flex-direction: column-reverse;
    align-items: stretch;
  }

  .modal-btn {
    width: 100%;
  }
}
</style>
