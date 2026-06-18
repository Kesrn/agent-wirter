<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api/client'
import { friendlyError, useUiStore } from '../stores'
import { renderMarkdown } from '../utils/markdown'
import NovelExtraction from './NovelExtraction.vue'

const props = defineProps<{ projectId: string }>()
const ui = useUiStore()

// ── State ──
interface Source {
  id: string; project_id: string; title: string; source_type: string
  content?: string; content_preview?: string; content_truncated?: boolean
  summary: string | null; key_facts: string[] | null
  constraints: string[] | null; characters: string[] | null; keywords: string[] | null
  tags: string[] | null; always_inject: boolean; chunk_count: number; token_count: number
  metadata_?: Record<string, any> | null
  created_at: string; updated_at: string
}

interface QaSession {
  id: string
  project_id: string
  title: string
  summary: string | null
  message_count: number
  created_at: string
  updated_at: string
}

interface QaMessage {
  role: string
  content: string
  citations?: Array<{ source_kind: string; source_id: string; chunk_id?: string | null; title: string; snippet: string; url?: string; evidence_type?: string; matched_query?: string; score?: number }> | null
  query_plan?: { intent: string; entities: string[]; search_queries: string[] }
  retrieval_stats?: { structured_hits: number; chunk_hits: number; web_hits?: number }
}

const sources = ref<Source[]>([])
const selectedId = ref<string | null>(null)
const filterType = ref<string>('')
const loading = ref(false)
const showAddForm = ref(false)
const editing = ref(false)
const editContent = ref('')
const editTitle = ref('')
const editType = ref('upload')
const editTags = ref('')
const editAlwaysInject = ref(false)
const actionLoading = ref<string | null>(null)
const pendingSourceDeleteId = ref<string | null>(null)

// QA state
const qaQuestion = ref('')
const qaLoading = ref(false)
const qaConversationId = ref<string | null>(null)
const qaMessages = ref<QaMessage[]>([])
const qaSessions = ref<QaSession[]>([])
const qaSessionsLoading = ref(false)
const summaryUpdated = ref(false)
const renamingQaSessionId = ref<string | null>(null)
const renameQaTitle = ref('')
const pendingQaDeleteId = ref<string | null>(null)

const SOURCE_TYPES = [
  { value: '', label: '全部' },
  { value: 'upload', label: '上传资料' },
  { value: 'novel', label: '小说原文' },
  { value: 'fanfic_rule', label: '同人规则' },
  { value: 'timeline', label: '时间线' },
  { value: 'note', label: '笔记' },
  { value: 'reference', label: '参考资料' },
]

const searchQuery = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(loadSources, 300)
}

const filteredSources = computed(() => {
  if (!filterType.value && !searchQuery.value) return sources.value
  return sources.value.filter(s => {
    if (filterType.value && s.source_type !== filterType.value) return false
    return true
  })
})

const selectedSource = computed(() => sources.value.find(s => s.id === selectedId.value) ?? null)
const selectedSourcePreview = computed(() => selectedSource.value?.content ?? selectedSource.value?.content_preview ?? '')

// ── Data loading ──
async function loadSources() {
  loading.value = true
  try {
    sources.value = await api.listKnowledgeSources(props.projectId, {
      source_type: filterType.value || undefined,
      q: searchQuery.value.trim() || undefined,
    })
  } catch (e: unknown) {
    sources.value = []
    ui.showToast(friendlyError(e, '加载资料库失败'), 'error')
  }
  finally { loading.value = false }
}

onMounted(async () => {
  await Promise.all([loadSources(), loadQaSessions()])
})
watch(filterType, loadSources)

// ── Source CRUD ──
function startAdd() {
  showAddForm.value = true
  pendingSourceDeleteId.value = null
  editTitle.value = ''
  editType.value = 'upload'
  editContent.value = ''
  editTags.value = ''
  editAlwaysInject.value = false
}

function cancelAdd() { showAddForm.value = false }

async function submitAdd() {
  if (!editTitle.value.trim()) return
  actionLoading.value = 'add'
  try {
    const res = await api.createKnowledgeSource(props.projectId, {
      title: editTitle.value.trim(),
      source_type: editType.value,
      content: editContent.value,
      tags: editTags.value ? editTags.value.split(',').map(t => t.trim()).filter(Boolean) : undefined,
      always_inject: editAlwaysInject.value,
    })
    showAddForm.value = false
    ui.showToast('资料已保存', 'success')
    await loadSources()
    selectedId.value = res.id
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存资料失败'), 'error')
  }
  finally { actionLoading.value = null }
}

function selectSource(id: string) {
  selectedId.value = id
  showAddForm.value = false
  editing.value = false
  pendingSourceDeleteId.value = null
  showChunks.value = false
  chunks.value = []
  chunkOffset.value = 0
  chunksHasMore.value = false
  // 不清空 qaConversationId 和 qaMessages，保持当前对话状态
}

function requestDeleteSource(id: string) {
  pendingSourceDeleteId.value = pendingSourceDeleteId.value === id ? null : id
}

