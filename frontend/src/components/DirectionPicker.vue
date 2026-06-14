<script setup lang="ts">
import { ref, watch } from 'vue'

interface DirectionOption {
  id: string
  title: string
  description: string
  risk: string
}

const props = defineProps<{
  options: DirectionOption[]
  loading?: boolean
}>()

const emit = defineEmits<{
  confirm: [directionId: string, directionTitle: string, userNote: string]
  cancel: []
  skip: []
}>()

const selectedId = ref(props.options[0]?.id ?? '')
const userNote = ref('')

// 当 options 从异步加载完成后自动选中第一项
watch(
  () => props.options,
  (opts) => {
    if (opts.length && !opts.find(o => o.id === selectedId.value)) {
      selectedId.value = opts[0].id
    }
  },
)

function handleConfirm() {
  const selected = props.options.find(o => o.id === selectedId.value)
  const fullDirection = selected
    ? `【${selected.title}】${selected.description}${selected.risk ? ` 风险：${selected.risk}` : ''}`
    : ''
  emit('confirm', selectedId.value, fullDirection, userNote.value)
}
</script>

<template>
  <div class="picker-overlay" @click.self="emit('cancel')">
    <div class="picker-card">
      <div class="picker-header">
        <h3>选择剧情走向</h3>
        <p class="picker-subtitle">AI 根据当前章节资料给出了以下走向，选择一个后确认</p>
      </div>

      <div class="picker-body">
        <div v-if="loading" class="loading-hint">
          <span class="spinner"></span>
          <span>AI 正在分析章节资料...</span>
        </div>
        <div v-else-if="!options.length" class="error-hint">
          <p>走向生成失败，可能是模型未就绪或网络问题。</p>
          <button class="btn-skip" @click="emit('skip')">跳过走向，直接生成</button>
        </div>
        <div v-else class="direction-list">
          <div
            v-for="opt in options"
            :key="opt.id"
            class="direction-card"
            :class="{ selected: selectedId === opt.id }"
            @click="selectedId = opt.id"
          >
            <div class="dir-header">
              <span class="dir-id">{{ opt.id }}</span>
              <span class="dir-title">{{ opt.title }}</span>
            </div>
            <p class="dir-desc">{{ opt.description }}</p>
            <p v-if="opt.risk" class="dir-risk">⚠ {{ opt.risk }}</p>
          </div>
        </div>

        <div v-if="options.length" class="note-section">
          <label class="note-label">补充要求</label>
          <textarea
            v-model="userNote"
            class="note-input"
            rows="2"
            placeholder="例如：我要 A + C，但结尾留悬念..."
          ></textarea>
        </div>
      </div>

      <div class="picker-footer">
        <button class="btn-cancel" @click="emit('cancel')">取消</button>
        <button class="btn-confirm" :disabled="!selectedId" @click="handleConfirm">确认走向</button>
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
  max-width: 540px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.picker-header {
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
}
.picker-header h3 { margin: 0 0 4px 0; font-size: var(--text-lg); }
.picker-subtitle { margin: 0; font-size: var(--text-sm); color: var(--text-secondary); }
.picker-body {
  padding: var(--sp-4) var(--sp-5);
  overflow-y: auto;
  flex: 1;
}
.loading-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: var(--sp-4);
  color: var(--text-secondary);
}
.error-hint {
  text-align: center;
  padding: var(--sp-6) var(--sp-4);
  color: var(--text-secondary);
}
.error-hint p { margin: 0 0 16px 0; font-size: var(--text-sm); }
.btn-skip {
  padding: 10px 24px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--accent);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.btn-skip:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.spinner {
  width: 16px; height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.direction-list { display: flex; flex-direction: column; gap: 10px; }
.direction-card {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.direction-card:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 6%, transparent); }
.direction-card.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }
.dir-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.dir-id {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px; height: 24px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}
.dir-title { font-weight: 650; font-size: var(--text-base); }
.dir-desc { margin: 0 0 4px 0; font-size: var(--text-sm); color: var(--text-secondary); line-height: 1.5; }
.dir-risk { margin: 0; font-size: 12px; color: var(--text-tertiary, #768390); }
.note-section { margin-top: 16px; }
.note-label { display: block; font-size: var(--text-sm); font-weight: 600; margin-bottom: 4px; }
.note-input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  font-size: var(--text-sm);
  resize: vertical;
  font-family: inherit;
}
.note-input:focus { outline: none; border-color: var(--accent); }
.picker-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border);
}
.btn-cancel {
  padding: 8px 20px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}
.btn-cancel:hover { background: color-mix(in srgb, var(--border) 20%, transparent); }
.btn-confirm {
  padding: 8px 20px;
  border: none;
  border-radius: var(--radius);
  background: var(--accent);
  color: #fff;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.btn-confirm:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-confirm:not(:disabled):hover { filter: brightness(1.1); }
</style>
