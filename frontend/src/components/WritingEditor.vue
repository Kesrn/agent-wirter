<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useChapterStore, useDocumentStore, useUiStore, useChapterVersionStore, useDocumentRevisionStore, useGenerationHistoryStore, friendlyError } from '../stores'
import type { ProjectMode, WritingUnit } from '../api/types'
import { api } from '../api/client'
import VersionHistoryPanel from './VersionHistoryPanel.vue'
import DocumentRevisionPanel from './DocumentRevisionPanel.vue'
import GenerationHistoryPanel from './GenerationHistoryPanel.vue'
import VersionDiffViewer from './VersionDiffViewer.vue'
import BaseSelect from './BaseSelect.vue'
import { renderMarkdown } from '../utils/markdown'
import { formatNovelChapterTitle } from '../utils/chapterTitle'

const props = defineProps<{
  projectId: string
  mode: ProjectMode
}>()

const chapterStore = useChapterStore()
const documentStore = useDocumentStore()
const ui = useUiStore()
const versionStore = useChapterVersionStore()
const revisionStore = useDocumentRevisionStore()
const generationHistoryStore = useGenerationHistoryStore()
const currentUnit = computed(() => props.mode === 'article' ? documentStore.currentDocumentForProject(props.projectId) : chapterStore.currentChapterForProject(props.projectId))
const currentDraftText = computed(() => currentUnit.value?.draft ?? currentUnit.value?.final_text ?? '')
const currentWordCount = computed(() => countWritingWords(currentDraftText.value))
const saving = computed(() => props.mode === 'article' ? documentStore.saving : chapterStore.saving)
const unitLabel = computed(() => props.mode === 'article' ? '稿件' : '章节')
const diffResult = computed(() => showGenerationPanel.value ? generationHistoryStore.diffResult : (props.mode === 'article' ? revisionStore.diffResult : versionStore.diffResult))
const saveError = ref('')
const dirty = ref(false)

const showVersionPanel = ref(false)
const showGenerationPanel = ref(false)
const showDiffViewer = ref(false)
const articleViewMode = ref<'edit' | 'preview' | 'split'>('edit')
const canUseFormatting = computed(() => props.mode === 'article' && articleViewMode.value !== 'preview')
const articlePreviewEmpty = computed(() => !currentDraftText.value.trim())
const articlePreviewHtml = computed(() => articlePreviewEmpty.value ? '' : renderMarkdown(currentDraftText.value))

function countWritingWords(text: string): number {
  return text.replace(/\s+/g, '').length
}

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

function editorTitle(unit: WritingUnit): string {
  if (props.mode === 'article') return unit.title
  return formatNovelChapterTitle(unitPosition(unit), unit.title)
}

function toggleVersionPanel() {
  showVersionPanel.value = !showVersionPanel.value
  if (showVersionPanel.value) showGenerationPanel.value = false
}

function toggleGenerationPanel() {
  showGenerationPanel.value = !showGenerationPanel.value
  if (showGenerationPanel.value) showVersionPanel.value = false
}

function updateCurrentDraft(text: string) {
  if (props.mode === 'article') {
    documentStore.updateDraft(props.projectId, text)
  } else {
    chapterStore.updateDraft(props.projectId, text)
  }
}

async function handleCompareWithCurrent(versionId: string, versionNumber: number) {
  if (!currentUnit.value) return
  const currentContent = currentUnit.value.draft ?? currentUnit.value.final_text ?? ''
  if (props.mode === 'article') {
    await revisionStore.loadDiffWithCurrent(props.projectId, currentUnit.value.id, versionId, versionNumber, currentContent)
  } else {
    await versionStore.loadDiffWithCurrent(props.projectId, unitPosition(currentUnit.value), versionId, versionNumber, currentContent)
  }
  if (diffResult.value) {
    showDiffViewer.value = true
  }
}

function handleCloseDiff() {
  showDiffViewer.value = false
}

async function handleCompareGenerationWithCurrent(recordId: string) {
  if (!currentUnit.value) return
  const currentContent = currentUnit.value.draft ?? currentUnit.value.final_text ?? ''
  await generationHistoryStore.loadDiffWithCurrent(props.projectId, recordId, currentContent)
  if (generationHistoryStore.diffResult) {
    showDiffViewer.value = true
  }
}

async function handleApplyGeneration(recordId: string, content: string) {
  updateCurrentDraft(content)
  dirty.value = true
  await generationHistoryStore.updateRecordStatus(props.projectId, recordId, 'applied')
  ui.showToast('已应用到编辑器，请确认后保存', 'success')
  nextTick(syncTextareaHeight)
}

async function handleRestoreVersion(_versionId: string, content: string) {
  if (!currentUnit.value) return
  updateCurrentDraft(content)
  dirty.value = true
  try {
    if (props.mode === 'article') {
      await documentStore.saveCurrentDocument(props.projectId)
    } else {
      await chapterStore.saveCurrentChapter(props.projectId)
    }
    dirty.value = false
    ui.showToast(props.mode === 'article' ? '已恢复到修订版本' : '已恢复到历史版本', 'success')
    if (props.mode === 'article') {
      revisionStore.loadRevisions(props.projectId, currentUnit.value.id)
    } else {
      versionStore.loadVersions(props.projectId, unitPosition(currentUnit.value))
    }
  } catch (e: unknown) {
    const msg = friendlyError(e, '恢复后保存失败')
    saveError.value = msg
    ui.showToast(msg, 'error')
  }
}