async function deleteSource(id: string) {
  actionLoading.value = 'delete-' + id
  try {
    await api.deleteKnowledgeSource(props.projectId, id)
    // 删除后自动选中下一条
    if (selectedId.value === id) {
      const idx = filteredSources.value.findIndex(s => s.id === id)
      const next = filteredSources.value[idx + 1] ?? filteredSources.value[idx - 1] ?? null
      selectedId.value = next?.id ?? null
      editing.value = false
      showChunks.value = false
      chunks.value = []
      chunkOffset.value = 0
      chunksHasMore.value = false
    }
    pendingSourceDeleteId.value = null
    ui.showToast('资料已删除', 'success')
    await loadSources()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除资料失败'), 'error')
  }
  finally { actionLoading.value = null }
}

async function reindexSource(id: string) {
  actionLoading.value = 'reindex-' + id
  try {
    const r = await api.reindexSource(props.projectId, id)
    ui.showToast(`切片完成：${r.chunk_count} 个片段，事实 ${r.fact_count} 条`, 'success')
    if (selectedId.value === id) {
      showChunks.value = false
      chunks.value = []
      chunkOffset.value = 0
      chunksHasMore.value = false
    }
    await loadSources()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '切片失败'), 'error')
  }
  finally { actionLoading.value = null }
}

async function rebuildFacts() {
  actionLoading.value = 'rebuild-facts'
  try {
    const r = await api.rebuildFacts(props.projectId)
    ui.showToast(`已重建事实索引：${r.fact_count} 条`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '重建事实索引失败'), 'error')
  }
  finally { actionLoading.value = null }
}

async function summarizeSource(id: string) {
  actionLoading.value = 'summarize-' + id
  try {
    await api.summarizeSource(props.projectId, id)
    ui.showToast('AI 摘要已更新', 'success')
    await loadSources()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, 'AI 摘要失败'), 'error')
  }
  finally { actionLoading.value = null }
}

async function ensureSelectedSourceContent() {
  if (!selectedId.value) return null
  const current = selectedSource.value
  if (current?.content !== undefined) return current
  actionLoading.value = 'load-source-' + selectedId.value
  try {
    const detail = await api.getKnowledgeSource(props.projectId, selectedId.value)
    const index = sources.value.findIndex(s => s.id === selectedId.value)
    if (index >= 0) {
      sources.value[index] = { ...sources.value[index], ...detail }
      return sources.value[index]
    }
    sources.value.unshift(detail)
    return detail
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '加载资料详情失败'), 'error')
    return null
  } finally {
    actionLoading.value = null
  }
}

// ── Edit ──
async function startEdit() {
  const source = await ensureSelectedSourceContent()
  if (!source) return
  editing.value = true
  pendingSourceDeleteId.value = null
  editTitle.value = source.title
  editType.value = source.source_type
  editContent.value = source.content ?? ''
  editAlwaysInject.value = source.always_inject
  editTags.value = source.tags?.join(', ') ?? ''
}

async function saveEdit() {
  if (!selectedId.value) return
  actionLoading.value = 'edit'
  try {
    await api.updateKnowledgeSource(props.projectId, selectedId.value, {
      title: editTitle.value.trim(),
      source_type: editType.value,
      content: editContent.value,
      always_inject: editAlwaysInject.value,
      tags: editTags.value ? editTags.value.split(',').map(t => t.trim()).filter(Boolean) : undefined,
    })
    editing.value = false
    ui.showToast('资料已更新', 'success')
    await loadSources()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '更新资料失败'), 'error')
  }
  finally { actionLoading.value = null }
}

// ── Chunks ──
const chunks = ref<Array<{
  id: string; source_id: string; chunk_index: number
  content: string; summary: string | null; keywords: string[] | null; token_count: number
}>>([])
const chunksLoading = ref(false)
const showChunks = ref(false)
const chunkOffset = ref(0)
const chunksHasMore = ref(false)
const CHUNK_PAGE_SIZE = 50

async function loadChunks() {
  if (!selectedId.value) return
  if (showChunks.value) { showChunks.value = false; return }
  showChunks.value = true
  await fetchChunks(true)
}

async function fetchChunks(reset = false) {
  if (!selectedId.value || chunksLoading.value) return
  if (reset) {
    chunks.value = []
    chunkOffset.value = 0
    chunksHasMore.value = false
  }
  chunksLoading.value = true
  try {
    const page = await api.listKnowledgeChunks(props.projectId, selectedId.value, {
      offset: chunkOffset.value,
      limit: CHUNK_PAGE_SIZE,
    })
    chunks.value = reset ? page : [...chunks.value, ...page]
    chunkOffset.value += page.length
    chunksHasMore.value = page.length === CHUNK_PAGE_SIZE
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '加载切片失败'), 'error')
  } finally { chunksLoading.value = false }
}

// ── QA ──
async function loadQaSessions() {
  qaSessionsLoading.value = true
  try {
    qaSessions.value = await api.listKnowledgeSessions(props.projectId)
    if (!qaConversationId.value && qaSessions.value.length) {
      await openQaSession(qaSessions.value[0].id)
    }
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '加载问答历史失败'), 'error')
  } finally {
    qaSessionsLoading.value = false
  }
}

async function openQaSession(sessionId: string) {
  qaConversationId.value = sessionId
  summaryUpdated.value = false
  qaLoading.value = true
  try {
    const messages = await api.listKnowledgeSessionMessages(props.projectId, sessionId)
    qaMessages.value = messages.map(msg => ({
      role: msg.role,
      content: msg.content,
      citations: msg.citations ?? undefined,
    }))
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '加载对话失败'), 'error')
  } finally {
    qaLoading.value = false
  }
}

