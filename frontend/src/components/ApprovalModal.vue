<script setup lang="ts">
import { computed, ref } from 'vue'
import type { GenerateMode, ProjectMode, QualityReportPayload } from '../api/types'
import { buildTextComparisonDiff } from '../utils/textDiff'

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
}>()

// 对比视图两侧的复制反馈状态（短暂显示“已复制”）
const copiedSide = ref<'original' | 'candidate' | null>(null)
let copyResetTimer: ReturnType<typeof setTimeout> | null = null

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
  props.content,
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
              <button
                class="copy-btn"
                :class="{ copied: copiedSide === 'candidate' }"
                :disabled="!content"
                @click="copySideContent('candidate')"
              >
                {{ copiedSide === 'candidate' ? '已复制' : '复制' }}
              </button>
            </div>
            <pre class="modal-content"><span v-for="(segment, index) in comparisonDiff.candidate" :key="`candidate-${index}`" class="diff-segment" :class="`diff-${segment.type}`">{{ segment.text }}</span></pre>
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
  background: var(--bg-overlay);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 90%;
  max-width: min(1180px, 94vw);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
}

.modal-title {
  margin: 0;
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--text-base);
  font-weight: 600;
  border-bottom: 1px solid var(--border);
}

.modal-hint {
  margin: 0;
  padding: var(--sp-2) var(--sp-4);
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

.diff-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  padding: 0 var(--sp-4) var(--sp-2);
  color: var(--text-tertiary);
  font-size: var(--text-xs);
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

.diff-legend-removed { background: var(--diff-del-bg); border: 1px solid var(--diff-del-fg); }
.diff-legend-added { background: var(--diff-ins-bg); border: 1px solid var(--diff-ins-fg); }

.quality-summary {
  margin: 0 var(--sp-4);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--text-xs);
  background: var(--bg-hover);
  border-radius: var(--radius);
  border: 1px solid var(--border);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
}

.quality-summary.warning {
  background: var(--sev-warning-bg);
  border-color: color-mix(in srgb, var(--sev-warning) 45%, transparent);
}

.quality-summary.warning .quality-label,
.quality-summary.warning .quality-issues {
  color: var(--sev-warning);
}

.quality-label {
  font-weight: 600;
  color: var(--text);
}

.quality-metric {
  color: var(--text-secondary);
}

.quality-issues {
  color: var(--status-error);
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

.diff-segment {
  border-radius: 3px;
  padding: 0 1px;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}

.diff-removed {
  background: var(--diff-del-bg);
  color: var(--diff-del-fg);
  border-bottom: 2px solid var(--diff-del-fg);
}

.diff-added {
  background: var(--diff-ins-bg);
  color: var(--diff-ins-fg);
  border-bottom: 2px solid var(--diff-ins-fg);
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
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.draft-block-original {
  border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
}

.draft-block-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  flex-shrink: 0;
  padding: var(--sp-3);
  border-bottom: 1px solid var(--border);
  margin-bottom: 0;
  color: var(--text);
  font-size: var(--text-sm);
}

.draft-block-title span {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-weight: 400;
}

.draft-block-title-right {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
}

.copy-btn {
  flex-shrink: 0;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-panel);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  transition:
    background var(--transition),
    border-color var(--transition),
    color var(--transition);
}

.copy-btn:hover:not(:disabled) {
  border-color: var(--border-focus);
  color: var(--text);
}

.copy-btn.copied {
  border-color: var(--status-final);
  color: var(--status-final);
  background: var(--status-final-bg);
}

.copy-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.modal-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  background: var(--bg-sidebar);
}

.modal-action-group {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  justify-content: flex-end;
}

.modal-btn {
  min-height: 36px;
  padding: 0 var(--sp-4);
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  font-size: var(--text-sm);
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
  transition:
    background var(--transition),
    border-color var(--transition),
    color var(--transition),
    box-shadow var(--transition),
    transform var(--transition);
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
  box-shadow: 0 0 0 3px var(--accent-subtle);
}

.modal-btn-ghost {
  color: var(--text-tertiary);
  background: transparent;
  border-color: transparent;
  font-weight: 500;
}

.modal-btn-ghost:hover {
  color: var(--status-error);
  background: var(--sev-critical-bg);
}

.modal-btn-secondary {
  color: var(--text-secondary);
  background: var(--bg-panel);
  border-color: var(--border);
  box-shadow: var(--shadow-sm);
}

.modal-btn-warning {
  color: var(--sev-warning);
  background: var(--sev-warning-bg);
  border-color: color-mix(in srgb, var(--sev-warning) 45%, transparent);
}

.modal-btn-warning:hover {
  background: color-mix(in srgb, var(--sev-warning) 24%, var(--bg-panel));
  border-color: var(--sev-warning);
}

.modal-btn-secondary:hover {
  color: var(--text);
  border-color: var(--border-focus);
  background: var(--bg-hover);
}

.modal-btn-primary {
  color: var(--text-inverse);
  background: var(--accent);
  border-color: var(--accent);
  box-shadow: var(--shadow-sm);
}

.modal-btn-primary:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

.modal-btn-final {
  color: var(--text-inverse);
  background: var(--status-final);
  border-color: var(--status-final);
  box-shadow: var(--shadow-sm);
}

.modal-btn-final:hover:not(:disabled) {
  opacity: 0.9;
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