async function handleSave(source: 'manual' | 'auto' = 'manual') {
  if (source === 'auto' && !dirty.value) return
  saveError.value = ''
  const draft = currentUnit.value?.draft ?? currentUnit.value?.final_text ?? ''
  if (!draft.trim()) {
    if (source === 'auto') return
    const msg = `正文为空，未保存。请先输入内容或生成${unitLabel.value}。`
    saveError.value = msg
    ui.showToast(msg, 'info')
    return
  }
  try {
    if (props.mode === 'article') {
      await documentStore.saveCurrentDocument(props.projectId)
    } else {
      await chapterStore.saveCurrentChapter(props.projectId)
    }
    dirty.value = false
    cancelAutoSave()
    ui.showToast(source === 'auto' ? '自动保存成功' : '保存成功', 'success')
    if (currentUnit.value) {
      if (props.mode === 'article') {
        revisionStore.loadRevisions(props.projectId, currentUnit.value.id)
      } else {
        versionStore.loadVersions(props.projectId, unitPosition(currentUnit.value))
      }
    }
  } catch (e: unknown) {
    const msg = friendlyError(e, source === 'auto' ? '自动保存失败' : '保存失败')
    saveError.value = msg
    ui.showToast(msg, 'error')
  }
}

async function handleManualSave() {
  await handleSave('manual')
}

// Debounced auto-save (30 seconds after last edit)
let autoSaveTimer: ReturnType<typeof setTimeout> | null = null

function scheduleAutoSave() {
  dirty.value = true
  if (autoSaveTimer) clearTimeout(autoSaveTimer)
  autoSaveTimer = setTimeout(() => {
    handleSave('auto')
  }, 30000)
}

function cancelAutoSave() {
  if (autoSaveTimer) {
    clearTimeout(autoSaveTimer)
    autoSaveTimer = null
  }
}

onUnmounted(() => cancelAutoSave())

function syncTextareaHeight() {
  const el = textareaRef.value
  const parent = el?.parentElement
  if (!el || !parent) return

  const parentStyle = window.getComputedStyle(parent)
  const verticalPadding =
    parseFloat(parentStyle.paddingTop || '0') + parseFloat(parentStyle.paddingBottom || '0')
  if (props.mode === 'novel') {
    const fillHeight = Math.max(360, parent.clientHeight - verticalPadding)
    el.style.height = `${fillHeight}px`
    return
  }

  const minHeight = Math.max(320, parent.clientHeight - verticalPadding)
  el.style.height = 'auto'
  el.style.height = `${Math.max(minHeight, el.scrollHeight)}px`
}