function startNewQaSession() {
  qaConversationId.value = null
  qaMessages.value = []
  summaryUpdated.value = false
  qaQuestion.value = ''
  renamingQaSessionId.value = null
  pendingQaDeleteId.value = null
}

function beginRenameQaSession(sessionId: string) {
  const session = qaSessions.value.find(s => s.id === sessionId)
  if (!session) return
  renamingQaSessionId.value = sessionId
  renameQaTitle.value = session.title
  pendingQaDeleteId.value = null
}

function cancelRenameQaSession() {
  renamingQaSessionId.value = null
  renameQaTitle.value = ''
}

async function saveRenameQaSession(sessionId: string) {
  const session = qaSessions.value.find(s => s.id === sessionId)
  const newTitle = renameQaTitle.value.trim()
  if (!session || !newTitle) return
  if (newTitle === session.title) {
    cancelRenameQaSession()
    return
  }
  actionLoading.value = 'rename-session-' + sessionId
  try {
    await api.updateKnowledgeSession(props.projectId, sessionId, { title: newTitle })
    session.title = newTitle
    cancelRenameQaSession()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '重命名失败'), 'error')
  } finally {
    actionLoading.value = null
  }
}

function requestDeleteQaSession(sessionId: string) {
  pendingQaDeleteId.value = pendingQaDeleteId.value === sessionId ? null : sessionId
  if (pendingQaDeleteId.value) {
    renamingQaSessionId.value = null
    renameQaTitle.value = ''
  }
}

async function deleteQaSession(sessionId: string) {
  const session = qaSessions.value.find(s => s.id === sessionId)
  if (!session) return
  actionLoading.value = 'delete-session-' + sessionId
  try {
    await api.deleteKnowledgeSession(props.projectId, sessionId)
    qaSessions.value = qaSessions.value.filter(s => s.id !== sessionId)
    pendingQaDeleteId.value = null
    if (renamingQaSessionId.value === sessionId) cancelRenameQaSession()
    // 如果删除的是当前会话，切换到最新会话或新建
    if (qaConversationId.value === sessionId) {
      if (qaSessions.value.length) {
        await openQaSession(qaSessions.value[0].id)
      } else {
        startNewQaSession()
      }
    }
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除失败'), 'error')
  } finally {
    actionLoading.value = null
  }
}

async function askQuestion() {
  if (!qaQuestion.value.trim() || qaLoading.value) return
  const q = qaQuestion.value.trim()
  qaQuestion.value = ''
  qaMessages.value.push({ role: 'user', content: q })
  qaLoading.value = true
  try {
    const res = await api.askKnowledge(props.projectId, {
      question: q,
      conversation_id: qaConversationId.value ?? undefined,
    })
    qaConversationId.value = res.conversation_id
    summaryUpdated.value = res.conversation_summary_updated
    qaMessages.value.push({ role: 'assistant', content: res.answer, citations: res.citations, query_plan: res.query_plan, retrieval_stats: res.retrieval_stats })
    await loadQaSessions()
  } catch (e: unknown) {
    qaMessages.value.push({ role: 'assistant', content: `请求失败：${String(e)}` })
  }
  finally { qaLoading.value = false }
}

function renderQaContent(content: string) {
  return renderMarkdown(content)
}

function typeLabel(t: string) {
  return SOURCE_TYPES.find(s => s.value === t)?.label ?? t
}

const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  direct_character_evidence: '人物证据',
  relationship_evidence: '关系证据',
  ability_table: '技能表',
  worldbuilding_entry: '世界观',
  timeline_event: '时间线',
  negative_evidence: '待确认',
  generic_context: '普通资料',
  web_search: '联网',
}
function evidenceTypeLabel(t?: string) {
  return EVIDENCE_TYPE_LABELS[t ?? ''] ?? ''
}

function handleCitationClick(c: NonNullable<QaMessage['citations']>[number]) {
  if (c.source_kind === 'web_search') {
    const url = c.url || c.source_id
    if (url) window.open(url, '_blank', 'noopener,noreferrer')
    return
  }
  if (c.source_kind === 'project_source_chunk') selectSource(c.source_id)
}

// ── File upload ──
const fileInputRef = ref<HTMLInputElement | null>(null)
const uploadAsNovel = ref(false)
const uploadGenre = ref<'magic_fantasy' | 'historical'>('magic_fantasy')

function triggerUpload() {
  fileInputRef.value?.click()
}

async function handleFileUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const fd = new FormData()
  fd.append('file', file)
  fd.append('title', file.name.replace(/\.(txt|md)$/i, ''))
  // 勾选"上传为小说原文"时传 source_type=novel + genre + canon_level
  if (uploadAsNovel.value) {
    fd.append('source_type', 'novel')
    fd.append('genre', uploadGenre.value)
    fd.append('canon_level', 'original')
  }
  try {
    loading.value = true
    const uploaded = await api.uploadKnowledgeFile(props.projectId, fd)
    filterType.value = ''
    await loadSources()
    selectedId.value = uploaded.id
    showAddForm.value = false
    editing.value = false
    ui.showToast(`已上传「${uploaded.title}」`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '上传失败'), 'error')
  }
  finally { loading.value = false; input.value = '' }
}
</script>

