<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useChapterStore, useUiStore, friendlyError } from '../stores'
import type { ProjectMode } from '../api/types'

const props = defineProps<{
  projectId: string
  mode: ProjectMode
}>()

const store = useChapterStore()
const ui = useUiStore()
const chapter = computed(() => store.currentChapterForProject(props.projectId))
const saving = computed(() => store.saving)
const saveError = ref('')
const dirty = ref(false)

async function handleSave() {
  saveError.value = ''
  try {
    await store.saveCurrentChapter(props.projectId)
    dirty.value = false
    ui.showToast('保存成功', 'success')
  } catch (e: unknown) {
    const msg = friendlyError(e, '保存失败')
    saveError.value = msg
    ui.showToast(msg, 'error')
  }
}

// Debounced auto-save (5 seconds after last edit)
let autoSaveTimer: ReturnType<typeof setTimeout> | null = null

function scheduleAutoSave() {
  dirty.value = true
  if (autoSaveTimer) clearTimeout(autoSaveTimer)
  autoSaveTimer = setTimeout(() => {
    handleSave()
  }, 5000)
}

function cancelAutoSave() {
  if (autoSaveTimer) {
    clearTimeout(autoSaveTimer)
    autoSaveTimer = null
  }
}

onUnmounted(() => cancelAutoSave())

// Detect external draft changes (e.g. accept_with_mods from AgentPanel)
let lastKnownDraft = ''
let lastChapterId = ''
watch(chapter, (ch) => {
  if (!ch) return
  // Reset tracking when switching chapters
  if (ch.id !== lastChapterId) {
    lastChapterId = ch.id
    lastKnownDraft = ch.draft
    return
  }
  if (ch.draft !== lastKnownDraft) {
    lastKnownDraft = ch.draft
    dirty.value = true
  }
}, { immediate: true })

// Warn before leaving with unsaved changes
onBeforeRouteLeave(() => {
  if (dirty.value) {
    const confirmed = window.confirm('有未保存的内容，确定离开吗？')
    if (!confirmed) return false
  }
  cancelAutoSave()
  return true
})

// Font switcher (novel mode)
const FONT_OPTIONS = [
  { label: '衬线', value: 'var(--font-serif)' },
  { label: '宋体', value: "'Songti SC', 'SimSun', serif" },
  { label: '黑体', value: "'PingFang SC', 'Microsoft YaHei', sans-serif" },
  { label: '等宽', value: 'var(--font-mono)' },
]
const selectedFont = ref('var(--font-serif)')

onMounted(() => {
  const saved = localStorage.getItem('novel_font_family')
  if (saved) selectedFont.value = saved
})

function setFont(value: string) {
  selectedFont.value = value
  localStorage.setItem('novel_font_family', value)
}

// Rich text toolbar (article mode)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

function wrapSelection(before: string, after: string) {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const end = el.selectionEnd
  const text = el.value
  const selected = text.slice(start, end)
  const replacement = before + (selected || '文本') + after
  const newText = text.slice(0, start) + replacement + text.slice(end)
  store.updateDraft(props.projectId, newText)
  scheduleAutoSave()
  requestAnimationFrame(() => {
    el.focus()
    el.setSelectionRange(start + before.length, start + before.length + (selected || '文本').length)
  })
}

function insertBold() { wrapSelection('**', '**') }
function insertItalic() { wrapSelection('*', '*') }

function insertLink() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const end = el.selectionEnd
  const text = el.value
  const selected = text.slice(start, end) || '链接文字'
  const replacement = `[${selected}](https://example.com)`
  const newText = text.slice(0, start) + replacement + text.slice(end)
  store.updateDraft(props.projectId, newText)
  scheduleAutoSave()
  requestAnimationFrame(() => {
    el.focus()
    el.setSelectionRange(start + 1, start + 1 + selected.length)
  })
}

function insertImage() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const text = el.value
  const replacement = `![描述](https://example.com/image.jpg)`
  const newText = text.slice(0, start) + replacement + text.slice(start)
  store.updateDraft(props.projectId, newText)
  scheduleAutoSave()
  requestAnimationFrame(() => {
    el.focus()
    el.setSelectionRange(start + 2, start + 4)
  })
}

