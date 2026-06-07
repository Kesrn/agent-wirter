<script setup lang="ts">
import type { DiffHunk } from '../api/types'

const props = defineProps<{
  leftTitle: string
  rightTitle: string
  diff: DiffHunk[]
}>()

const emit = defineEmits<{
  close: []
}>()

interface DiffLine {
  tag: 'equal' | 'insert' | 'delete' | 'replace'
  text: string
  side: 'left' | 'right'
  lineNo: number
}

function splitDiff(diff: DiffHunk[]): { left: DiffLine[]; right: DiffLine[] } {
  const left: DiffLine[] = []
  const right: DiffLine[] = []
  let leftNo = 1
  let rightNo = 1

  for (const hunk of diff) {
    if (hunk.tag === 'equal' && hunk.lines) {
      for (const line of hunk.lines) {
        left.push({ tag: 'equal', text: line, side: 'left', lineNo: leftNo++ })
        right.push({ tag: 'equal', text: line, side: 'right', lineNo: rightNo++ })
      }
    } else if (hunk.tag === 'delete' && hunk.lines) {
      for (const line of hunk.lines) {
        left.push({ tag: 'delete', text: line, side: 'left', lineNo: leftNo++ })
        right.push({ tag: 'equal', text: '', side: 'right', lineNo: 0 })
      }
    } else if (hunk.tag === 'insert' && hunk.lines) {
      for (const line of hunk.lines) {
        left.push({ tag: 'equal', text: '', side: 'left', lineNo: 0 })
        right.push({ tag: 'insert', text: line, side: 'right', lineNo: rightNo++ })
      }
    } else if (hunk.tag === 'replace') {
      const linesA = hunk.lines_a ?? []
      const linesB = hunk.lines_b ?? []
      const maxLen = Math.max(linesA.length, linesB.length)
      for (let i = 0; i < maxLen; i++) {
        if (i < linesA.length) {
          left.push({ tag: 'replace', text: linesA[i], side: 'left', lineNo: leftNo++ })
        } else {
          left.push({ tag: 'equal', text: '', side: 'left', lineNo: 0 })
        }
        if (i < linesB.length) {
          right.push({ tag: 'replace', text: linesB[i], side: 'right', lineNo: rightNo++ })
        } else {
          right.push({ tag: 'equal', text: '', side: 'right', lineNo: 0 })
        }
      }
    }
  }
  return { left, right }
}
</script>