<template>
  <div class="knowledge-page">
    <header class="knowledge-topbar">
      <div class="topbar-main">
        <router-link class="back-link" :to="`/projects/${props.projectId}`">返回章节</router-link>
        <div>
          <h2>小说资料库</h2>
          <p>管理本小说的参考资料、同人规则与问答上下文</p>
        </div>
      </div>
      <div class="topbar-actions">
        <button class="btn-secondary compact" @click="startAdd">新建资料</button>
        <label class="upload-novel-toggle">
          <input type="checkbox" v-model="uploadAsNovel" />
          <span>上传为小说原文</span>
        </label>
        <select v-if="uploadAsNovel" v-model="uploadGenre" class="upload-genre-select">
          <option value="magic_fantasy">魔法玄幻</option>
          <option value="historical">历史</option>
        </select>
        <button class="btn-primary compact" @click="triggerUpload">上传文件</button>
        <input ref="fileInputRef" type="file" accept=".txt,.md,.text,.csv,.tsv,.docx,.xlsx,.xlsm,.pdf" style="display:none" @change="handleFileUpload" />
      </div>
    </header>

    <div class="knowledge-library">
      <!-- Left: source list -->
      <div class="source-sidebar">
        <div class="sidebar-header">
          <h3>资料列表</h3>
        </div>
      <div class="sidebar-search">
        <input v-model="searchQuery" placeholder="搜索标题..." @input="onSearchInput" />
      </div>
      <div class="filter-tabs">
        <button
          v-for="t in SOURCE_TYPES" :key="t.value"
          class="filter-tab" :class="{ active: filterType === t.value }"
          @click="filterType = t.value"
        >{{ t.label }}</button>
      </div>
      <div v-if="loading" class="loading">加载中...</div>
      <div v-else-if="!filteredSources.length" class="empty-list">暂无资料</div>
      <div v-else class="source-list">
        <div
          v-for="s in filteredSources" :key="s.id"
          class="source-item" :class="{ selected: selectedId === s.id }"
          @click="selectSource(s.id)"
        >
          <div class="source-item-row">
            <span class="source-type-badge" :class="s.source_type">{{ typeLabel(s.source_type) }}</span>
            <span class="source-title">{{ s.title }}</span>
          </div>
          <div class="source-item-meta">
            {{ s.chunk_count }}片 · {{ s.token_count.toLocaleString() }}字
            <span v-if="s.always_inject" class="inject-dot">必注入</span>
            · {{ new Date(s.updated_at).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }) }}
          </div>
        </div>
      </div>
      </div>

      <!-- Middle: detail / add form -->
      <div class="source-detail">
      <template v-if="showAddForm">
        <h3>新建资料</h3>
        <div class="form-row"><label>标题</label><input v-model="editTitle" placeholder="资料标题" /></div>
        <div class="form-row"><label>类型</label>
          <select v-model="editType">
            <option value="upload">上传资料</option>
            <option value="novel">小说原文</option>
            <option value="fanfic_rule">同人规则</option>
            <option value="timeline">时间线</option>
            <option value="note">笔记</option>
            <option value="reference">参考资料</option>
          </select>
        </div>
        <div class="form-row"><label>标签（逗号分隔）</label><input v-model="editTags" placeholder="标签1, 标签2" /></div>
        <div class="form-row checkbox-row"><label><input type="checkbox" v-model="editAlwaysInject" /> 每次生成必注入</label></div>
        <div class="form-row"><label>内容</label><textarea v-model="editContent" rows="12" placeholder="粘贴或输入资料内容..." /></div>
        <div class="form-actions">
          <button class="btn-primary" :disabled="!editTitle.trim() || actionLoading === 'add'" @click="submitAdd">
            {{ actionLoading === 'add' ? '保存中...' : '保存' }}
          </button>
          <button class="btn-secondary" @click="cancelAdd">取消</button>
        </div>
      </template>

      <template v-else-if="selectedSource">
        <div class="detail-header">
          <span class="source-type-badge" :class="selectedSource.source_type">{{ typeLabel(selectedSource.source_type) }}</span>
          <h3>{{ selectedSource.title }}</h3>
          <div class="detail-actions">
            <button class="btn-sm" @click="startEdit">编辑</button>
            <button class="btn-sm" :disabled="actionLoading === 'reindex-' + selectedId" @click="reindexSource(selectedId!)">切片</button>
            <button class="btn-sm" :disabled="actionLoading === 'rebuild-facts'" @click="rebuildFacts">重建事实索引</button>
            <button class="btn-sm" :disabled="actionLoading === 'summarize-' + selectedId" @click="summarizeSource(selectedId!)">AI摘要</button>
            <button class="btn-sm btn-danger" :disabled="actionLoading === 'delete-' + selectedId" @click="requestDeleteSource(selectedId!)">删除</button>
          </div>
        </div>

        <div v-if="pendingSourceDeleteId === selectedId" class="inline-confirm">
          <span>删除此资料？关联切片也会被删除。</span>
          <button class="btn-sm btn-danger" :disabled="actionLoading === 'delete-' + selectedId" @click="deleteSource(selectedId!)">
            {{ actionLoading === 'delete-' + selectedId ? '删除中...' : '确认删除' }}
          </button>
          <button class="btn-sm" :disabled="actionLoading === 'delete-' + selectedId" @click="pendingSourceDeleteId = null">取消</button>
        </div>

        <div v-if="selectedSource.always_inject" class="inject-badge">每次生成必注入</div>
        <div v-if="selectedSource.tags?.length" class="tags-row">
          <span v-for="t in selectedSource.tags" :key="t" class="tag">{{ t }}</span>
        </div>

        <div class="detail-meta">
          <span>{{ selectedSource.chunk_count }} 片</span>
          <span>{{ selectedSource.token_count.toLocaleString() }} 字</span>
          <span>{{ new Date(selectedSource.updated_at).toLocaleString('zh-CN') }}</span>
        </div>

        <div v-if="editing" class="edit-form">
          <div class="form-row"><label>标题</label><input v-model="editTitle" /></div>
          <div class="form-row"><label>类型</label>
            <select v-model="editType">
              <option value="upload">上传资料</option>
              <option value="novel">小说原文</option>
              <option value="fanfic_rule">同人规则</option>
              <option value="timeline">时间线</option>
              <option value="note">笔记</option>
              <option value="reference">参考资料</option>
            </select>
          </div>
          <div class="form-row checkbox-row"><label><input type="checkbox" v-model="editAlwaysInject" /> 每次生成必注入</label></div>
          <div class="form-row"><label>标签（逗号分隔）</label><input v-model="editTags" /></div>
          <div class="form-row"><label>内容</label><textarea v-model="editContent" rows="15" /></div>
          <div class="form-actions">
            <button class="btn-primary" :disabled="actionLoading === 'edit'" @click="saveEdit">保存</button>
            <button class="btn-secondary" @click="editing = false">取消</button>
          </div>
        </div>

        <template v-else>
          <div v-if="selectedSource.summary" class="section">
            <h4>AI 摘要</h4>
            <p class="summary-text">{{ selectedSource.summary }}</p>
          </div>
          <div v-if="selectedSource.key_facts?.length" class="section">
            <h4>关键事实</h4>
            <ul><li v-for="f in selectedSource.key_facts" :key="f">{{ f }}</li></ul>
          </div>
          <div v-if="selectedSource.constraints?.length" class="section">
            <h4>约束/禁忌</h4>
            <ul><li v-for="c in selectedSource.constraints" :key="c">{{ c }}</li></ul>
          </div>
          <div class="section">
            <h4>原文</h4>
            <pre class="content-pre">{{ selectedSourcePreview }}</pre>
            <div v-if="selectedSource.content_truncated && selectedSource.content === undefined" class="content-preview-note">
              当前仅显示前 {{ selectedSourcePreview.length.toLocaleString() }} 字。点击「编辑」会按需加载完整原文。
            </div>
          </div>
          <div class="section">
            <h4 class="chunks-toggle" @click="loadChunks">
              切片（{{ selectedSource.chunk_count }}）
              <span v-if="showChunks">▲</span>
              <span v-else>▼</span>
            </h4>
            <div v-if="showChunks">
              <div v-if="chunksLoading && !chunks.length" class="loading">加载中...</div>
              <div v-else-if="!chunks.length" class="empty-list">无切片</div>
              <div v-else class="chunk-list">
                <div v-for="c in chunks" :key="c.id" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">#{{ c.chunk_index }}</span>
                    <span class="chunk-tokens">{{ c.token_count }}字</span>
                  </div>
                  <pre class="chunk-content">{{ c.content }}</pre>
                  <div v-if="c.summary" class="chunk-summary">摘要：{{ c.summary }}</div>
                  <div v-if="c.keywords?.length" class="chunk-keywords">
                    <span v-for="kw in c.keywords" :key="kw" class="keyword-tag">{{ kw }}</span>
                  </div>
                </div>
                <button
                  v-if="chunksHasMore"
                  class="load-more-btn"
                  :disabled="chunksLoading"
                  @click="fetchChunks(false)"
                >
                  {{ chunksLoading ? '加载中...' : `加载更多（已显示 ${chunks.length} / ${selectedSource.chunk_count}）` }}
                </button>
              </div>
            </div>
          </div>
        </template>

        <!-- 小说结构化抽取（仅 novel 类型显示） -->
        <NovelExtraction
          :project-id="projectId"
          :source-id="selectedId!"
          :source-type="selectedSource.source_type"
          :initial-genre="selectedSource.metadata_?.genre"
        />
      </template>

      <div v-else class="empty-detail">
        <p>选择左侧资料查看详情</p>
        <p>或点击「新建资料」添加资料</p>
      </div>
      </div>

      <!-- Right: QA panel -->
      <div class="qa-panel">
      <div class="qa-panel-header">
        <h3>资料问答</h3>
        <button class="btn-sm" @click="startNewQaSession">新对话</button>
      </div>
      <div class="qa-session-list">
        <div v-if="qaSessionsLoading" class="qa-session-empty">加载历史...</div>
        <div v-else-if="!qaSessions.length" class="qa-session-empty">暂无历史对话</div>
        <template v-else>
          <div
            v-for="session in qaSessions"
            :key="session.id"
            class="qa-session-item"
            :class="{ active: qaConversationId === session.id, confirming: pendingQaDeleteId === session.id }"
            @click="openQaSession(session.id)"
          >
            <form
              v-if="renamingQaSessionId === session.id"
              class="qa-session-rename"
              @click.stop
              @submit.prevent="saveRenameQaSession(session.id)"
            >
              <input v-model="renameQaTitle" aria-label="对话标题" @keydown.esc.prevent="cancelRenameQaSession" />
              <button class="session-action-btn strong" :disabled="!renameQaTitle.trim() || actionLoading === 'rename-session-' + session.id" type="submit">
                {{ actionLoading === 'rename-session-' + session.id ? '保存中' : '保存' }}
              </button>
              <button class="session-action-btn" :disabled="actionLoading === 'rename-session-' + session.id" type="button" @click="cancelRenameQaSession">取消</button>
            </form>
            <div v-else-if="pendingQaDeleteId === session.id" class="qa-session-confirm" @click.stop>
              <div class="qa-session-confirm-copy">
                <span class="qa-session-confirm-title">删除此对话？</span>
                <span class="qa-session-confirm-name">{{ session.title }}</span>
              </div>
              <button class="session-action-btn danger strong" :disabled="actionLoading === 'delete-session-' + session.id" @click="deleteQaSession(session.id)">
                {{ actionLoading === 'delete-session-' + session.id ? '删除中' : '确认' }}
              </button>
              <button class="session-action-btn" :disabled="actionLoading === 'delete-session-' + session.id" @click="pendingQaDeleteId = null">取消</button>
            </div>
            <template v-else>
              <span class="qa-session-title">{{ session.title }}</span>
              <span class="qa-session-meta">{{ session.message_count }} 条 · {{ new Date(session.updated_at).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }) }}</span>
              <div class="qa-session-actions">
                <button class="session-action-btn" @click.stop="beginRenameQaSession(session.id)" title="重命名">改</button>
                <button class="session-action-btn danger" @click.stop="requestDeleteQaSession(session.id)" title="删除">删</button>
              </div>
            </template>
          </div>
        </template>
      </div>
      <div v-if="summaryUpdated" class="summary-notice">已整理较早对话</div>
      <div class="qa-messages">
        <div v-for="(msg, i) in qaMessages" :key="i" class="qa-msg" :class="msg.role">
          <div class="msg-content markdown-body" v-html="renderQaContent(msg.content)"></div>
          <div v-if="msg.citations?.length" class="msg-citations">
            <span v-for="(c, ci) in msg.citations" :key="ci" class="citation-chip" :class="{ web: c.source_kind === 'web_search' }" :title="c.snippet + (c.matched_query ? ' | 匹配: ' + c.matched_query : '')"
              @click="handleCitationClick(c)">
              <span v-if="c.evidence_type" class="evidence-tag" :class="c.evidence_type">{{ evidenceTypeLabel(c.evidence_type) }}</span>
              <span class="citation-prefix">引用</span>{{ c.title || c.source_kind }}
            </span>
          </div>
          <details v-if="msg.query_plan" class="query-plan-details">
            <summary>检索计划</summary>
            <div class="query-plan-body">
              <div>意图：{{ msg.query_plan.intent }} · 实体：{{ msg.query_plan.entities.join('、') || '无' }} · 检索方向：{{ msg.query_plan.search_queries.slice(0, 4).join('、') }}</div>
              <div v-if="msg.retrieval_stats">命中：结构化 {{ msg.retrieval_stats.structured_hits }} 条，资料片段 {{ msg.retrieval_stats.chunk_hits }} 条，联网 {{ msg.retrieval_stats.web_hits ?? 0 }} 条</div>
            </div>
          </details>
        </div>
        <div v-if="qaLoading" class="qa-msg assistant loading-msg">思考中...</div>
        <div v-if="!qaMessages.length && !qaLoading" class="qa-empty">问一个关于资料库的问题</div>
      </div>
      <div class="qa-input-row">
        <input
          v-model="qaQuestion" placeholder="输入问题..."
          @keyup.enter="askQuestion"
          :disabled="qaLoading"
        />
        <button class="btn-primary" :disabled="!qaQuestion.trim() || qaLoading" @click="askQuestion">发送</button>
      </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.knowledge-page {
  height: calc(100vh - var(--desktop-status-bar-height, 0px));
  height: calc(100dvh - var(--desktop-status-bar-height, 0px));
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
}