// Detect external draft changes (for example, applying an AI candidate from AgentPanel)
let lastKnownDraft = ''
let lastUnitId = ''
watch(currentUnit, (unit) => {
  if (!unit) return
  // Reset tracking when switching writing units
  if (unit.id !== lastUnitId) {
    lastUnitId = unit.id
    lastKnownDraft = unit.draft
    versionStore.clearState()
    revisionStore.clearState()
    generationHistoryStore.clearState()
    showDiffViewer.value = false
    showGenerationPanel.value = false
    nextTick(syncTextareaHeight)
    return
  }
  if (unit.draft !== lastKnownDraft) {
    lastKnownDraft = unit.draft
    dirty.value = true
    nextTick(syncTextareaHeight)
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
const HEADING_OPTIONS = [
  { label: '正文', value: 'paragraph' },
  { label: 'H1', value: '1' },
  { label: 'H2', value: '2' },
  { label: 'H3', value: '3' },
]
const selectedFont = ref('var(--font-serif)')

onMounted(() => {
  const saved = localStorage.getItem('novel_font_family')
  if (saved) selectedFont.value = saved
  const savedArticleView = localStorage.getItem('article_markdown_view')
  if (isArticleViewMode(savedArticleView)) articleViewMode.value = savedArticleView
  window.addEventListener('resize', syncTextareaHeight)
  nextTick(syncTextareaHeight)
})
onUnmounted(() => window.removeEventListener('resize', syncTextareaHeight))

function setFont(value: string) {
  selectedFont.value = value
  localStorage.setItem('novel_font_family', value)
}

function setArticleViewMode(mode: 'edit' | 'preview' | 'split') {
  articleViewMode.value = mode
  localStorage.setItem('article_markdown_view', mode)
  nextTick(syncTextareaHeight)
}

function isArticleViewMode(value: string | null): value is 'edit' | 'preview' | 'split' {
  return value === 'edit' || value === 'preview' || value === 'split'
}

// Rich text toolbar (article mode)
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const editorHistory = ref<string[]>([])
const editorRedoStack = ref<string[]>([])
const imageInputRef = ref<HTMLInputElement | null>(null)
const linkUrlInputRef = ref<HTMLInputElement | null>(null)
const imageUploading = ref(false)
const showLinkDialog = ref(false)
const linkText = ref('')
const linkUrl = ref('')
const linkError = ref('')
const MAX_HISTORY = 80
const TYPING_GROUP_DELAY = 1000
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const ALLOWED_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const isComposing = ref(false)
const compositionBaseText = ref('')
let pendingLinkSelection: { start: number; end: number } | null = null
let pendingImageSelection: { start: number; end: number; alt: string } | null = null
let typingSnapshot: string | null = null
let typingSnapshotTimer: ReturnType<typeof setTimeout> | null = null

function currentEditorText(): string {
  return currentDraftText.value
}

function snapshotEditorScroll(el: HTMLTextAreaElement) {
  const scroller = el.closest('.editor-content') as HTMLElement | null
  return {
    scroller,
    scrollTop: scroller?.scrollTop ?? 0,
    scrollLeft: scroller?.scrollLeft ?? 0,
    textareaScrollTop: el.scrollTop,
    textareaScrollLeft: el.scrollLeft,
    windowX: window.scrollX,
    windowY: window.scrollY,
  }
}

function restoreEditorScroll(snapshot: ReturnType<typeof snapshotEditorScroll>, el?: HTMLTextAreaElement | null) {
  if (snapshot.scroller) {
    snapshot.scroller.scrollTop = snapshot.scrollTop
    snapshot.scroller.scrollLeft = snapshot.scrollLeft
  }
  if (el) {
    el.scrollTop = snapshot.textareaScrollTop
    el.scrollLeft = snapshot.textareaScrollLeft
  }
  window.scrollTo(snapshot.windowX, snapshot.windowY)
}

function pushEditorHistory(previousText: string) {
  const last = editorHistory.value[editorHistory.value.length - 1]
  if (last !== previousText) {
    editorHistory.value.push(previousText)
    if (editorHistory.value.length > MAX_HISTORY) editorHistory.value.shift()
  }
  editorRedoStack.value = []
}

function clearTypingSnapshot() {
  typingSnapshot = null
  if (typingSnapshotTimer) {
    clearTimeout(typingSnapshotTimer)
    typingSnapshotTimer = null
  }
}

function markTypingHistory(previousText: string) {
  if (typingSnapshot === null) {
    typingSnapshot = previousText
    pushEditorHistory(previousText)
  }
  if (typingSnapshotTimer) clearTimeout(typingSnapshotTimer)
  typingSnapshotTimer = setTimeout(() => {
    typingSnapshot = null
    typingSnapshotTimer = null
  }, TYPING_GROUP_DELAY)
}

function applyEditorText(
  nextText: string,
  selectionStart: number,
  selectionEnd: number = selectionStart,
  recordHistory = true,
) {
  const previousText = currentEditorText()
  clearTypingSnapshot()
  if (recordHistory && nextText !== previousText) {
    pushEditorHistory(previousText)
  }
  updateCurrentDraft(nextText)
  scheduleAutoSave()
  const scrollSnapshot = textareaRef.value ? snapshotEditorScroll(textareaRef.value) : null
  requestAnimationFrame(() => {
    const el = textareaRef.value
    syncTextareaHeight()
    if (!el) return
    el.focus({ preventScroll: true })
    el.setSelectionRange(selectionStart, selectionEnd)
    if (scrollSnapshot) restoreEditorScroll(scrollSnapshot, el)
  })
}

function wrapSelection(before: string, after: string) {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const end = el.selectionEnd
  const text = el.value
  const selected = text.slice(start, end)
  const replacement = before + (selected || '文本') + after
  const newText = text.slice(0, start) + replacement + text.slice(end)
  applyEditorText(newText, start + before.length, start + before.length + (selected || '文本').length)
}

function insertBold() { wrapSelection('**', '**') }
function insertItalic() { wrapSelection('*', '*') }
function insertStrike() { wrapSelection('~~', '~~') }
function insertInlineCode() { wrapSelection('`', '`') }

function selectedLineRange(text: string, start: number, end: number) {
  const lineStart = text.lastIndexOf('\n', Math.max(0, start - 1)) + 1
  const lineEndIndex = text.indexOf('\n', end)
  const lineEnd = lineEndIndex === -1 ? text.length : lineEndIndex
  return {
    lineStart,
    lineEnd,
    block: text.slice(lineStart, lineEnd),
  }
}

function transformSelectedLines(transform: (line: string, index: number) => string) {
  const el = textareaRef.value
  if (!el) return
  const text = el.value
  const { lineStart, lineEnd, block } = selectedLineRange(text, el.selectionStart, el.selectionEnd)
  const transformed = block.split('\n').map(transform).join('\n')
  const nextText = text.slice(0, lineStart) + transformed + text.slice(lineEnd)
  applyEditorText(nextText, lineStart, lineStart + transformed.length)
}

function setHeading(level: number | null) {
  transformSelectedLines((line) => {
    const clean = line.replace(/^#{1,6}\s+/, '')
    return level ? `${'#'.repeat(level)} ${clean || '标题'}` : clean
  })
}

function handleHeadingSelect(value: string) {
  if (value === 'paragraph') setHeading(null)
  else setHeading(Number(value))
}

function insertQuote() {
  transformSelectedLines(line => line.startsWith('> ') ? line.slice(2) : `> ${line || '引用内容'}`)
}

function insertBulletList() {
  transformSelectedLines(line => /^[-*+]\s+/.test(line) ? line.replace(/^[-*+]\s+/, '') : `- ${line || '列表项'}`)
}

function insertNumberedList() {
  transformSelectedLines((line, index) => /^\d+\.\s+/.test(line) ? line.replace(/^\d+\.\s+/, '') : `${index + 1}. ${line || '列表项'}`)
}

function insertTaskList() {
  transformSelectedLines(line => /^-\s+\[[ xX]\]\s+/.test(line) ? line.replace(/^-\s+\[[ xX]\]\s+/, '') : `- [ ] ${line || '待办事项'}`)
}

function indentLines() {
  transformSelectedLines(line => line ? `  ${line}` : line)
}

function outdentLines() {
  transformSelectedLines(line => line.replace(/^( {1,2}|\t)/, ''))
}

function insertCodeBlock() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const end = el.selectionEnd
  const text = el.value
  const selected = text.slice(start, end) || '代码'
  const replacement = `\n\`\`\`\n${selected}\n\`\`\`\n`
  const nextText = text.slice(0, start) + replacement + text.slice(end)
  const contentStart = start + 5
  applyEditorText(nextText, contentStart, contentStart + selected.length)
}

function insertDivider() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const text = el.value
  const replacement = `${start > 0 && text[start - 1] !== '\n' ? '\n\n' : ''}---\n\n`
  const nextText = text.slice(0, start) + replacement + text.slice(start)
  applyEditorText(nextText, start + replacement.length)
}

function insertTable() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const text = el.value
  const replacement = [
    '',
    '| 标题 | 说明 |',
    '| --- | --- |',
    '| 内容 | 备注 |',
    '',
  ].join('\n')
  const nextText = text.slice(0, start) + replacement + text.slice(el.selectionEnd)
  applyEditorText(nextText, start + 3, start + 5)
}

function insertLink() {
  const el = textareaRef.value
  if (!el) return
  const start = el.selectionStart
  const end = el.selectionEnd
  const text = el.value
  const selected = text.slice(start, end).trim()
  const existingLink = parseMarkdownLink(selected)
  pendingLinkSelection = { start, end }
  linkText.value = existingLink?.label ?? (looksLikeLinkUrl(selected) ? selected : selected || '链接文字')
  linkUrl.value = existingLink?.url ?? (looksLikeLinkUrl(selected) ? selected : '')
  linkError.value = ''
  showLinkDialog.value = true
  nextTick(() => {
    linkUrlInputRef.value?.focus()
    linkUrlInputRef.value?.select()
  })
}

function parseMarkdownLink(value: string): { label: string; url: string } | null {
  const match = value.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
  if (!match) return null
  return { label: match[1], url: match[2] }
}

function looksLikeLinkUrl(value: string): boolean {
  const trimmed = value.trim()
  return /^(https?:\/\/|mailto:|tel:|\/|#|\.{1,2}\/)/i.test(trimmed) || /^www\./i.test(trimmed) || /^[^\s/]+\.[^\s]+/.test(trimmed)
}

function normalizeLinkUrl(value: string): string {
  const trimmed = value.trim()
  if (!trimmed || /[\u0000-\u001F\u007F\s]/.test(trimmed)) return ''
  const explicitProtocol = trimmed.match(/^([a-z][a-z0-9+.-]*):/i)
  if (explicitProtocol) {
    return /^(https?|mailto|tel)$/i.test(explicitProtocol[1]) ? trimmed : ''
  }
  if (/^(\/|#|\.{1,2}\/)/.test(trimmed)) return trimmed
  if (/^www\./i.test(trimmed) || /^[^\s/]+\.[^\s]+/.test(trimmed)) return `https://${trimmed}`
  return `./${trimmed.replace(/^\/+/, '')}`
}

function escapeMarkdownLinkText(value: string): string {
  return value.replace(/\s+/g, ' ').trim().replace(/[\\[\]]/g, '\\$&')
}

function escapeMarkdownLinkUrl(value: string): string {
  return value.replace(/\(/g, '%28').replace(/\)/g, '%29')
}

function closeLinkDialog() {
  showLinkDialog.value = false
  linkError.value = ''
  pendingLinkSelection = null
  textareaRef.value?.focus({ preventScroll: true })
}

function confirmLinkInsert() {
  const selection = pendingLinkSelection
  if (!selection) return
  const label = escapeMarkdownLinkText(linkText.value)
  const url = normalizeLinkUrl(linkUrl.value)
  if (!label) {
    linkError.value = '请输入链接文字'
    return
  }
  if (!url) {
    linkError.value = '请输入有效链接'
    return
  }
  const escapedUrl = escapeMarkdownLinkUrl(url)
  const source = currentEditorText()
  const replacement = `[${label}](${escapedUrl})`
  const nextText = source.slice(0, selection.start) + replacement + source.slice(selection.end)
  showLinkDialog.value = false
  linkError.value = ''
  pendingLinkSelection = null
  applyEditorText(nextText, selection.start + replacement.length)
}

function insertImage() {
  const el = textareaRef.value
  if (!el) return
  pendingImageSelection = {
    start: el.selectionStart,
    end: el.selectionEnd,
    alt: el.value.slice(el.selectionStart, el.selectionEnd).trim() || '图片',
  }
  imageInputRef.value?.click()
}

function imageAltFromFile(file: File): string {
  const base = file.name.replace(/\.[^.]+$/, '').trim()
  return base || '图片'
}

function escapeMarkdownImageAlt(value: string): string {
  const normalized = value.replace(/\s+/g, ' ').trim() || '图片'
  return normalized.replace(/[\\[\]]/g, '\\$&')
}

async function handleImageSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return

  if (!ALLOWED_IMAGE_TYPES.has(file.type)) {
    ui.showToast('仅支持 PNG、JPEG、GIF、WebP 图片', 'error')
    return
  }
  if (file.size > MAX_IMAGE_BYTES) {
    ui.showToast('图片不能超过 5MB', 'error')
    return
  }

  imageUploading.value = true
  try {
    const uploaded = await api.uploadProjectImage(props.projectId, file)
    const el = textareaRef.value
    const selection = pendingImageSelection ?? {
      start: currentEditorText().length,
      end: currentEditorText().length,
      alt: imageAltFromFile(file),
    }
    const text = currentEditorText()
    const alt = escapeMarkdownImageAlt(selection.alt === '图片' ? imageAltFromFile(file) : selection.alt)
    const prefix = selection.start > 0 && text[selection.start - 1] !== '\n' ? '\n\n' : ''
    const suffix = selection.end < text.length && text[selection.end] !== '\n' ? '\n\n' : ''
    const replacement = `${prefix}![${alt}](${uploaded.url})${suffix}`
    const nextText = text.slice(0, selection.start) + replacement + text.slice(selection.end)
    applyEditorText(nextText, selection.start + replacement.length, selection.start + replacement.length)
    ui.showToast('图片已插入', 'success')
    if (!el) nextTick(syncTextareaHeight)
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '图片上传失败'), 'error')
  } finally {
    imageUploading.value = false
    pendingImageSelection = null
  }
}

function clearFormatting() {
  const el = textareaRef.value
  if (!el) return
  const text = el.value
  const start = el.selectionStart
  const end = el.selectionEnd
  const hasSelection = end > start
  const range = hasSelection ? { lineStart: start, lineEnd: end, block: text.slice(start, end) } : selectedLineRange(text, start, end)
  const cleaned = range.block
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^>\s?/gm, '')
    .replace(/^\s*-\s+\[[ xX]\]\s+/gm, '')
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, '')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/(\*|_)(.*?)\1/g, '$2')
    .replace(/~~(.*?)~~/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
  const nextText = text.slice(0, range.lineStart) + cleaned + text.slice(range.lineEnd)
  applyEditorText(nextText, range.lineStart, range.lineStart + cleaned.length)
}