function onTextareaInput(e: Event) {
  store.updateDraft(props.projectId, (e.target as HTMLTextAreaElement).value)
  scheduleAutoSave()
}
</script>

<template>
  <div class="novel-editor">
    <div class="editor-header">
      <span v-if="chapter" class="editor-title">
        第{{ chapter.chapter_num }}章 · {{ chapter.title }}
      </span>
      <span v-if="chapter" class="editor-status" :class="chapter.status">
        {{ chapter.status === 'final' ? '终稿' : chapter.status === 'reviewing' ? '审核中' : '草稿' }}
      </span>
      <span v-if="!chapter" class="editor-title">选择一个章节开始写作</span>

      <!-- Save button -->
      <button v-if="chapter" class="btn-save" :disabled="saving" @click="handleSave">
        {{ saving ? '保存中...' : '保存' }}
      </button>
      <span v-if="dirty && !saving" class="dirty-indicator">未保存</span>
      <span v-if="saveError" class="save-error">{{ saveError }}</span>

      <!-- Font switcher (novel mode only) -->
      <div v-if="mode === 'novel' && chapter" class="font-switcher">
        <select v-model="selectedFont" @change="setFont(selectedFont)" class="font-select">
          <option v-for="f in FONT_OPTIONS" :key="f.value" :value="f.value">{{ f.label }}</option>
        </select>
      </div>

      <!-- Rich text toolbar (article mode only) -->
      <div v-if="mode === 'article' && chapter" class="rich-toolbar">
        <button class="tb-btn" @click="insertBold" title="加粗">B</button>
        <button class="tb-btn tb-italic" @click="insertItalic" title="斜体">I</button>
        <button class="tb-btn" @click="insertLink" title="插入链接">🔗</button>
        <button class="tb-btn" @click="insertImage" title="插入图片">🖼</button>
        <span class="tb-label">富文本快捷工具</span>
      </div>
    </div>
    <div class="editor-body">
      <textarea
        v-if="chapter"
        ref="textareaRef"
        class="editor-textarea"
        :style="{ fontFamily: mode === 'novel' ? selectedFont : 'var(--font-sans)' }"
        :value="chapter.draft || chapter.final_text"
        @input="onTextareaInput"
        placeholder="章节正文..."
      />
      <div v-else class="editor-empty">
        <p>从左侧选择章节，或点击"生成"让 AI 开始创作。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.novel-editor {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.editor-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-5);
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.editor-title {
  font-weight: 600;
  font-size: var(--text-base);
  color: var(--text);
}
.editor-status {
  font-size: 10px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 8px;
}
.editor-status.draft { background: var(--status-draft-bg); color: var(--status-draft); }
.editor-status.reviewing { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.editor-status.final { background: var(--status-final-bg); color: var(--status-final); }

/* Save button */
.btn-save {
  margin-left: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 500;
  cursor: pointer;
  transition: opacity var(--transition);
}
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-save:hover:not(:disabled) { opacity: 0.9; }
.dirty-indicator {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  margin-left: var(--sp-1);
}
.save-error {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  margin-left: var(--sp-2);
}

/* Font switcher */
.font-switcher {
  margin-left: auto;
}
.font-select {
  font-size: var(--text-xs);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-secondary);
  cursor: pointer;
}

/* Rich text toolbar */
.rich-toolbar {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}
.tb-btn {
  padding: var(--sp-1) var(--sp-2);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
  line-height: 1;
}
.tb-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
}
.tb-italic { font-style: italic; }
.tb-label {
  font-size: 10px;
  color: var(--text-tertiary);
  margin-left: var(--sp-1);
}

.editor-body {
  flex: 1;
  overflow: hidden;
}
.editor-textarea {
  width: 100%;
  height: 100%;
  padding: var(--sp-6) var(--sp-8);
  border: none;
  resize: none;
  font-size: var(--text-lg);
  line-height: 1.9;
  color: var(--text);
  background: var(--bg-panel);
  outline: none;
}
.editor-textarea:focus {
  background: #fdfdfd;
}
.editor-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}
</style>