<template>
  <div class="diff-overlay" @click.self="emit('close')">
    <div class="diff-modal">
      <div class="diff-header">
        <span class="diff-title">版本对比</span>
        <button class="btn-close" @click="emit('close')">&times;</button>
      </div>
      <div class="diff-desk">
        <div class="diff-page diff-page-left">
          <div class="page-title">{{ leftTitle }}</div>
          <div class="page-body">
            <div
              v-for="(line, i) in splitDiff(diff).left"
              :key="i"
              class="page-line"
              :class="{
                'line-delete': line.tag === 'delete',
                'line-replace': line.tag === 'replace',
                'line-empty': line.text === '' && line.lineNo === 0,
              }"
            >
              <span v-if="line.lineNo > 0" class="line-no">{{ line.lineNo }}</span>
              <span v-else class="line-no line-no-dim">&nbsp;</span>
              <span class="line-text">{{ line.text || '​' }}</span>
            </div>
          </div>
        </div>
        <div class="diff-page diff-page-right">
          <div class="page-title">{{ rightTitle }}</div>
          <div class="page-body">
            <div
              v-for="(line, i) in splitDiff(diff).right"
              :key="i"
              class="page-line"
              :class="{
                'line-insert': line.tag === 'insert',
                'line-replace': line.tag === 'replace',
                'line-empty': line.text === '' && line.lineNo === 0,
              }"
            >
              <span v-if="line.lineNo > 0" class="line-no">{{ line.lineNo }}</span>
              <span v-else class="line-no line-no-dim">&nbsp;</span>
              <span class="line-text">{{ line.text || '​' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ─── Overlay ─── */
.diff-overlay {
  --diff-modal-bg: #f0ede8;
  --diff-chrome-bg: #e8e4de;
  --diff-header-bg: #e8e4de;
  --diff-border: #d5d0c8;
  --diff-title: #3a3632;
  --diff-muted: #8a8580;
  --diff-page-bg: var(--paper-bg);
  --diff-page-title-bg: linear-gradient(to bottom, #faf8f4, #f5f2ec);
  --diff-page-title: #6b6560;
  --diff-text: #2c2825;
  --diff-line-no: #8e8580;
  --diff-delete-bg: rgba(220, 38, 38, 0.16);
  --diff-delete-border: #dc322f;
  --diff-delete-decoration: rgba(220, 50, 47, 0.58);
  --diff-insert-bg: rgba(22, 163, 74, 0.16);
  --diff-insert-border: #28a03c;
  --diff-replace-bg: rgba(202, 138, 4, 0.18);
  --diff-replace-border: #c8a014;
  --diff-empty-bg: rgba(0, 0, 0, 0.04);
  position: fixed;
  inset: 0;
  background: rgba(30, 30, 40, 0.5);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.diff-modal {
  width: 94vw;
  max-width: 1140px;
  max-height: 88vh;
  background: var(--diff-modal-bg);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 16px 56px rgba(0, 0, 0, 0.28);
}

/* ─── Header ─── */
.diff-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  background: var(--diff-header-bg);
  border-bottom: 1px solid var(--diff-border);
  flex-shrink: 0;
}
.diff-title {
  font-weight: 700;
  font-size: 15px;
  color: var(--diff-title);
  letter-spacing: 0.02em;
}
.btn-close {
  background: none;
  border: none;
  font-size: 24px;
  color: var(--diff-muted);
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
  transition: color 0.15s;
}
.btn-close:hover {
  color: var(--diff-title);
}

/* ─── Desk (holds two pages) ─── */
.diff-desk {
  flex: 1;
  display: flex;
  gap: 20px;
  padding: 20px;
  overflow: auto;
  background: var(--diff-chrome-bg);
}

/* ─── Single page ─── */
.diff-page {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--diff-page-bg);
  border-radius: var(--paper-radius);
  box-shadow: var(--paper-shadow), 0 0 0 1px rgba(0,0,0,0.03);
  overflow: hidden;
}
.page-title {
  padding: 10px 20px;
  font-size: 12px;
  font-weight: 700;
  color: var(--diff-page-title);
  background: var(--diff-page-title-bg);
  border-bottom: 1px solid var(--diff-border);
  text-align: center;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  flex-shrink: 0;
}
.page-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 0;
  font-family: var(--font-serif);
  font-size: 14px;
  line-height: 1.85;
  color: var(--diff-text);
  background-image: repeating-linear-gradient(
    transparent,
    transparent calc(1.85em - 1px),
    var(--paper-line) calc(1.85em - 1px),
    var(--paper-line) 1.85em
  );
  background-size: 100% 1.85em;
  background-position: 0 16px;
}

/* ─── Lines ─── */
.page-line {
  display: flex;
  padding: 0 20px;
  min-height: 1.85em;
  border-left: 3px solid transparent;
  transition: background 0.1s;
}
.page-line.line-delete {
  background: var(--diff-delete-bg);
  border-left-color: var(--diff-delete-border);
}
.page-line.line-delete .line-text {
  text-decoration: line-through;
  text-decoration-color: var(--diff-delete-decoration);
}
.page-line.line-insert {
  background: var(--diff-insert-bg);
  border-left-color: var(--diff-insert-border);
}
.page-line.line-replace {
  background: var(--diff-replace-bg);
  border-left-color: var(--diff-replace-border);
}
.page-line.line-empty {
  background: var(--diff-empty-bg);
}

.line-no {
  display: inline-block;
  width: 3.2em;
  text-align: right;
  padding-right: 12px;
  color: var(--diff-line-no);
  user-select: none;
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.85;
}
.line-no-dim {
  color: transparent;
}
.line-text {
  white-space: pre-wrap;
  word-break: break-all;
  flex: 1;
  min-width: 0;
}

html[data-theme="dark"] .diff-overlay {
  --diff-modal-bg: #0d1420;
  --diff-chrome-bg: #111a29;
  --diff-header-bg: #172235;
  --diff-border: #334156;
  --diff-title: #f1f5f9;
  --diff-muted: #93a4b8;
  --diff-page-bg: #121b2b;
  --diff-page-title-bg: linear-gradient(to bottom, #1e2a3d, #182336);
  --diff-page-title: #dbe7f5;
  --diff-text: #eef4ff;
  --diff-line-no: #a9b8ca;
  --diff-delete-bg: rgba(248, 113, 113, 0.24);
  --diff-delete-border: #fb7185;
  --diff-delete-decoration: rgba(252, 165, 165, 0.86);
  --diff-insert-bg: rgba(52, 211, 153, 0.22);
  --diff-insert-border: #34d399;
  --diff-replace-bg: rgba(251, 191, 36, 0.24);
  --diff-replace-border: #fbbf24;
  --diff-empty-bg: rgba(148, 163, 184, 0.09);
  background: rgba(3, 7, 18, 0.74);
}

html[data-theme="dark"] .diff-page {
  box-shadow: 0 22px 55px rgba(0, 0, 0, 0.48), 0 0 0 1px rgba(148, 163, 184, 0.2);
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .diff-modal {
    width: 99vw;
    max-height: 94vh;
    border-radius: 6px;
  }
  .diff-desk {
    flex-direction: column;
    gap: 12px;
    padding: 12px;
  }
  .page-body {
    max-height: 36vh;
  }
  .page-title {
    font-size: 11px;
    padding: 8px 16px;
  }
  .page-line {
    padding: 0 12px;
  }
}
</style>