function undoEdit() {
  clearTypingSnapshot()
  const previous = editorHistory.value.pop()
  if (previous === undefined) return
  const current = currentEditorText()
  editorRedoStack.value.push(current)
  updateCurrentDraft(previous)
  scheduleAutoSave()
  requestAnimationFrame(() => {
    syncTextareaHeight()
    textareaRef.value?.focus()
  })
}

function redoEdit() {
  clearTypingSnapshot()
  const next = editorRedoStack.value.pop()
  if (next === undefined) return
  editorHistory.value.push(currentEditorText())
  updateCurrentDraft(next)
  scheduleAutoSave()
  requestAnimationFrame(() => {
    syncTextareaHeight()
    textareaRef.value?.focus()
  })
}

function handleEditorKeydown(event: KeyboardEvent) {
  if (props.mode === 'article' && articleViewMode.value === 'preview') return
  const mod = event.metaKey || event.ctrlKey
  if (event.key === 'Tab' && props.mode === 'article') {
    event.preventDefault()
    if (event.shiftKey) outdentLines()
    else indentLines()
    return
  }
  if (!mod || props.mode !== 'article') return

  const key = event.key.toLowerCase()
  if (key === 'b') {
    event.preventDefault()
    insertBold()
  } else if (key === 'i') {
    event.preventDefault()
    insertItalic()
  } else if (key === 'k') {
    event.preventDefault()
    insertLink()
  } else if (key === 'z') {
    event.preventDefault()
    if (event.shiftKey) redoEdit()
    else undoEdit()
  } else if (key === 'y') {
    event.preventDefault()
    redoEdit()
  }
}

