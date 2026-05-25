<script setup lang="ts">
import { ref } from 'vue'
import type { ProjectMode } from '../api/types'

const props = defineProps<{
  suggestions: string[]
  projectMode?: ProjectMode
}>()

const emit = defineEmits<{
  confirm: [direction: string, userNote: string]
  cancel: []
}>()

const selectedDirection = ref(props.suggestions[0] ?? '')
const userNote = ref('')
const isArticle = props.projectMode === 'article'

function handleConfirm() {
  emit('confirm', selectedDirection.value, userNote.value)
}
</script>

<template>
  <div class="picker-overlay" @click.self="emit('cancel')">
    <div class="picker-card">
      <div class="picker-header">
        <h3>{{ isArticle ? '选择内容方向' : '选择转折方向' }}</h3>
        <p class="picker-subtitle">{{ isArticle ? 'AI 根据稿件和 brief 给出以下标题/内容方向' : 'AI 分析了当前写作情况，给出以下方向建议' }}</p>
      </div>

      <div class="picker-body">
        <div v-if="!suggestions.length" class="loading-hint">
          <span class="spinner"></span>
          <span>AI 正在分析{{ isArticle ? '稿件内容' : '章节内容' }}...</span>
        </div>
        <div v-else class="direction-list">
          <div v-for="(dir, i) in suggestions" :key="i" class="direction-item" :class="{ selected: selectedDirection === dir }" @click="selectedDirection = dir">
            <span class="dir-index">{{ i + 1 }}</span>
            <span class="dir-text">{{ dir }}</span>
          </div>
        </div>

        <div v-if="suggestions.length" class="note-section">
          <label class="note-label">补充说明</label>
          <textarea v-model="userNote" class="note-input" rows="2" :placeholder="isArticle ? '补充你希望强调的角度、卖点或标题方向...' : '补充你希望的走向...'"></textarea>
        </div>
      </div>

      <div class="picker-footer">
        <button class="btn-cancel" @click="emit('cancel')">取消</button>
        <button class="btn-confirm" :disabled="!selectedDirection" @click="handleConfirm">{{ isArticle ? '确认生成' : '确认续写' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.picker-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 520px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.picker-header {
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
}
.picker-header h3 {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text);
  margin: 0;
}
.picker-subtitle {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: var(--sp-1) 0 0;
}
.picker-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-height: 0;
}

.loading-hint {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-4);
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.direction-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.direction-item {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
}
.direction-item:hover { background: var(--bg-hover); }
.direction-item.selected {
  border-color: var(--accent);
  background: var(--accent-subtle);
}
.dir-index {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--bg);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.direction-item.selected .dir-index {
  background: var(--accent);
  color: var(--text-inverse);
  border-color: var(--accent);
}
.dir-text {
  font-size: var(--text-sm);
  color: var(--text);
  line-height: 1.5;
}
.direction-item.selected .dir-text { color: var(--accent); }

.note-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.note-label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-secondary);
}
.note-input {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  resize: vertical;
  transition: border-color var(--transition);
}
.note-input:focus { border-color: var(--border-focus); outline: none; }

.picker-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border);
}
.btn-cancel {
  padding: var(--sp-2) var(--sp-4);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-cancel:hover { background: var(--bg-hover); }
.btn-confirm {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition);
}
.btn-confirm:hover { background: var(--accent-hover); }
.btn-confirm:disabled { opacity: 0.5; cursor: not-allowed; }

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