.knowledge-topbar {
  flex: 0 0 auto;
  min-height: 76px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 14px 22px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
}

.topbar-main {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 16px;
}

.topbar-main h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.25;
}

.topbar-main p {
  margin: 4px 0 0;
  color: var(--text-secondary);
  font-size: 13px;
}

.back-link {
  flex: 0 0 auto;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 13px;
}

.back-link:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.upload-novel-toggle {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-soft);
  cursor: pointer;
}
.upload-novel-toggle input { cursor: pointer; }
.upload-genre-select {
  padding: 2px 6px;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
}

.knowledge-library {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(280px, 320px) minmax(420px, 1fr) minmax(340px, 380px);
  gap: 1px;
  background: var(--border);
}

.source-sidebar {
  background: var(--bg-panel);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.sidebar-header h3 { margin: 0; font-size: 15px; }
.filter-tabs {
  display: flex;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.filter-tab {
  padding: 5px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}
.filter-tab.active { background: var(--accent); color: #fff; }
.filter-tab:hover:not(.active) { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.loading, .empty-list { padding: 20px; text-align: center; color: var(--text-secondary); font-size: 13px; }
.source-list { flex: 1; overflow-y: auto; }
.source-item {
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
}
.source-item:hover { background: color-mix(in srgb, var(--accent) 5%, transparent); }
.source-item.selected { background: color-mix(in srgb, var(--accent) 12%, transparent); }
.source-item-row { display: flex; align-items: center; gap: 6px; }
.source-item-meta {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 3px;
  padding-left: 2px;
}
.inject-dot {
  display: inline-block;
  padding: 0 4px;
  border-radius: 3px;
  background: #ff9800;
  color: #fff;
  font-size: 10px;
  font-weight: 600;
}
.sidebar-search {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
.sidebar-search input {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 12px;
  background: var(--bg);
  color: var(--text);
}
.source-type-badge {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  flex-shrink: 0;
}
.source-type-badge.upload { background: #e3f2fd; color: #1565c0; }
.source-type-badge.fanfic_rule { background: #fce4ec; color: #c62828; }
.source-type-badge.timeline { background: #f3e5f5; color: #7b1fa2; }
.source-type-badge.note { background: #fff3e0; color: #e65100; }
.source-type-badge.reference { background: #e8f5e9; color: #2e7d32; }
.source-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.source-meta { font-size: 11px; color: var(--text-secondary); flex-shrink: 0; }

.source-detail {
  background: var(--bg-panel);
  padding: 16px 20px;
  overflow-y: auto;
  min-width: 0;
}
.detail-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.detail-header h3 { margin: 0; flex: 1; }
.detail-actions { display: flex; gap: 6px; }
.btn-sm {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.btn-sm:hover { border-color: var(--accent); color: var(--accent); }
.btn-sm.btn-danger { color: #c62828; }
.btn-sm.btn-danger:hover { border-color: #c62828; }
.inline-confirm {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: -2px 0 12px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, #c62828 35%, var(--border));
  border-radius: 6px;
  background: color-mix(in srgb, #c62828 7%, transparent);
  color: var(--text);
  font-size: 12px;
}
.inline-confirm > span {
  flex: 1;
  min-width: 180px;
  color: var(--text-secondary);
}
.inject-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, #ff9800 15%, transparent);
  color: #e65100;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 10px;
}
.tags-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
.tag {
  padding: 2px 8px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  color: var(--accent);
  font-size: 11px;
}
.detail-meta {
  display: flex; gap: 12px; flex-wrap: wrap;
  font-size: 12px; color: var(--text-secondary);
  margin-bottom: 14px; padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}
.section { margin-bottom: 16px; }
.section h4 { margin: 0 0 6px; font-size: 13px; color: var(--text-secondary); }
.chunks-toggle {
  cursor: pointer; user-select: none;
  display: flex; align-items: center; gap: 6px;
}
.chunks-toggle:hover { color: var(--accent); }
.chunk-list { display: flex; flex-direction: column; gap: 8px; }
.chunk-item {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--bg) 50%, transparent);
}
.chunk-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.chunk-index {
  font-weight: 700; font-size: 12px;
  padding: 1px 6px; border-radius: 4px;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  color: var(--accent);
}
.chunk-tokens { font-size: 11px; color: var(--text-secondary); }
.chunk-content {
  white-space: pre-wrap; word-break: break-word;
  font-size: 12px; line-height: 1.5;
  max-height: 120px; overflow-y: auto;
  margin: 0; padding: 6px 8px;
  background: var(--bg-panel); border-radius: 4px;
}
.chunk-summary { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.chunk-keywords { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; }
.keyword-tag {
  padding: 1px 6px; border-radius: 3px;
  background: color-mix(in srgb, #4caf50 10%, transparent);
  color: #2e7d32; font-size: 10px;
}
.load-more-btn {
  align-self: center;
  padding: 7px 14px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
}
.load-more-btn:hover:not(:disabled) {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}
.load-more-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.summary-text { font-size: 13px; line-height: 1.6; }
.section ul { margin: 0; padding-left: 20px; font-size: 13px; }
.section li { margin-bottom: 4px; }
.content-pre {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.6;
  max-height: 400px;
  overflow-y: auto;
  background: color-mix(in srgb, var(--bg) 50%, transparent);
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
}
.content-preview-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.empty-detail { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--text-secondary); }
.empty-detail p { margin: 4px 0; }

.form-row { margin-bottom: 12px; }
.form-row label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 4px; color: var(--text-secondary); }
.form-row input, .form-row textarea, .form-row select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}
.form-row textarea { resize: vertical; }
.form-row.checkbox-row label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.form-row.checkbox-row input[type="checkbox"] { width: auto; }
.form-actions { display: flex; gap: 8px; margin-top: 12px; }
.btn-primary {
  padding: 8px 20px;
  border: none;
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.btn-primary.compact,
.btn-secondary.compact {
  padding: 8px 14px;
  white-space: nowrap;
}
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary {
  padding: 8px 20px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}

.qa-panel {
  background: var(--bg-panel);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}
.qa-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.qa-panel-header h3 { margin: 0; font-size: 15px; }
.qa-session-list {
  flex: 0 0 auto;
  max-height: 132px;
  overflow-y: auto;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 35%, transparent);
}
.qa-session-item {
  position: relative;
  width: 100%;
  min-height: 44px;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.qa-session-item:hover {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.qa-session-item.active {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.qa-session-item.confirming {
  cursor: default;
}
.qa-session-title {
  display: block;
  padding-right: 46px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}
.qa-session-meta,
.qa-session-empty {
  color: var(--text-secondary);
  font-size: 11px;
}
.qa-session-meta {
  display: block;
  margin-top: 3px;
}
.qa-session-actions {
  position: absolute;
  top: 4px;
  right: 4px;
  display: none;
  gap: 2px;
}
.qa-session-item:hover .qa-session-actions {
  display: flex;
}
.session-action-btn {
  padding: 2px 6px;
  border: none;
  border-radius: 3px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.4;
  cursor: pointer;
  opacity: 0.6;
}
.session-action-btn.strong {
  font-weight: 600;
  opacity: 0.85;
}
.session-action-btn.danger {
  color: #c62828;
}
.session-action-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.session-action-btn:hover {
  opacity: 1;
  background: color-mix(in srgb, var(--text) 10%, transparent);
}
.qa-session-rename {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 4px;
  align-items: center;
}
.qa-session-rename input {
  min-width: 0;
  width: 100%;
  padding: 4px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 12px;
}
.qa-session-rename input:focus {
  outline: none;
  border-color: var(--accent);
}
.qa-session-confirm {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 11px;
}
.qa-session-confirm-copy {
  min-width: 0;
}
.qa-session-confirm-title,
.qa-session-confirm-name {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qa-session-confirm-title {
  color: var(--text);
  font-weight: 600;
}
.qa-session-confirm-name {
  margin-top: 2px;
  color: var(--text-secondary);
}
.qa-session-empty {
  padding: 8px 4px;
  text-align: center;
}
.summary-notice {
  padding: 6px 14px;
  font-size: 11px;
  color: #e65100;
  background: color-mix(in srgb, #ff9800 10%, transparent);
  text-align: center;
}
.qa-messages { flex: 1; overflow-y: auto; padding: 12px; }
.qa-msg { margin-bottom: 12px; }
.qa-msg.user .msg-content {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  padding: 8px 12px;
  border-radius: 12px 12px 4px 12px;
  font-size: 13px;
  line-height: 1.5;
  margin-left: 30px;
}
.qa-msg.assistant .msg-content {
  background: color-mix(in srgb, var(--bg) 80%, transparent);
  padding: 8px 12px;
  border-radius: 12px 12px 12px 4px;
  font-size: 13px;
  line-height: 1.6;
}
.qa-msg .markdown-body {
  overflow-wrap: anywhere;
}
.qa-msg .markdown-body :deep(p) {
  margin: 0 0 8px;
}
.qa-msg .markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.qa-msg .markdown-body :deep(ol),
.qa-msg .markdown-body :deep(ul) {
  margin: 8px 0 0;
  padding-left: 22px;
}
.qa-msg .markdown-body :deep(li) {
  margin: 4px 0;
  padding-left: 2px;
}
.qa-msg .markdown-body :deep(strong) {
  color: var(--text);
  font-weight: 700;
}
.loading-msg { color: var(--text-secondary); font-style: italic; font-size: 13px; }
.qa-empty { color: var(--text-secondary); text-align: center; padding: 40px 0; font-size: 13px; }
.msg-citations { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; }
.citation-chip {
  padding: 2px 8px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--accent);
  font-size: 11px;
  cursor: pointer;
}
.citation-chip.web {
  background: color-mix(in srgb, #00bfa5 14%, transparent);
  color: #19d4bd;
}
.citation-chip:hover { text-decoration: underline; }
.citation-prefix {
  display: inline-block;
  margin-right: 4px;
  color: var(--text-secondary);
  font-weight: 600;
}
.evidence-tag {
  display: inline-block;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 9px;
  font-weight: 700;
  margin-right: 2px;
  vertical-align: middle;
  line-height: 14px;
}
.evidence-tag.direct_character_evidence { background: #4caf50; color: #fff; }
.evidence-tag.relationship_evidence { background: #e91e63; color: #fff; }
.evidence-tag.ability_table { background: #2196f3; color: #fff; }
.evidence-tag.worldbuilding_entry { background: #9c27b0; color: #fff; }
.evidence-tag.timeline_event { background: #ff9800; color: #fff; }
.evidence-tag.generic_context { background: #78909c; color: #fff; }
.evidence-tag.negative_evidence { background: #b0bec5; color: #546e7a; }
.evidence-tag.web_search { background: #009688; color: #fff; }
.query-plan-details {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary);
}
.query-plan-details summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 11px;
}
.query-plan-body {
  margin-top: 4px;
  padding: 6px 8px;
  background: color-mix(in srgb, var(--bg) 50%, transparent);
  border-radius: 6px;
  line-height: 1.5;
}
.qa-input-row {
  display: flex;
  gap: 6px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
}
.qa-input-row > input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-size: 13px;
}
.qa-input-row > .btn-primary {
  flex: 0 0 72px;
  min-width: 72px;
  white-space: nowrap;
}
.web-toggle {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
}
.web-toggle.active {
  border-color: #00bfa5;
  color: #19d4bd;
  background: color-mix(in srgb, #00bfa5 10%, transparent);
}
.web-toggle input {
  flex: 0 0 auto;
  width: 13px;
  height: 13px;
  margin: 0;
}

@media (max-width: 1180px) {
  .knowledge-library {
    grid-template-columns: minmax(240px, 280px) minmax(360px, 1fr) minmax(300px, 340px);
  }
}

@media (max-width: 900px) {
  .knowledge-topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .knowledge-library {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .source-sidebar,
  .source-detail,
  .qa-panel {
    min-height: 320px;
  }
}
</style>