function handleCompositionStart() {
  isComposing.value = true
  compositionBaseText.value = currentEditorText()
}

function handleCompositionEnd(e: CompositionEvent) {
  const nextText = (e.target as HTMLTextAreaElement).value
  isComposing.value = false
  if (nextText !== compositionBaseText.value) {
    clearTypingSnapshot()
    pushEditorHistory(compositionBaseText.value)
  }
  updateCurrentDraft(nextText)
  syncTextareaHeight()
  scheduleAutoSave()
  compositionBaseText.value = ''
}

function onTextareaInput(e: Event) {
  const previousText = currentEditorText()
  const inputEvent = e as InputEvent
  const nextText = (e.target as HTMLTextAreaElement).value
  if (!isComposing.value && !inputEvent.isComposing && nextText !== previousText) {
    markTypingHistory(previousText)
  }
  updateCurrentDraft(nextText)
  syncTextareaHeight()
  scheduleAutoSave()
}
</script>

<template>
  <div class="writing-editor">
    <div class="editor-header">
      <span v-if="currentUnit" class="editor-title">
        {{ editorTitle(currentUnit) }}
      </span>
      <span v-if="currentUnit" class="editor-status" :class="currentUnit.status">
        {{ currentUnit.status === 'final' ? '终稿' : currentUnit.status === 'reviewing' ? '审核中' : '草稿' }}
      </span>
      <span v-if="currentUnit" class="editor-word-count" aria-label="当前正文字数">
        {{ currentWordCount.toLocaleString('zh-CN') }} 字
      </span>
      <span v-if="!currentUnit" class="editor-title">选择一个{{ unitLabel }}开始写作</span>

      <!-- Save button -->
      <button v-if="currentUnit" class="btn-save" :disabled="saving" @click="handleManualSave">
        {{ saving ? '保存中...' : '保存' }}
      </button>
      <span v-if="dirty && !saving" class="dirty-indicator">未保存</span>
      <span v-if="saveError" class="save-error">{{ saveError }}</span>

      <!-- Version history toggle -->
      <button v-if="currentUnit" class="btn-version-toggle" :class="{ active: showVersionPanel }" @click="toggleVersionPanel">
        {{ mode === 'article' ? '修订历史' : '版本历史' }}
      </button>

      <button v-if="currentUnit" class="btn-version-toggle" :class="{ active: showGenerationPanel }" @click="toggleGenerationPanel">
        AI生成历史
      </button>

      <!-- Font switcher (novel mode only) -->
      <div v-if="mode === 'novel' && currentUnit" class="font-switcher">
        <BaseSelect
          v-model="selectedFont"
          class="font-select"
          :options="FONT_OPTIONS"
          compact
          @change="(value: string) => setFont(value)"
        />
      </div>

      <!-- Rich text toolbar (article mode only) -->
      <div v-if="mode === 'article' && currentUnit" class="rich-toolbar">
        <div class="view-segment" aria-label="Markdown 视图">
          <button
            class="view-segment-btn"
            :class="{ active: articleViewMode === 'edit' }"
            @click="setArticleViewMode('edit')"
          >
            编辑
          </button>
          <button
            class="view-segment-btn"
            :class="{ active: articleViewMode === 'preview' }"
            @click="setArticleViewMode('preview')"
          >
            预览
          </button>
          <button
            class="view-segment-btn"
            :class="{ active: articleViewMode === 'split' }"
            @click="setArticleViewMode('split')"
          >
            分屏
          </button>
        </div>
        <div class="tb-group">
          <button class="tb-btn" :disabled="editorHistory.length === 0" @click="undoEdit" title="撤销">↶</button>
          <button class="tb-btn" :disabled="editorRedoStack.length === 0" @click="redoEdit" title="重做">↷</button>
        </div>
        <BaseSelect
          class="tb-select"
          model-value="paragraph"
          title="段落样式"
          :options="HEADING_OPTIONS"
          :disabled="!canUseFormatting"
          compact
          @change="handleHeadingSelect"
        />
        <div class="tb-group">
          <button class="tb-btn tb-bold" :disabled="!canUseFormatting" @click="insertBold" title="加粗">B</button>
          <button class="tb-btn tb-italic" :disabled="!canUseFormatting" @click="insertItalic" title="斜体">I</button>
          <button class="tb-btn tb-strike" :disabled="!canUseFormatting" @click="insertStrike" title="删除线">S</button>
          <button class="tb-btn tb-code" :disabled="!canUseFormatting" @click="insertInlineCode" title="行内代码">`</button>
        </div>
        <div class="tb-group">
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertBulletList" title="无序列表">•</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertNumberedList" title="编号列表">1.</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertTaskList" title="待办列表">☑</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="outdentLines" title="取消缩进">⇤</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="indentLines" title="缩进">⇥</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertQuote" title="引用">“</button>
        </div>
        <div class="tb-group">
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertLink" title="插入链接">Link</button>
          <button class="tb-btn" :disabled="!canUseFormatting || imageUploading" @click="insertImage" title="插入图片">
            {{ imageUploading ? '...' : 'Img' }}
          </button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertTable" title="插入表格">▦</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertCodeBlock" title="代码块">{ }</button>
          <button class="tb-btn" :disabled="!canUseFormatting" @click="insertDivider" title="分割线">--</button>
        </div>
        <button class="tb-btn tb-clear" :disabled="!canUseFormatting" @click="clearFormatting" title="清除格式">Tx</button>
        <input
          ref="imageInputRef"
          class="image-file-input"
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          @change="handleImageSelected"
        />
      </div>
    </div>
    <div class="editor-body">
      <div
        class="editor-content"
        :class="{
          'with-panel': showVersionPanel || showGenerationPanel,
          'novel-content': mode === 'novel',
          'article-content': mode === 'article',
          'article-preview-only': mode === 'article' && articleViewMode === 'preview',
          'article-split': mode === 'article' && articleViewMode === 'split',
        }"
      >
        <template v-if="currentUnit">
          <textarea
            v-if="mode !== 'article' || articleViewMode !== 'preview'"
            ref="textareaRef"
            class="editor-textarea"
            :style="{ fontFamily: mode === 'novel' ? selectedFont : 'var(--font-sans)' }"
            :value="currentDraftText"
            @input="onTextareaInput"
            @keydown="handleEditorKeydown"
            @compositionstart="handleCompositionStart"
            @compositionend="handleCompositionEnd"
            :placeholder="`${unitLabel}正文...`"
          />
          <article
            v-if="mode === 'article' && articleViewMode !== 'edit'"
            class="markdown-preview"
            :class="{ empty: articlePreviewEmpty }"
          >
            <div v-if="articlePreviewEmpty" class="preview-empty">当前稿件为空</div>
            <div v-else class="markdown-body" v-html="articlePreviewHtml"></div>
          </article>
        </template>
        <div v-else class="editor-empty">
          <p>从左侧选择{{ unitLabel }}，或点击"生成"让 AI 开始创作。</p>
        </div>
      </div>
      <VersionHistoryPanel
        v-if="showVersionPanel && currentUnit && mode === 'novel'"
        :projectId="projectId"
        :chapterId="currentUnit.id"
        :sequenceNumber="unitPosition(currentUnit)"
        @compare-current="handleCompareWithCurrent"
        @restore="handleRestoreVersion"
      />
      <DocumentRevisionPanel
        v-if="showVersionPanel && currentUnit && mode === 'article'"
        :projectId="projectId"
        :documentId="currentUnit.id"
        @compare-current="handleCompareWithCurrent"
        @restore="handleRestoreVersion"
      />
      <GenerationHistoryPanel
        v-if="showGenerationPanel && currentUnit"
        :projectId="projectId"
        :mode="mode"
        :sequenceNumber="mode === 'novel' ? unitPosition(currentUnit) : undefined"
        :documentId="mode === 'article' ? currentUnit.id : undefined"
        @compare-current="handleCompareGenerationWithCurrent"
        @apply="handleApplyGeneration"
      />
    </div>
    <VersionDiffViewer
      v-if="showDiffViewer && diffResult"
      :leftTitle="diffResult.leftTitle"
      :rightTitle="diffResult.rightTitle"
      :diff="diffResult.diff"
      @close="handleCloseDiff"
    />
    <div v-if="showLinkDialog" class="link-dialog-overlay" @click.self="closeLinkDialog">
      <form class="link-dialog" @submit.prevent="confirmLinkInsert">
        <div class="link-dialog-header">
          <h3>插入链接</h3>
          <button type="button" class="link-dialog-close" title="关闭" @click="closeLinkDialog">×</button>
        </div>
        <label class="link-dialog-field">
          <span>链接文字</span>
          <input
            v-model="linkText"
            class="link-dialog-input"
            type="text"
            placeholder="输入显示文字"
            @keydown.escape="closeLinkDialog"
          />
        </label>
        <label class="link-dialog-field">
          <span>链接地址</span>
          <input
            ref="linkUrlInputRef"
            v-model="linkUrl"
            class="link-dialog-input"
            type="text"
            placeholder="https://example.com"
            @keydown.escape="closeLinkDialog"
          />
        </label>
        <p v-if="linkError" class="link-dialog-error">{{ linkError }}</p>
        <div class="link-dialog-actions">
          <button type="button" class="link-dialog-cancel" @click="closeLinkDialog">取消</button>
          <button type="submit" class="link-dialog-confirm">插入</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.writing-editor {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.editor-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-5);
  background: color-mix(in srgb, var(--bg-panel) 94%, transparent);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  flex-shrink: 0;
  box-shadow: 0 1px 0 rgba(17, 24, 39, 0.03);
  backdrop-filter: blur(10px);
}
.editor-title {
  font-weight: 720;
  font-size: var(--text-base);
  color: var(--text);
}
.editor-status {
  font-size: 10px;
  font-weight: 700;
  padding: 3px 8px;
  border-radius: 8px;
}
.editor-status.draft { background: var(--status-draft-bg); color: var(--status-draft); }
.editor-status.reviewing { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.editor-status.final { background: var(--status-final-bg); color: var(--status-final); }
.editor-word-count {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--bg-sidebar) 70%, transparent);
  font-size: var(--text-xs);
  font-weight: 700;
  white-space: nowrap;
}

/* Save button */
.btn-save {
  margin-left: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 8px 18px color-mix(in srgb, var(--accent) 18%, transparent);
  transition: opacity var(--transition), background var(--transition), transform var(--transition);
}
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-save:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
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

/* Version history toggle */
.btn-version-toggle {
  margin-left: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  background: var(--bg-panel);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 650;
  cursor: pointer;
  transition: all var(--transition);
  box-shadow: var(--shadow-sm);
}
.btn-version-toggle:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
}
.btn-version-toggle.active {
  background: var(--accent);
  color: var(--text-inverse);
  border-color: var(--accent);
}

/* Font switcher */
.font-switcher {
  margin-left: auto;
}
.font-select {
  width: 86px;
  min-width: 86px;
}

/* Rich text toolbar */
.rich-toolbar {
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--sp-2);
  flex: 1 1 560px;
  min-width: 280px;
}
.tb-group {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg-sidebar) 72%, var(--bg-panel));
  box-shadow: var(--shadow-sm);
}
.view-segment {
  display: inline-flex;
  align-items: center;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg-sidebar) 72%, var(--bg-panel));
  box-shadow: var(--shadow-sm);
}
.view-segment-btn {
  min-width: 44px;
  height: 26px;
  padding: 0 var(--sp-2);
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 700;
  line-height: 1;
}
.view-segment-btn:hover {
  background: var(--bg-hover);
}
.view-segment-btn.active {
  background: var(--accent);
  color: var(--text-inverse);
}
.tb-select {
  flex: 0 0 auto;
  width: 74px;
  min-width: 74px;
}
.tb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 26px;
  padding: 0;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
  line-height: 1;
  white-space: nowrap;
}
.tb-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--accent);
}
.tb-btn:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}
.tb-bold { font-weight: 800; }
.tb-italic { font-style: italic; }
.tb-strike { text-decoration: line-through; }
.tb-code { font-family: var(--font-mono); }
.tb-clear { width: 34px; border-color: var(--border); }
.image-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}
.link-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
  background: color-mix(in srgb, #0f172a 42%, transparent);
  backdrop-filter: blur(4px);
}
.link-dialog {
  width: min(420px, 100%);
  padding: var(--sp-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  box-shadow: var(--shadow-lg);
}
.link-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.link-dialog-header h3 {
  margin: 0;
  color: var(--text);
  font-size: var(--text-base);
  font-weight: 760;
}
.link-dialog-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.link-dialog-close:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.link-dialog-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-bottom: var(--sp-3);
}
.link-dialog-field span {
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 650;
}
.link-dialog-input {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text);
  font-size: var(--text-sm);
  outline: none;
}
.link-dialog-input:focus {
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}
.link-dialog-error {
  margin: 0 0 var(--sp-3);
  color: var(--status-error, #dc2626);
  font-size: var(--text-xs);
}
.link-dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
}
.link-dialog-cancel,
.link-dialog-confirm {
  height: 32px;
  padding: 0 var(--sp-4);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
}
.link-dialog-cancel {
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text-secondary);
}
.link-dialog-cancel:hover {
  background: var(--bg-hover);
}
.link-dialog-confirm {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--text-inverse);
  font-weight: 700;
}
.link-dialog-confirm:hover {
  background: var(--accent-hover);
}

.editor-body {
  flex: 1;
  overflow: hidden;
  display: flex;
  background:
    radial-gradient(circle at 50% 0, color-mix(in srgb, var(--bg-panel) 36%, transparent), transparent 44%),
    var(--paper-stage, #eceff3);
  min-height: 0;
  min-width: 0;
}
.editor-content {
  flex: 1;
  overflow: auto;
  -webkit-overflow-scrolling: touch;
  min-width: 0;
  min-height: 0;
  padding: var(--sp-6) var(--sp-6) calc(var(--sp-6) + 88px);
  scroll-padding-bottom: 96px;
}
.editor-content.novel-content {
  padding: var(--sp-4) var(--sp-5);
  scroll-padding-bottom: var(--sp-4);
}
.editor-content.with-panel {
  flex: 1;
}
.editor-content.article-content {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-5);
}
.editor-content.article-preview-only {
  display: block;
}
.editor-content.article-split {
  padding-left: var(--sp-4);
  padding-right: var(--sp-4);
}
.editor-textarea {
  display: block;
  width: min(860px, 100%);
  min-height: 320px;
  margin: 0 auto;
  padding: 56px 64px 112px;
  border: 1px solid var(--paper-border, rgba(148, 163, 184, 0.22));
  border-radius: var(--paper-radius);
  resize: none;
  overflow-y: auto;
  overflow-x: hidden;
  overflow-anchor: none;
  font-size: var(--text-lg);
  line-height: 2;
  color: var(--text);
  background:
    linear-gradient(90deg, rgba(239, 68, 68, 0.14) 0 1px, transparent 1px) 46px 0 / 1px 100% no-repeat,
    repeating-linear-gradient(to bottom, var(--paper-bg) 0, var(--paper-bg) 37px, var(--paper-line) 38px),
    var(--paper-bg);
  outline: none;
  box-shadow: var(--paper-shadow);
  caret-color: var(--accent);
}
.editor-textarea:focus {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--paper-border));
  box-shadow:
    var(--paper-shadow),
    0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent);
}
.novel-content .editor-textarea {
  padding: 36px 56px 48px;
}
.article-content .editor-textarea {
  font-size: var(--text-base);
  line-height: 1.8;
  background: var(--paper-bg);
}
.article-split .editor-textarea,
.article-split .markdown-preview {
  flex: 1 1 0;
  width: auto;
  max-width: none;
  min-height: 100%;
  min-width: 0;
  margin: 0;
}
.markdown-preview {
  width: min(860px, 100%);
  min-height: 100%;
  margin: 0 auto;
  padding: 56px 64px 112px;
  border: 1px solid var(--paper-border, rgba(148, 163, 184, 0.22));
  border-radius: var(--paper-radius);
  background: var(--paper-bg);
  color: var(--text);
  box-shadow: var(--paper-shadow);
}
.markdown-preview.empty {
  display: flex;
  align-items: center;
  justify-content: center;
}
.preview-empty {
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}
.markdown-body {
  overflow-wrap: anywhere;
  word-break: break-word;
  font-size: var(--text-base);
  line-height: 1.8;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4),
.markdown-body :deep(h5),
.markdown-body :deep(h6) {
  margin: 1.4em 0 0.65em;
  line-height: 1.35;
  font-weight: 700;
}
.markdown-body :deep(h1) {
  margin-top: 0;
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border);
  font-size: 1.65rem;
}
.markdown-body :deep(h2) {
  padding-bottom: var(--sp-2);
  border-bottom: 1px solid var(--border);
  font-size: 1.35rem;
}
.markdown-body :deep(h3) { font-size: 1.12rem; }
.markdown-body :deep(p) {
  margin: 0 0 1em;
}
.markdown-body :deep(a) {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.markdown-body :deep(strong) {
  font-weight: 700;
}
.markdown-body :deep(em) {
  font-style: italic;
}
.markdown-body :deep(del) {
  color: var(--text-tertiary);
}
.markdown-body :deep(blockquote) {
  margin: var(--sp-4) 0;
  padding: var(--sp-2) var(--sp-4);
  border-left: 3px solid var(--border-focus);
  background: color-mix(in srgb, var(--accent-subtle) 44%, var(--bg-panel));
  color: var(--text-secondary);
}
.markdown-body :deep(blockquote > :last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 1em;
  padding-left: 1.5em;
}
.markdown-body :deep(li) {
  margin: 0.25em 0;
}
.markdown-body :deep(.task-list) {
  padding-left: 0;
  list-style: none;
}
.markdown-body :deep(.task-list-item) {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.markdown-body :deep(.task-list-item input) {
  flex: 0 0 auto;
  margin-top: 0.45em;
}
.markdown-body :deep(code) {
  padding: 0.12em 0.35em;
  border-radius: 4px;
  background: var(--code-block-bg);
  font-family: var(--font-mono);
  font-size: 0.9em;
}
.markdown-body :deep(pre) {
  overflow: auto;
  margin: var(--sp-4) 0;
  padding: var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--code-block-bg);
}
.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
  font-size: var(--text-sm);
  line-height: 1.65;
}
.markdown-body :deep(hr) {
  margin: var(--sp-6) 0;
  border: 0;
  border-top: 1px solid var(--border);
}
.markdown-body :deep(table) {
  width: 100%;
  margin: var(--sp-4) 0;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  vertical-align: top;
}
.markdown-body :deep(th) {
  background: var(--bg-sidebar);
  font-weight: 700;
}
.markdown-body :deep(img) {
  display: block;
  max-width: 100%;
  height: auto;
  margin: var(--sp-4) 0;
  border-radius: var(--radius);
}
.editor-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}

@media (max-width: 760px) {
  .editor-content {
    padding: var(--sp-3) var(--sp-3) calc(var(--sp-3) + 72px);
    scroll-padding-bottom: 80px;
  }

  .editor-textarea {
    width: 100%;
    padding: var(--sp-5) var(--sp-4) 96px;
    font-size: var(--text-base);
    line-height: 1.9;
    background:
      repeating-linear-gradient(to bottom, var(--paper-bg) 0, var(--paper-bg) 35px, var(--paper-line) 36px),
      var(--paper-bg);
  }

  .editor-content.article-content {
    display: block;
  }

  .article-split .editor-textarea {
    margin-bottom: var(--sp-4);
  }

  .markdown-preview {
    width: 100%;
    min-height: 320px;
    padding: var(--sp-5) var(--sp-4) 96px;
  }

  .markdown-body :deep(h1) { font-size: 1.35rem; }
  .markdown-body :deep(h2) { font-size: 1.18rem; }
}
</style>
