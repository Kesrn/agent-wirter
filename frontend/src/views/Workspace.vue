<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore, useChapterStore, useRelationStore, useWorldEntryStore, useCharacterStore, useOutlineStore, useHiddenThreadStore, useUiStore, friendlyError } from '../stores'
import { API_BASE_URL, type OutlineItem, type HiddenThread, type Character, type WorldEntry } from '../api/types'
import NovelEditor from '../components/NovelEditor.vue'
import AgentPanel from '../components/AgentPanel.vue'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()
const chapterStore = useChapterStore()
const relationStore = useRelationStore()
const worldEntryStore = useWorldEntryStore()
const characterStore = useCharacterStore()
const outlineStore = useOutlineStore()
const hiddenThreadStore = useHiddenThreadStore()
const ui = useUiStore()

const projectId = computed(() => route.params.id as string)
const currentProject = computed(() => projectStore.projects.find(p => p.id === projectId.value))
const projectMode = computed(() => currentProject.value?.mode ?? 'novel')
const sidebarOpen = ref(true)
const agentOpen = ref(true)
const activeTab = ref<'chapters' | 'outline' | 'characters' | 'world'>('chapters')

// Mobile: which panel to show
const mobilePanel = ref<'editor' | 'chapters' | 'characters' | 'world' | 'agent'>('editor')

watch(projectId, (id) => {
  projectStore.setCurrent(id)
  chapterStore.loadChapters(id)
  worldEntryStore.loadWorldEntries(id)
  characterStore.loadCharacters(id)
  relationStore.loadRelations(id)
  outlineStore.loadOutlines(id)
  hiddenThreadStore.loadHiddenThreads(id)
}, { immediate: true })

const chapters = computed(() => chapterStore.chaptersForProject(projectId.value))
const currentChapter = computed(() => chapterStore.currentChapterForProject(projectId.value))
const characters = computed(() => characterStore.charactersForProject(projectId.value))
const outline = computed(() => outlineStore.entriesForProject(projectId.value))
const worldEntries = computed(() => worldEntryStore.entriesForProject(projectId.value))
const hiddenThreads = computed(() => hiddenThreadStore.threadsForProject(projectId.value))

function selectChapter(num: number) {
  chapterStore.setCurrentChapter(num)
  mobilePanel.value = 'editor'
}

const isMobile = ref(window.innerWidth < 900)
function onResize() { isMobile.value = window.innerWidth < 900 }
onMounted(() => window.addEventListener('resize', onResize))
onUnmounted(() => window.removeEventListener('resize', onResize))

// Navigation
function handleLogout() {
  localStorage.removeItem('ai_write_logged_in')
  localStorage.removeItem('ai_write_token')
  router.push('/login')
}

// Export
const showExportMenu = ref(false)
const exporting = ref(false)

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('ai_write_token')
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

async function exportFromBackend(format: 'txt' | 'md') {
  const project = currentProject.value
  if (!project || exporting.value) return

  exporting.value = true
  showExportMenu.value = false
  try {
    const res = await fetch(
      `${API_BASE_URL}/projects/${project.id}/export?format=${format}`,
      { headers: getAuthHeaders() },
    )
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      let msg = `导出失败 (${res.status})`
      try {
        const parsed = JSON.parse(text)
        if (parsed.detail) msg = typeof parsed.detail === 'string' ? parsed.detail : msg
      } catch { /* not JSON */ }
      throw new Error(msg)
    }
    // Extract filename from Content-Disposition header
    const disposition = res.headers.get('Content-Disposition') ?? ''
    let filename = `${project.title}.${format}`
    const match = disposition.match(/filename\*=UTF-8''(.+)/)
    if (match) filename = decodeURIComponent(match[1])

    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ui.showToast('导出成功', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '导出失败'), 'error')
  } finally {
    exporting.value = false
  }
}

function exportTxt() {
  exportFromBackend('txt')
}

function exportMarkdown() {
  exportFromBackend('md')
}

// New chapter form
const showNewChapter = ref(false)
const newTitle = ref('')
const newChapterNum = ref(1)
const newSummary = ref('')
const newChapterError = ref('')

function openNewChapterForm() {
  newChapterNum.value = chapterStore.nextChapterNum(projectId.value)
  newTitle.value = ''
  newSummary.value = ''
  newChapterError.value = ''
  showNewChapter.value = true
}

async function submitNewChapter() {
  if (!newTitle.value.trim()) {
    newChapterError.value = '章节标题不能为空'
    return
  }
  const payload = {
    title: newTitle.value.trim(),
    chapter_num: newChapterNum.value,
    summary: newSummary.value.trim() || undefined,
  }
  try {
    const ch = await chapterStore.createChapterRemote(projectId.value, payload)
    showNewChapter.value = false
    if (ch) selectChapter(ch.chapter_num)
    ui.showToast('章节创建成功', 'success')
  } catch (e: unknown) {
    const msg = friendlyError(e, '创建章节失败')
    if (msg.includes('已存在')) {
      newChapterError.value = '该章节序号已存在'
      ui.showToast('该章节序号已存在', 'error')
    } else {
      newChapterError.value = msg
      ui.showToast(msg, 'error')
    }
  }
}

