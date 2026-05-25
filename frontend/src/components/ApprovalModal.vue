<script setup lang="ts">
import type { GenerateMode, ProjectMode } from '../api/types'

const props = defineProps<{
  show: boolean
  content: string
  mode: GenerateMode
  hasHITL: boolean
  projectMode: ProjectMode
}>()

const emit = defineEmits<{
  (e: 'decision', value: 'accept' | 'accept_with_mods' | 'reject'): void
}>()

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

const ACCEPT_LABEL = '应用到编辑器'
const REJECT_LABEL = '丢弃'

function titleLabel(): string {
  return props.projectMode === 'article'
    ? ARTICLE_TITLE_MAP[props.mode]
    : NOVEL_TITLE_MAP[props.mode]
}

function modsLabel(): string {
  return props.hasHITL ? '让 AI 修改' : '放入编辑器修改'
}
</script>

<template>
  <div v-if="show" class="modal-overlay" @click.self="emit('decision', 'reject')">
    <div class="modal-card">
      <h3 class="modal-title">{{ titleLabel() }}</h3>
      <p class="modal-hint">以下为 AI 生成的{{ props.projectMode === 'article' ? '候选内容' : '候选稿' }}，确认后将写入编辑器草稿，需手动保存。</p>
      <div class="modal-body">
        <pre class="modal-content">{{ content }}</pre>
      </div>
      <div class="modal-actions">
        <button class="modal-btn modal-btn-ghost" @click="emit('decision', 'reject')">{{ REJECT_LABEL }}</button>
        <div class="modal-action-group">
          <button class="modal-btn modal-btn-secondary" @click="emit('decision', 'accept_with_mods')">{{ modsLabel() }}</button>
          <button class="modal-btn modal-btn-primary" @click="emit('decision', 'accept')">{{ ACCEPT_LABEL }}</button>
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
  max-width: 720px;
  max-height: 80vh;
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

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
}

.modal-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-sm, 13px);
  line-height: 1.7;
  margin: 0;
  font-family: inherit;
  color: var(--text-primary, #111827);
}

.modal-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3, 12px);
  padding: var(--sp-3, 12px) var(--sp-4, 16px);
  border-top: 1px solid var(--border, #e5e7eb);
  background: color-mix(in srgb, var(--bg-sidebar, #f7f7f8) 72%, #fff);
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

@media (max-width: 560px) {
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