// ─── Outline CRUD ───
const showNewOutline = ref(false)
const newOutlineSeq = ref(1)
const newOutlineTitle = ref('')
const newOutlineSummary = ref('')
const newOutlineTurningPoint = ref('')
const newOutlineError = ref('')

// Inline editing state for outlines
const editingOutlineId = ref<string | null>(null)
const editOutlineTitle = ref('')
const editOutlineSummary = ref('')
const editOutlineTurningPoint = ref('')

function openNewOutlineForm() {
  const existingSeqs = outline.value.map(o => o.chapter_num)
  newOutlineSeq.value = existingSeqs.length ? Math.max(...existingSeqs) + 1 : 1
  newOutlineTitle.value = ''
  newOutlineSummary.value = ''
  newOutlineTurningPoint.value = ''
  newOutlineError.value = ''
  showNewOutline.value = true
}

async function submitNewOutline() {
  if (!newOutlineTitle.value.trim()) {
    newOutlineError.value = '大纲标题不能为空'
    return
  }
  try {
    await outlineStore.createOutlineItem(projectId.value, {
      sequence_number: newOutlineSeq.value,
      title: newOutlineTitle.value.trim(),
      summary: newOutlineSummary.value.trim() || undefined,
      turning_point: newOutlineTurningPoint.value.trim() || undefined,
    })
    showNewOutline.value = false
    ui.showToast('大纲创建成功', 'success')
  } catch (e: unknown) {
    newOutlineError.value = friendlyError(e, '创建大纲失败')
    ui.showToast(friendlyError(e, '创建大纲失败'), 'error')
  }
}

function startEditOutline(item: OutlineItem) {
  editingOutlineId.value = item.id
  editOutlineTitle.value = item.title
  editOutlineSummary.value = item.summary
  editOutlineTurningPoint.value = item.turning_point ?? ''
}

function cancelEditOutline() {
  editingOutlineId.value = null
}

async function saveEditOutline(item: OutlineItem) {
  if (!editOutlineTitle.value.trim()) return
  try {
    await outlineStore.updateOutlineItem(projectId.value, item.id, {
      title: editOutlineTitle.value.trim(),
      summary: editOutlineSummary.value.trim() || undefined,
      turning_point: editOutlineTurningPoint.value.trim() || undefined,
    })
    editingOutlineId.value = null
    ui.showToast('大纲已更新', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '更新大纲失败'), 'error')
  }
}

async function deleteOutlineItemConfirm(item: OutlineItem) {
  try {
    await outlineStore.deleteOutlineItem(projectId.value, item.id)
    ui.showToast('大纲已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除大纲失败'), 'error')
  }
}

// ─── HiddenThread CRUD ───
const showNewHiddenThread = ref(false)
const newHTName = ref('')
const newHTDescription = ref('')
const newHTError = ref('')

function openNewHiddenThreadForm() {
  newHTName.value = ''
  newHTDescription.value = ''
  newHTError.value = ''
  showNewHiddenThread.value = true
}

async function submitNewHiddenThread() {
  if (!newHTName.value.trim()) {
    newHTError.value = '暗线名称不能为空'
    return
  }
  try {
    await hiddenThreadStore.createHiddenThreadItem(projectId.value, {
      name: newHTName.value.trim(),
      description: newHTDescription.value.trim() || undefined,
    })
    showNewHiddenThread.value = false
    ui.showToast('暗线创建成功', 'success')
  } catch (e: unknown) {
    newHTError.value = friendlyError(e, '创建暗线失败')
    ui.showToast(friendlyError(e, '创建暗线失败'), 'error')
  }
}

async function deleteHiddenThreadConfirm(ht: HiddenThread) {
  try {
    await hiddenThreadStore.deleteHiddenThreadItem(projectId.value, ht.id)
    ui.showToast('暗线已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除暗线失败'), 'error')
  }
}

// ─── Character CRUD ───
const showNewCharacter = ref(false)
const newCharName = ref('')
const newCharRoleType = ref<'protagonist' | 'antagonist' | 'supporting' | 'minor'>('supporting')
const newCharProfile = ref('')
const newCharFaction = ref('')
const newCharError = ref('')

const editingCharId = ref<string | null>(null)
const editCharName = ref('')
const editCharRoleType = ref<'protagonist' | 'antagonist' | 'supporting' | 'minor'>('supporting')
const editCharProfile = ref('')
const editCharFaction = ref('')

function openNewCharacterForm() {
  newCharName.value = ''
  newCharRoleType.value = 'supporting'
  newCharProfile.value = ''
  newCharFaction.value = ''
  newCharError.value = ''
  showNewCharacter.value = true
}

async function submitNewCharacter() {
  if (!newCharName.value.trim()) {
    newCharError.value = '角色名称不能为空'
    return
  }
  try {
    await characterStore.addCharacter(projectId.value, {
      name: newCharName.value.trim(),
      role_type: newCharRoleType.value,
      profile: newCharProfile.value.trim() || undefined,
      faction: newCharFaction.value.trim() || undefined,
    })
    showNewCharacter.value = false
    ui.showToast('角色创建成功', 'success')
  } catch (e: unknown) {
    newCharError.value = friendlyError(e, '创建角色失败')
    ui.showToast(friendlyError(e, '创建角色失败'), 'error')
  }
}

function startEditCharacter(char: Character) {
  editingCharId.value = char.id
  editCharName.value = char.name
  editCharRoleType.value = char.role_type
  editCharProfile.value = char.profile ?? ''
  editCharFaction.value = char.faction ?? ''
}

function cancelEditCharacter() {
  editingCharId.value = null
}

async function saveEditCharacter(char: Character) {
  if (!editCharName.value.trim()) return
  try {
    await characterStore.updateCharacter(projectId.value, char.id, {
      name: editCharName.value.trim(),
      role_type: editCharRoleType.value,
      profile: editCharProfile.value.trim() || undefined,
      faction: editCharFaction.value.trim() || undefined,
    })
    editingCharId.value = null
    ui.showToast('角色已更新', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '更新角色失败'), 'error')
  }
}

async function deleteCharacterConfirm(char: Character) {
  if (!window.confirm(`确定删除角色「${char.name}」？`)) return
  try {
    await characterStore.removeCharacter(projectId.value, char.id)
    ui.showToast('角色已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除角色失败'), 'error')
  }
}

// ─── WorldEntry CRUD ───
const showNewWorldEntry = ref(false)
const newWETitle = ref('')
const newWECategory = ref('')
const newWEContent = ref('')
const newWEConfidence = ref<'low' | 'medium' | 'high'>('medium')
const newWEError = ref('')

const editingWEId = ref<string | null>(null)
const editWETitle = ref('')
const editWECategory = ref('')
const editWEContent = ref('')
const editWEConfidence = ref<'low' | 'medium' | 'high'>('medium')

function openNewWorldEntryForm() {
  newWETitle.value = ''
  newWECategory.value = ''
  newWEContent.value = ''
  newWEConfidence.value = 'medium'
  newWEError.value = ''
  showNewWorldEntry.value = true
}

async function submitNewWorldEntry() {
  if (!newWETitle.value.trim()) {
    newWEError.value = '设定标题不能为空'
    return
  }
  try {
    await worldEntryStore.addWorldEntry(projectId.value, {
      title: newWETitle.value.trim(),
      category: newWECategory.value.trim() || undefined,
      content: newWEContent.value.trim(),
      confidence: newWEConfidence.value,
    })
    showNewWorldEntry.value = false
    ui.showToast('世界观创建成功', 'success')
  } catch (e: unknown) {
    newWEError.value = friendlyError(e, '创建世界观失败')
    ui.showToast(friendlyError(e, '创建世界观失败'), 'error')
  }
}

function startEditWorldEntry(entry: WorldEntry) {
  editingWEId.value = entry.id
  editWETitle.value = entry.title
  editWECategory.value = entry.category ?? ''
  editWEContent.value = entry.content ?? ''
  editWEConfidence.value = entry.confidence
}

function cancelEditWorldEntry() {
  editingWEId.value = null
}

async function saveEditWorldEntry(entry: WorldEntry) {
  if (!editWETitle.value.trim()) return
  try {
    await worldEntryStore.updateWorldEntry(projectId.value, entry.id, {
      title: editWETitle.value.trim(),
      category: editWECategory.value.trim() || undefined,
      content: editWEContent.value.trim(),
      confidence: editWEConfidence.value,
    })
    editingWEId.value = null
    ui.showToast('世界观已更新', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '更新世界观失败'), 'error')
  }
}

async function deleteWorldEntryConfirm(entry: WorldEntry) {
  if (!window.confirm(`确定删除设定「${entry.title}」？`)) return
  try {
    await worldEntryStore.removeWorldEntry(projectId.value, entry.id)
    ui.showToast('世界观已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除世界观失败'), 'error')
  }
}
</script>

<template>
  <!-- Desktop layout -->
  <div v-if="!isMobile" class="workspace-wrapper">
    <!-- Top bar -->
    <header class="top-bar">
      <div class="top-left">
        <router-link to="/projects" class="top-link">切换小说</router-link>
        <span class="top-sep">|</span>
        <span class="top-project-name">{{ currentProject?.title ?? '项目' }}</span>
        <span class="mode-badge" :class="projectMode">{{ projectMode === 'novel' ? '小说' : '文章' }}</span>
      </div>
      <div class="top-right">
        <div class="export-group">
          <button class="top-btn" :disabled="exporting" @click="showExportMenu = !showExportMenu">{{ exporting ? '导出中...' : '导出' }}</button>
          <div v-if="showExportMenu" class="export-dropdown">
            <button class="export-option" :disabled="exporting" @click="exportTxt">导出 TXT</button>
            <button class="export-option" :disabled="exporting" @click="exportMarkdown">导出 Markdown</button>
          </div>
        </div>
        <button class="top-btn top-btn-logout" @click="handleLogout">退出登录</button>
      </div>
    </header>

    <div class="workspace" :class="{ 'sidebar-collapsed': !sidebarOpen, 'agent-collapsed': !agentOpen }">
      <!-- Left sidebar column (always in DOM for grid integrity) -->
      <aside class="sidebar" :class="{ 'sidebar-hidden': !sidebarOpen }">
        <button class="sidebar-toggle" @click="sidebarOpen = !sidebarOpen" :title="sidebarOpen ? '收起侧栏' : '展开侧栏'">
          {{ sidebarOpen ? '‹' : '›' }}
        </button>
        <div class="sidebar-inner">
        <nav class="sidebar-tabs">
          <button :class="{ active: activeTab === 'chapters' }" @click="activeTab = 'chapters'">章节</button>
          <button :class="{ active: activeTab === 'outline' }" @click="activeTab = 'outline'">大纲</button>
          <button :class="{ active: activeTab === 'characters' }" @click="activeTab = 'characters'">角色</button>
          <button :class="{ active: activeTab === 'world' }" @click="activeTab = 'world'">世界观</button>
        </nav>

        <div class="sidebar-content">
          <!-- Chapters -->
          <div v-if="activeTab === 'chapters'" class="tab-pane">
            <button class="btn-add-chapter" @click="openNewChapterForm">+ 新增章节</button>
            <div v-if="showNewChapter" class="new-chapter-form">
              <div class="form-row">
                <label>章节标题 <span class="required">*</span></label>
                <input v-model="newTitle" type="text" placeholder="输入章节标题" class="form-input" />
              </div>
              <div class="form-row">
                <label>章节序号</label>
                <input v-model.number="newChapterNum" type="number" min="1" class="form-input form-input-sm" />
              </div>
              <div class="form-row">
                <label>章节大纲/摘要</label>
                <textarea v-model="newSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
              </div>
              <div v-if="newChapterError" class="form-error">{{ newChapterError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewChapter">确定</button>
                <button class="btn-cancel" @click="showNewChapter = false">取消</button>
              </div>
            </div>
            <div
              v-for="ch in chapters"
              :key="ch.id"
              class="chapter-item"
              :class="{ active: ch.chapter_num === chapterStore.currentChapterNum }"
              @click="selectChapter(ch.chapter_num)"
            >
              <span class="ch-num">{{ ch.chapter_num }}</span>
              <span class="ch-title">{{ ch.title }}</span>
              <span class="ch-status" :class="ch.status">{{ ch.status === 'final' ? '终稿' : ch.status === 'reviewing' ? '审核' : ch.status === 'revision' ? '修订' : '草稿' }}</span>
            </div>
          </div>

          <!-- Outline -->
          <div v-if="activeTab === 'outline'" class="tab-pane">
            <button class="btn-add-chapter" @click="openNewOutlineForm">+ 添加章节大纲</button>
            <div v-if="showNewOutline" class="new-chapter-form">
              <div class="form-row">
                <label>大纲标题 <span class="required">*</span></label>
                <input v-model="newOutlineTitle" type="text" placeholder="输入大纲标题" class="form-input" />
              </div>
              <div class="form-row">
                <label>章节序号</label>
                <input v-model.number="newOutlineSeq" type="number" min="1" class="form-input form-input-sm" />
              </div>
              <div class="form-row">
                <label>摘要</label>
                <textarea v-model="newOutlineSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
              </div>
              <div class="form-row">
                <label>转折点</label>
                <input v-model="newOutlineTurningPoint" type="text" placeholder="可选" class="form-input" />
              </div>
              <div v-if="newOutlineError" class="form-error">{{ newOutlineError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewOutline">确定</button>
                <button class="btn-cancel" @click="showNewOutline = false">取消</button>
              </div>
            </div>
            <div v-for="item in outline" :key="item.chapter_num" class="outline-item">
              <template v-if="editingOutlineId === item.id">
                <div class="new-chapter-form" style="margin: 0;">
                  <div class="form-row">
                    <label>标题 <span class="required">*</span></label>
                    <input v-model="editOutlineTitle" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>摘要</label>
                    <textarea v-model="editOutlineSummary" class="form-textarea" rows="2"></textarea>
                  </div>
                  <div class="form-row">
                    <label>转折点</label>
                    <input v-model="editOutlineTurningPoint" type="text" class="form-input" />
                  </div>
                  <div class="form-actions">
                    <button class="btn-submit" @click="saveEditOutline(item)">保存</button>
                    <button class="btn-cancel" @click="cancelEditOutline">取消</button>
                  </div>
                </div>
              </template>
              <template v-else>
                <div class="outline-header">
                  <span class="outline-num">{{ item.chapter_num }}</span>
                  <span class="outline-title">{{ item.title }}</span>
                  <span class="outline-actions">
                    <button class="icon-btn" title="编辑" @click.stop="startEditOutline(item)">&#9998;</button>
                    <button class="icon-btn icon-btn-danger" title="删除" @click.stop="deleteOutlineItemConfirm(item)">&#10005;</button>
                  </span>
                </div>
                <p class="outline-summary">{{ item.summary }}</p>
                <div v-if="item.turning_point" class="turning-point">
                  <span class="tp-icon">↯</span> {{ item.turning_point }}
                </div>
                <div v-if="item.hidden_thread_ids.length" class="hidden-threads">
                  <span v-for="htId in item.hidden_thread_ids" :key="htId" class="thread-tag">
                    {{ hiddenThreads.find(ht => ht.id === htId)?.name ?? htId }}
                  </span>
                </div>
              </template>
            </div>

            <!-- Hidden threads sub-section within outline tab -->
            <div class="hidden-thread-section">
              <div class="section-header">
                <span class="section-title">暗线</span>
                <button class="btn-add-inline" @click="openNewHiddenThreadForm">+ 添加暗线</button>
              </div>
              <div v-if="showNewHiddenThread" class="new-chapter-form">
                <div class="form-row">
                  <label>暗线名称 <span class="required">*</span></label>
                  <input v-model="newHTName" type="text" placeholder="输入暗线名称" class="form-input" />
                </div>
                <div class="form-row">
                  <label>描述</label>
                  <textarea v-model="newHTDescription" placeholder="可选" class="form-textarea" rows="2"></textarea>
                </div>
                <div v-if="newHTError" class="form-error">{{ newHTError }}</div>
                <div class="form-actions">
                  <button class="btn-submit" @click="submitNewHiddenThread">确定</button>
                  <button class="btn-cancel" @click="showNewHiddenThread = false">取消</button>
                </div>
              </div>
              <div v-for="ht in hiddenThreads" :key="ht.id" class="ht-item">
                <div class="ht-header">
                  <span class="ht-name">{{ ht.name }}</span>
                  <button class="icon-btn icon-btn-danger" title="删除" @click.stop="deleteHiddenThreadConfirm(ht)">&#10005;</button>
                </div>
                <p v-if="ht.description" class="ht-desc">{{ ht.description }}</p>
              </div>
              <div v-if="!hiddenThreads.length && !showNewHiddenThread" class="empty-hint" style="padding: var(--sp-4) 0;">暂无暗线</div>
            </div>
          </div>

          <!-- Characters -->
          <div v-if="activeTab === 'characters'" class="tab-pane">
            <button class="btn-add-chapter" @click="openNewCharacterForm">+ 新增角色</button>
            <div v-if="showNewCharacter" class="new-chapter-form" style="margin: 0;">
              <div class="form-row">
                <label>角色名称 <span class="required">*</span></label>
                <input v-model="newCharName" type="text" placeholder="输入角色名称" class="form-input" />
              </div>
              <div class="form-row">
                <label>角色类型</label>
                <select v-model="newCharRoleType" class="form-input">
                  <option value="protagonist">主角</option>
                  <option value="antagonist">反派</option>
                  <option value="supporting">配角</option>
                  <option value="minor">路人</option>
                </select>
              </div>
              <div class="form-row">
                <label>阵营</label>
                <input v-model="newCharFaction" type="text" placeholder="可选" class="form-input" />
              </div>
              <div class="form-row">
                <label>简介</label>
                <textarea v-model="newCharProfile" class="form-textarea" rows="3" placeholder="角色描述"></textarea>
              </div>
              <div v-if="newCharError" class="form-error">{{ newCharError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewCharacter">确定</button>
                <button class="btn-cancel" @click="showNewCharacter = false">取消</button>
              </div>
            </div>
            <div v-for="char in characters" :key="char.id" class="character-card">
              <template v-if="editingCharId === char.id">
                <div class="new-chapter-form" style="margin: 0;">
                  <div class="form-row">
                    <label>角色名称 <span class="required">*</span></label>
                    <input v-model="editCharName" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>角色类型</label>
                    <select v-model="editCharRoleType" class="form-input">
                      <option value="protagonist">主角</option>
                      <option value="antagonist">反派</option>
                      <option value="supporting">配角</option>
                      <option value="minor">路人</option>
                    </select>
                  </div>
                  <div class="form-row">
                    <label>阵营</label>
                    <input v-model="editCharFaction" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>简介</label>
                    <textarea v-model="editCharProfile" class="form-textarea" rows="3"></textarea>
                  </div>
                  <div class="form-actions">
                    <button class="btn-submit" @click="saveEditCharacter(char)">保存</button>
                    <button class="btn-cancel" @click="cancelEditCharacter">取消</button>
                  </div>
                </div>
              </template>
              <template v-else>
                <div class="char-name">
                  {{ char.name }}
                  <span class="char-role-type" :class="char.role_type">{{ { protagonist: '主角', antagonist: '反派', supporting: '配角', minor: '路人' }[char.role_type] }}</span>
                  <span class="outline-actions">
                    <button class="icon-btn" title="编辑" @click.stop="startEditCharacter(char)">&#9998;</button>
                    <button class="icon-btn icon-btn-danger" title="删除" @click.stop="deleteCharacterConfirm(char)">&#10005;</button>
                  </span>
                </div>
                <p v-if="char.faction" class="char-faction">阵营: {{ char.faction }}</p>
                <p class="char-profile">{{ char.profile }}</p>
                <div v-if="char.appearance_count" class="char-appearance">出场次数: {{ char.appearance_count }}</div>
              </template>
            </div>
            <div v-if="!characters.length && !showNewCharacter" class="empty-hint">暂无角色</div>
          </div>

          <!-- World -->
          <div v-if="activeTab === 'world'" class="tab-pane">
            <button class="btn-add-chapter" @click="openNewWorldEntryForm">+ 新增设定</button>
            <div v-if="showNewWorldEntry" class="new-chapter-form" style="margin: 0;">
              <div class="form-row">
                <label>设定标题 <span class="required">*</span></label>
                <input v-model="newWETitle" type="text" placeholder="输入设定标题" class="form-input" />
              </div>
              <div class="form-row">
                <label>分类</label>
                <input v-model="newWECategory" type="text" placeholder="可选，如：地理、历史、魔法" class="form-input" />
              </div>
              <div class="form-row">
                <label>内容</label>
                <textarea v-model="newWEContent" class="form-textarea" rows="3" placeholder="世界观设定内容"></textarea>
              </div>
              <div class="form-row">
                <label>可信度</label>
                <select v-model="newWEConfidence" class="form-input">
                  <option value="low">低</option>
                  <option value="medium">中</option>
                  <option value="high">高</option>
                </select>
              </div>
              <div v-if="newWEError" class="form-error">{{ newWEError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewWorldEntry">确定</button>
                <button class="btn-cancel" @click="showNewWorldEntry = false">取消</button>
              </div>
            </div>
            <div v-for="entry in worldEntries" :key="entry.id" class="world-block">
              <template v-if="editingWEId === entry.id">
                <div class="new-chapter-form" style="margin: 0;">
                  <div class="form-row">
                    <label>设定标题 <span class="required">*</span></label>
                    <input v-model="editWETitle" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>分类</label>
                    <input v-model="editWECategory" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>内容</label>
                    <textarea v-model="editWEContent" class="form-textarea" rows="3"></textarea>
                  </div>
                  <div class="form-row">
                    <label>可信度</label>
                    <select v-model="editWEConfidence" class="form-input">
                      <option value="low">低</option>
                      <option value="medium">中</option>
                      <option value="high">高</option>
                    </select>
                  </div>
                  <div class="form-actions">
                    <button class="btn-submit" @click="saveEditWorldEntry(entry)">保存</button>
                    <button class="btn-cancel" @click="cancelEditWorldEntry">取消</button>
                  </div>
                </div>
              </template>
              <template v-else>
                <div class="world-label">
                  {{ entry.title }}
                  <span v-if="entry.category" class="world-category">· {{ entry.category }}</span>
                  <span class="we-confidence" :class="entry.confidence">{{ { low: '低', medium: '中', high: '高' }[entry.confidence] }}</span>
                  <span class="outline-actions">
                    <button class="icon-btn" title="编辑" @click.stop="startEditWorldEntry(entry)">&#9998;</button>
                    <button class="icon-btn icon-btn-danger" title="删除" @click.stop="deleteWorldEntryConfirm(entry)">&#10005;</button>
                  </span>
                </div>
                <p class="world-text">{{ entry.content }}</p>
              </template>
            </div>
            <div v-if="!worldEntries.length && !showNewWorldEntry" class="empty-hint">暂无世界观设定</div>
          </div>
        </div>
        </div>
      </aside>

      <!-- Center: editor -->
      <main class="editor-area">
        <NovelEditor v-if="currentChapter" :project-id="projectId" :mode="projectMode" />
        <div v-else class="empty-hint" style="padding-top: 120px;">选择一个章节开始写作</div>
      </main>

      <!-- Right: agent panel (always in DOM for grid integrity) -->
      <aside class="agent-area" :class="{ 'agent-hidden': !agentOpen }">
        <button class="agent-toggle" @click="agentOpen = !agentOpen" :title="agentOpen ? '收起面板' : '展开 Agent 面板'">
          {{ agentOpen ? '›' : '‹' }}
        </button>
        <div class="agent-content">
          <AgentPanel :project-id="projectId" />
        </div>
      </aside>
    </div>
  </div>

  <!-- Mobile layout -->
  <div v-else class="workspace-mobile">
    <!-- Mobile top bar -->
    <header class="mobile-top-bar">
      <router-link to="/projects" class="top-link">切换小说</router-link>
      <span class="mode-badge" :class="projectMode">{{ projectMode === 'novel' ? '小说' : '文章' }}</span>
      <div class="export-group">
        <button class="top-btn" @click="showExportMenu = !showExportMenu">导出</button>
        <div v-if="showExportMenu" class="export-dropdown">
          <button class="export-option" @click="exportTxt">导出 TXT</button>
          <button class="export-option" @click="exportMarkdown">导出 Markdown</button>
        </div>
      </div>
      <button class="top-btn top-btn-logout" @click="handleLogout">退出登录</button>
    </header>

    <nav class="mobile-nav">
      <button :class="{ active: mobilePanel === 'chapters' }" @click="mobilePanel = 'chapters'">章节</button>
      <button :class="{ active: mobilePanel === 'characters' }" @click="mobilePanel = 'characters'">角色</button>
      <button :class="{ active: mobilePanel === 'world' }" @click="mobilePanel = 'world'">世界观</button>
      <button :class="{ active: mobilePanel === 'editor' }" @click="mobilePanel = 'editor'">编辑</button>
      <button :class="{ active: mobilePanel === 'agent' }" @click="mobilePanel = 'agent'">Agent</button>
    </nav>

    <div v-if="mobilePanel === 'chapters'" class="mobile-pane">
      <button class="btn-add-chapter" @click="openNewChapterForm">+ 新增章节</button>
      <div v-if="showNewChapter" class="new-chapter-form">
        <div class="form-row">
          <label>章节标题 <span class="required">*</span></label>
          <input v-model="newTitle" type="text" placeholder="输入章节标题" class="form-input" />
        </div>
        <div class="form-row">
          <label>章节序号</label>
          <input v-model.number="newChapterNum" type="number" min="1" class="form-input form-input-sm" />
        </div>
        <div class="form-row">
          <label>章节大纲/摘要</label>
          <textarea v-model="newSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
        </div>
        <div v-if="newChapterError" class="form-error">{{ newChapterError }}</div>
        <div class="form-actions">
          <button class="btn-submit" @click="submitNewChapter">确定</button>
          <button class="btn-cancel" @click="showNewChapter = false">取消</button>
        </div>
      </div>
      <div
        v-for="ch in chapters"
        :key="ch.id"
        class="chapter-item"
        :class="{ active: ch.chapter_num === chapterStore.currentChapterNum }"
        @click="selectChapter(ch.chapter_num)"
      >
        <span class="ch-num">{{ ch.chapter_num }}</span>
        <span class="ch-title">{{ ch.title }}</span>
        <span class="ch-status" :class="ch.status">{{ ch.status === 'final' ? '终稿' : '草稿' }}</span>
      </div>
    </div>

    <div v-if="mobilePanel === 'editor'" class="mobile-pane mobile-editor">
      <NovelEditor v-if="currentChapter" :project-id="projectId" :mode="projectMode" />
      <div v-else class="empty-hint">选择一个章节开始写作</div>
    </div>

    <div v-if="mobilePanel === 'agent'" class="mobile-pane">
      <AgentPanel :project-id="projectId" />
    </div>
  </div>
</template>

<style scoped>
/* ─── Top bar ─── */
.top-bar, .mobile-top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-2) var(--sp-4);
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.top-left, .top-right {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.top-link {
  font-size: var(--text-sm);
  color: var(--accent);
  white-space: nowrap;
}
.top-link:hover { text-decoration: none; opacity: 0.85; }
.top-sep { color: var(--text-tertiary); font-size: var(--text-xs); }
.top-project-name {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text);
}
.mode-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
}
.mode-badge.novel { background: var(--status-final-bg); color: var(--status-final); }
.mode-badge.article { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.top-btn {
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
}
.top-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
}
.top-btn-logout { color: var(--text-tertiary); }
.export-group { position: relative; }
.export-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: var(--sp-1);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  z-index: 20;
  min-width: 140px;
}
.export-option {
  display: block;
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: none;
  font-size: var(--text-sm);
  color: var(--text);
  cursor: pointer;
  text-align: left;
}
.export-option:hover { background: var(--bg-hover); }

.mobile-top-bar {
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  flex-wrap: wrap;
}

/* ─── Desktop ─── */
.workspace-wrapper {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg);
}
.workspace {
  display: grid;
  grid-template-columns: 220px 1fr 300px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  transition: grid-template-columns 220ms ease;
}
.workspace.sidebar-collapsed { grid-template-columns: 0px 1fr 300px; }
.workspace.agent-collapsed { grid-template-columns: 220px 1fr 0px; }
.workspace.sidebar-collapsed.agent-collapsed { grid-template-columns: 0px 1fr 0px; }

/* Sidebar & agent toggle buttons */
.sidebar-toggle, .agent-toggle {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  z-index: 10;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  width: 24px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), opacity 220ms ease;
  padding: 0;
}
.sidebar-toggle {
  right: -12px; /* overlap the border */
}
.agent-toggle {
  left: -12px; /* overlap the border */
}

.sidebar-toggle:hover, .agent-toggle:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}

/* Sidebar */
.sidebar {
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: visible;
  position: relative;
  min-width: 0;
}
.sidebar.sidebar-hidden {
  border-right: none;
}
.sidebar-hidden .sidebar-toggle {
  /* When collapsed, the toggle button sits on the right edge of the 0px column */
  right: -24px;
}
.sidebar-inner {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.sidebar.sidebar-hidden .sidebar-inner {
  visibility: hidden;
}
.sidebar-tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.sidebar-tabs button {
  flex: 1;
  padding: var(--sp-2) var(--sp-1);
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}
.sidebar-tabs button:hover { color: var(--text-secondary); }
.sidebar-tabs button.active {
  color: var(--text);
  border-bottom-color: var(--accent);
}
.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-2);
  min-width: 0;
}
.tab-pane { display: flex; flex-direction: column; gap: var(--sp-1); }

/* Chapter items */
.chapter-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  cursor: pointer;
  font-size: var(--text-sm);
  transition: background var(--transition);
}
.chapter-item:hover { background: var(--bg-hover); }
.chapter-item.active {
  background: var(--accent-subtle);
  color: var(--accent);
  font-weight: 600;
}
.ch-num {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  min-width: 18px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.chapter-item.active .ch-num { color: var(--accent); }
.ch-title { flex: 1; }
.ch-status {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.ch-status.draft { background: var(--status-draft-bg); color: var(--status-draft); }
.ch-status.reviewing { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.ch-status.revision { background: var(--status-revision-bg); color: var(--status-revision); }
.ch-status.final { background: var(--status-final-bg); color: var(--status-final); }

/* Outline */
.outline-item {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  border-left: 2px solid var(--border);
}
.outline-header {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.outline-num {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.outline-summary {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: var(--sp-1) 0 0;
  line-height: 1.5;
}
.turning-point {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  margin-top: var(--sp-1);
}
.tp-icon { font-weight: 700; }
.hidden-threads {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  margin-top: var(--sp-1);
}
.thread-tag {
  font-size: 10px;
  background: #f5f0ff;
  color: #7c3aed;
  padding: 1px 6px;
  border-radius: 8px;
}

/* Characters */
.character-card {
  padding: var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.char-name {
  font-size: var(--text-sm);
  font-weight: 600;
  margin-bottom: var(--sp-1);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.char-role-type {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.char-role-type.protagonist { background: var(--status-final-bg); color: var(--status-final); }
.char-role-type.antagonist { background: var(--status-error-bg, #fef2f2); color: var(--status-error, #ef4444); }
.char-role-type.supporting { background: var(--accent-subtle); color: var(--accent); }
.char-role-type.minor { background: var(--bg-hover); color: var(--text-tertiary); }
.char-faction {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin: 0 0 var(--sp-1);
}
.char-profile {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: 0;
  line-height: 1.5;
}
.char-appearance {
  font-size: 10px;
  color: var(--text-tertiary);
  margin-top: var(--sp-1);
}

/* World */
.world-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--sp-2);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.we-confidence {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.we-confidence.high { background: var(--status-final-bg); color: var(--status-final); }
.we-confidence.medium { background: var(--accent-subtle); color: var(--accent); }
.we-confidence.low { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.world-category {
  font-weight: 400;
  text-transform: none;
  letter-spacing: normal;
  color: var(--text-tertiary);
}
.world-text {
  font-size: var(--text-sm);
  line-height: 1.7;
  color: var(--text-secondary);
}
.world-block {
  padding: var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.empty-hint {
  text-align: center;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  padding: var(--sp-8) var(--sp-4);
}

/* New chapter form */
.btn-add-chapter {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color var(--transition), color var(--transition);
}
.btn-add-chapter:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.new-chapter-form {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.form-row label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-secondary);
}
.required { color: var(--status-reviewing); }
.form-input, .form-textarea {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  transition: border-color var(--transition);
}
.form-input:focus, .form-textarea:focus {
  border-color: var(--border-focus);
  outline: none;
}
.form-input-sm { width: 80px; }
.form-textarea { resize: vertical; }
.form-error {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
}
.form-actions {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
}
.btn-submit {
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
.btn-submit:hover { background: var(--accent-hover); }
.btn-cancel {
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-cancel:hover { background: var(--bg-hover); }

/* Editor area */
.editor-area {
  overflow-y: auto;
  background: var(--bg-panel);
  min-width: 0;
}

/* Agent area */
.agent-area {
  border-left: 1px solid var(--border);
  background: var(--bg-sidebar);
  overflow: visible;
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.agent-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--sp-4);
  min-width: 0;
}
.agent-area.agent-hidden {
  border-left: none;
}
.agent-area.agent-hidden .agent-content {
  visibility: hidden;
}
.agent-hidden .agent-toggle {
  left: -24px;
}

/* ─── Mobile ─── */
.workspace-mobile {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg);
}
.mobile-nav {
  display: flex;
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
  flex-shrink: 0;
}
.mobile-nav button {
  flex: 1;
  padding: var(--sp-3);
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-tertiary);
  cursor: pointer;
}
.mobile-nav button.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.mobile-pane {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-3);
}
.mobile-editor {
  padding: 0;
}

/* Outline actions (edit/delete icons) */
.outline-actions {
  margin-left: auto;
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}

/* Icon button (edit/delete) */
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  transition: background var(--transition), color var(--transition);
  padding: 0;
}
.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.icon-btn-danger:hover {
  background: var(--status-error-bg, #fef2f2);
  color: var(--status-error, #ef4444);
}

/* Hidden thread section */
.hidden-thread-section {
  margin-top: var(--sp-4);
  border-top: 1px solid var(--border);
  padding-top: var(--sp-3);
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-2);
}
.section-title {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.btn-add-inline {
  border: none;
  background: none;
  font-size: var(--text-xs);
  color: var(--accent);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  transition: background var(--transition);
}
.btn-add-inline:hover {
  background: var(--accent-subtle);
}
.ht-item {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  border-left: 2px solid #7c3aed;
}
.ht-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ht-name {
  font-size: var(--text-sm);
  font-weight: 600;
  color: #7c3aed;
}
.ht-desc {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: var(--sp-1) 0 0;
  line-height: 1.5;
}
</style>