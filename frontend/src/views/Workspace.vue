<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave } from 'vue-router'
import { useProjectStore, useChapterStore, useDocumentStore, useRelationStore, useWorldEntryStore, useCharacterStore, useOutlineStore, useHiddenThreadStore, useExpertStore, useUiStore, friendlyError } from '../stores'
import { API_BASE_URL, type OutlineItem, type HiddenThread, type Character, type WorldEntry, type WritingUnit } from '../api/types'
import WritingEditor from '../components/WritingEditor.vue'
import AgentPanel from '../components/AgentPanel.vue'
import ConfirmModal from '../components/ConfirmModal.vue'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()
const chapterStore = useChapterStore()
const documentStore = useDocumentStore()
const relationStore = useRelationStore()
const worldEntryStore = useWorldEntryStore()
const characterStore = useCharacterStore()
const outlineStore = useOutlineStore()
const hiddenThreadStore = useHiddenThreadStore()
const expertStore = useExpertStore()
const ui = useUiStore()

const agentPanelRef = ref<InstanceType<typeof AgentPanel> | null>(null)

const projectId = computed(() => route.params.id as string)
const currentProject = computed(() => projectStore.projects.find(p => p.id === projectId.value))
const projectMode = computed(() => currentProject.value?.mode ?? 'novel')
const unitLabel = computed(() => projectMode.value === 'article' ? '稿件' : '章节')
const switchProjectLabel = computed(() => projectMode.value === 'article' ? '切换项目' : '切换小说')
const unitSequenceLabel = computed(() => `${unitLabel.value}序号`)
const unitSummaryLabel = computed(() => projectMode.value === 'article' ? '内容摘要' : '章节大纲/摘要')
const outlineLabel = computed(() => projectMode.value === 'article' ? '内容结构' : '大纲')
const outlineAddLabel = computed(() => projectMode.value === 'article' ? '添加选题规划' : `添加${unitLabel.value}大纲`)
const outlineTitleLabel = computed(() => projectMode.value === 'article' ? '选题标题' : '大纲标题')
const turningPointLabel = computed(() => projectMode.value === 'article' ? '核心角度' : '转折点')
const hiddenThreadLabel = computed(() => projectMode.value === 'article' ? '内容策略' : '暗线')
const characterLabel = computed(() => projectMode.value === 'article' ? '受众画像' : '角色')
const worldLabel = computed(() => projectMode.value === 'article' ? '品牌/产品资料' : '世界观')
const worldEntryLabel = computed(() => projectMode.value === 'article' ? '资料' : '设定')
const isGenerating = computed(() => expertStore.getState(projectId.value).isGenerating)
const sidebarOpen = ref(true)
const agentOpen = ref(true)
const activeTab = ref<'units' | 'outline' | 'characters' | 'world'>('units')
const projectsLoading = ref(false)

// Mobile: which panel to show
const mobilePanel = ref<'editor' | 'units' | 'characters' | 'world' | 'agent'>('editor')

watch(projectId, (id) => {
  projectStore.setCurrent(id)
  worldEntryStore.loadWorldEntries(id)
  characterStore.loadCharacters(id)
  relationStore.loadRelations(id)
  outlineStore.loadOutlines(id)
  hiddenThreadStore.loadHiddenThreads(id)
}, { immediate: true })

async function ensureProjectsLoaded() {
  if (currentProject.value || projectsLoading.value) return
  projectsLoading.value = true
  try {
    await projectStore.loadProjects()
  } finally {
    projectsLoading.value = false
  }
}

watch([projectId, currentProject], ([id, project]) => {
  if (!project) {
    ensureProjectsLoaded()
    return
  }
  if (project.mode === 'article') {
    documentStore.loadDocuments(id)
  } else {
    chapterStore.loadChapters(id)
  }
}, { immediate: true })

const writingUnits = computed(() => projectMode.value === 'article' ? documentStore.documentsForProject(projectId.value) : chapterStore.chaptersForProject(projectId.value))
const currentWritingUnit = computed(() => projectMode.value === 'article' ? documentStore.currentDocumentForProject(projectId.value) : chapterStore.currentChapterForProject(projectId.value))
const currentUnitNum = computed(() => projectMode.value === 'article' ? documentStore.currentDocumentPosition : chapterStore.currentChapterNum)
const characters = computed(() => characterStore.charactersForProject(projectId.value))
const outline = computed(() => outlineStore.entriesForProject(projectId.value))
const worldEntries = computed(() => worldEntryStore.entriesForProject(projectId.value))
const hiddenThreads = computed(() => hiddenThreadStore.threadsForProject(projectId.value))

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

function selectWritingUnit(num: number) {
  if (projectMode.value === 'article') {
    documentStore.setCurrentDocument(num)
  } else {
    chapterStore.setCurrentChapter(num)
  }
  mobilePanel.value = 'editor'
}

const isMobile = ref(window.innerWidth < 900)
function onResize() { isMobile.value = window.innerWidth < 900 }
onMounted(() => {
  window.addEventListener('resize', onResize)
})
onUnmounted(() => window.removeEventListener('resize', onResize))

// ─── Generation guard ───

function cancelGenerationAndProceed(callback: () => void) {
  agentPanelRef.value?.cancelStream()
  callback()
}

function confirmLeaveIfGenerating(callback: () => void) {
  if (!isGenerating.value) {
    callback()
    return
  }
  showConfirm(`当前正在${projectMode.value === 'article' ? '生成内容' : '生成小说'}，退出将取消生成，是否继续？`, () => cancelGenerationAndProceed(callback), '继续退出', true)
}

onBeforeRouteLeave(() => {
  if (isGenerating.value) {
    showConfirm(`当前正在${projectMode.value === 'article' ? '生成内容' : '生成小说'}，退出将取消生成，是否继续？`, () => {
      agentPanelRef.value?.cancelStream()
      router.push('/projects')
    }, '继续退出', true)
    return false
  }
})

function onBeforeUnload(e: BeforeUnloadEvent) {
  if (isGenerating.value) {
    e.preventDefault()
  }
}
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))

function handleSwitchProject() {
  confirmLeaveIfGenerating(() => router.push('/projects'))
}

// Navigation
function handleLogout() {
  confirmLeaveIfGenerating(() => {
    localStorage.removeItem('ai_write_logged_in')
    localStorage.removeItem('ai_write_token')
    router.push('/login')
  })
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

// New writing unit form
const showNewUnit = ref(false)
const newTitle = ref('')
const newUnitNum = ref(1)
const newSummary = ref('')
const newUnitError = ref('')

function openNewUnitForm() {
  newUnitNum.value = projectMode.value === 'article'
    ? documentStore.nextDocumentPosition(projectId.value)
    : chapterStore.nextChapterNum(projectId.value)
  newTitle.value = ''
  newSummary.value = ''
  newUnitError.value = ''
  showNewUnit.value = true
}

async function submitNewUnit() {
  if (!newTitle.value.trim()) {
    newUnitError.value = `${unitLabel.value}标题不能为空`
    return
  }
  const payload = {
    title: newTitle.value.trim(),
    position: newUnitNum.value,
    summary: newSummary.value.trim() || undefined,
  }
  try {
    const ch = projectMode.value === 'article'
      ? await documentStore.createDocumentRemote(projectId.value, {
          title: payload.title,
          position: payload.position,
        })
      : await chapterStore.createChapterRemote(projectId.value, {
          title: payload.title,
          chapter_num: payload.position,
          summary: payload.summary,
        })
    showNewUnit.value = false
    if (ch) selectWritingUnit(unitPosition(ch))
    ui.showToast(`${unitLabel.value}创建成功`, 'success')
  } catch (e: unknown) {
    const msg = friendlyError(e, `创建${unitLabel.value}失败`)
    if (msg.includes('已存在')) {
      newUnitError.value = `该${unitLabel.value}序号已存在`
      ui.showToast(`该${unitLabel.value}序号已存在`, 'error')
    } else {
      newUnitError.value = msg
      ui.showToast(msg, 'error')
    }
  }
}

// ─── Writing unit title inline edit ───
const editingUnitId = ref<string | null>(null)
const editUnitTitle = ref('')
const unitMenu = ref<{ x: number; y: number; unit: WritingUnit } | null>(null)

// ─── Confirm modal ───
const confirmState = ref<{ message: string; confirmText: string; danger: boolean; onConfirm: () => void } | null>(null)

function showConfirm(message: string, onConfirm: () => void, confirmText = '确定', danger = false) {
  confirmState.value = { message, confirmText, danger, onConfirm }
}

function handleConfirmOk() {
  if (!confirmState.value) return
  const cb = confirmState.value.onConfirm
  confirmState.value = null
  cb()
}

function handleConfirmCancel() {
  confirmState.value = null
}

function openUnitMenu(e: MouseEvent, unit: WritingUnit) {
  unitMenu.value = { x: e.clientX, y: e.clientY, unit }
}

function closeUnitMenu() {
  unitMenu.value = null
}

function menuEditUnit() {
  if (!unitMenu.value) return
  startEditUnit(unitMenu.value.unit)
  closeUnitMenu()
}

function menuDeleteUnit() {
  if (!unitMenu.value) return
  const unit = unitMenu.value.unit
  closeUnitMenu()
  deleteUnitConfirm(unit)
}

function startEditUnit(ch: WritingUnit) {
  editingUnitId.value = ch.id
  editUnitTitle.value = ch.title
}

function cancelEditUnit() {
  editingUnitId.value = null
}

async function saveEditUnit(ch: WritingUnit) {
  const title = editUnitTitle.value.trim()
  if (!title) return
  const position = unitPosition(ch)
  try {
    if (projectMode.value === 'article') {
      await documentStore.updateDocumentTitle(projectId.value, position, title)
    } else {
      await chapterStore.updateChapterTitle(projectId.value, position, title)
    }
    editingUnitId.value = null
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '修改标题失败'), 'error')
  }
}

function deleteUnitConfirm(ch: WritingUnit) {
  const position = unitPosition(ch)
  const label = projectMode.value === 'article'
    ? `${unitLabel.value} ${ch.title}`
    : `第${position}${unitLabel.value} ${ch.title}`
  showConfirm(`确定删除「${label}」吗？`, () => {
    const deleteTask = projectMode.value === 'article'
      ? documentStore.deleteDocumentRemote(projectId.value, position)
      : chapterStore.deleteChapterRemote(projectId.value, position)
    deleteTask.catch((e: unknown) => {
      ui.showToast(friendlyError(e, `删除${unitLabel.value}失败`), 'error')
    })
  }, '删除', true)
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
    newOutlineError.value = `${outlineTitleLabel.value}不能为空`
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
    ui.showToast(`${outlineLabel.value}创建成功`, 'success')
  } catch (e: unknown) {
    newOutlineError.value = friendlyError(e, `创建${outlineLabel.value}失败`)
    ui.showToast(friendlyError(e, `创建${outlineLabel.value}失败`), 'error')
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
    ui.showToast(`${outlineLabel.value}已更新`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, `更新${outlineLabel.value}失败`), 'error')
  }
}

async function deleteOutlineItemConfirm(item: OutlineItem) {
  try {
    await outlineStore.deleteOutlineItem(projectId.value, item.id)
    ui.showToast(`${outlineLabel.value}已删除`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, `删除${outlineLabel.value}失败`), 'error')
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
    newHTError.value = `${hiddenThreadLabel.value}名称不能为空`
    return
  }
  try {
    await hiddenThreadStore.createHiddenThreadItem(projectId.value, {
      name: newHTName.value.trim(),
      description: newHTDescription.value.trim() || undefined,
    })
    showNewHiddenThread.value = false
    ui.showToast(`${hiddenThreadLabel.value}创建成功`, 'success')
  } catch (e: unknown) {
    newHTError.value = friendlyError(e, `创建${hiddenThreadLabel.value}失败`)
    ui.showToast(friendlyError(e, `创建${hiddenThreadLabel.value}失败`), 'error')
  }
}

async function deleteHiddenThreadConfirm(ht: HiddenThread) {
  try {
    await hiddenThreadStore.deleteHiddenThreadItem(projectId.value, ht.id)
    ui.showToast(`${hiddenThreadLabel.value}已删除`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, `删除${hiddenThreadLabel.value}失败`), 'error')
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
    newCharError.value = `${characterLabel.value}名称不能为空`
    return
  }
  try {
    await characterStore.addCharacter(projectId.value, {
      name: newCharName.value.trim(),
      role_type: newCharRoleType.value,
      profile: newCharProfile.value.trim(),
      faction: newCharFaction.value.trim(),
    })
    await characterStore.loadCharacters(projectId.value)
    showNewCharacter.value = false
    ui.showToast(`${characterLabel.value}创建成功`, 'success')
  } catch (e: unknown) {
    newCharError.value = friendlyError(e, `创建${characterLabel.value}失败`)
    ui.showToast(friendlyError(e, `创建${characterLabel.value}失败`), 'error')
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
      profile: editCharProfile.value.trim(),
      faction: editCharFaction.value.trim(),
    })
    await characterStore.loadCharacters(projectId.value)
    editingCharId.value = null
    ui.showToast(`${characterLabel.value}已更新`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, `更新${characterLabel.value}失败`), 'error')
  }
}

async function deleteCharacterConfirm(char: Character) {
  showConfirm(`确定删除${characterLabel.value}「${char.name}」？`, async () => {
    try {
      await characterStore.removeCharacter(projectId.value, char.id)
      ui.showToast(`${characterLabel.value}已删除`, 'success')
    } catch (e: unknown) {
      ui.showToast(friendlyError(e, `删除${characterLabel.value}失败`), 'error')
    }
  }, '删除', true)
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
    newWEError.value = `${worldEntryLabel.value}标题不能为空`
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
    ui.showToast(`${worldLabel.value}创建成功`, 'success')
  } catch (e: unknown) {
    newWEError.value = friendlyError(e, `创建${worldLabel.value}失败`)
    ui.showToast(friendlyError(e, `创建${worldLabel.value}失败`), 'error')
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
    ui.showToast(`${worldLabel.value}已更新`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, `更新${worldLabel.value}失败`), 'error')
  }
}

async function deleteWorldEntryConfirm(entry: WorldEntry) {
  showConfirm(`确定删除${worldEntryLabel.value}「${entry.title}」？`, async () => {
    try {
      await worldEntryStore.removeWorldEntry(projectId.value, entry.id)
      ui.showToast(`${worldLabel.value}已删除`, 'success')
    } catch (e: unknown) {
      ui.showToast(friendlyError(e, `删除${worldLabel.value}失败`), 'error')
    }
  }, '删除', true)
}
</script>

<template>
  <div v-if="!currentProject" class="workspace-loading">
    {{ projectsLoading ? '加载项目中...' : '项目不存在或无权访问' }}
  </div>

  <!-- Desktop layout -->
  <div v-else-if="!isMobile" class="workspace-wrapper">
    <!-- Top bar -->
    <header class="top-bar">
      <div class="top-left">
        <a class="top-link" @click.prevent="handleSwitchProject">{{ switchProjectLabel }}</a>
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
          <button :class="{ active: activeTab === 'units' }" @click="activeTab = 'units'">{{ unitLabel }}</button>
          <button :class="{ active: activeTab === 'outline' }" @click="activeTab = 'outline'">{{ outlineLabel }}</button>
          <button :class="{ active: activeTab === 'characters' }" @click="activeTab = 'characters'">{{ characterLabel }}</button>
          <button :class="{ active: activeTab === 'world' }" @click="activeTab = 'world'">{{ worldLabel }}</button>
        </nav>

        <div class="sidebar-content">
          <!-- Writing units -->
          <div v-if="activeTab === 'units'" class="tab-pane">
            <button class="btn-add-unit" @click="openNewUnitForm">+ 新增{{ unitLabel }}</button>
            <div v-if="showNewUnit" class="unit-form">
              <div class="form-row">
                <label>{{ unitLabel }}标题 <span class="required">*</span></label>
                <input v-model="newTitle" type="text" :placeholder="`输入${unitLabel}标题`" class="form-input" />
              </div>
              <div class="form-row">
                <label>{{ unitSequenceLabel }}</label>
                <input v-model.number="newUnitNum" type="number" min="1" class="form-input form-input-sm" />
              </div>
              <div class="form-row">
                <label>{{ unitSummaryLabel }}</label>
                <textarea v-model="newSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
              </div>
              <div v-if="newUnitError" class="form-error">{{ newUnitError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewUnit">确定</button>
                <button class="btn-cancel" @click="showNewUnit = false">取消</button>
              </div>
            </div>
            <div
              v-for="ch in writingUnits"
              :key="ch.id"
              class="unit-item"
              :class="{ active: unitPosition(ch) === currentUnitNum }"
              @click="selectWritingUnit(unitPosition(ch))"
              @contextmenu.prevent="openUnitMenu($event, ch)"
            >
              <template v-if="editingUnitId === ch.id">
                <span class="unit-num">{{ unitPosition(ch) }}</span>
                <input v-model="editUnitTitle" type="text" class="form-input unit-edit-input" @click.stop @keyup.enter="saveEditUnit(ch)" @keyup.escape="cancelEditUnit" />
                <button class="icon-btn" title="保存" @click.stop="saveEditUnit(ch)">&#10003;</button>
                <button class="icon-btn icon-btn-danger" title="取消" @click.stop="cancelEditUnit">&#10005;</button>
              </template>
              <template v-else>
                <span class="unit-num">{{ unitPosition(ch) }}</span>
                <span class="unit-title">{{ ch.title }}</span>
                <span class="unit-status" :class="ch.status">{{ ch.status === 'final' ? '终稿' : ch.status === 'reviewing' ? '审核' : ch.status === 'revision' ? '修订' : '草稿' }}</span>
              </template>
            </div>
          </div>

          <!-- Outline -->
          <div v-if="activeTab === 'outline'" class="tab-pane">
            <button class="btn-add-unit" @click="openNewOutlineForm">+ {{ outlineAddLabel }}</button>
            <div v-if="showNewOutline" class="unit-form">
              <div class="form-row">
                <label>{{ outlineTitleLabel }} <span class="required">*</span></label>
                <input v-model="newOutlineTitle" type="text" :placeholder="`输入${outlineTitleLabel}`" class="form-input" />
              </div>
              <div class="form-row">
                <label>{{ unitLabel }}序号</label>
                <input v-model.number="newOutlineSeq" type="number" min="1" class="form-input form-input-sm" />
              </div>
              <div class="form-row">
                <label>{{ projectMode === 'article' ? '内容摘要' : '摘要' }}</label>
                <textarea v-model="newOutlineSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
              </div>
              <div class="form-row">
                <label>{{ turningPointLabel }}</label>
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
                <div class="unit-form" style="margin: 0;">
                  <div class="form-row">
                    <label>标题 <span class="required">*</span></label>
                    <input v-model="editOutlineTitle" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>{{ projectMode === 'article' ? '内容摘要' : '摘要' }}</label>
                    <textarea v-model="editOutlineSummary" class="form-textarea" rows="2"></textarea>
                  </div>
                  <div class="form-row">
                    <label>{{ turningPointLabel }}</label>
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
                <span class="section-title">{{ hiddenThreadLabel }}</span>
                <button class="btn-add-inline" @click="openNewHiddenThreadForm">+ 添加{{ hiddenThreadLabel }}</button>
              </div>
              <div v-if="showNewHiddenThread" class="unit-form">
                <div class="form-row">
                  <label>{{ hiddenThreadLabel }}名称 <span class="required">*</span></label>
                  <input v-model="newHTName" type="text" :placeholder="`输入${hiddenThreadLabel}名称`" class="form-input" />
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
              <div v-if="!hiddenThreads.length && !showNewHiddenThread" class="empty-hint" style="padding: var(--sp-4) 0;">暂无{{ hiddenThreadLabel }}</div>
            </div>
          </div>

          <!-- Characters -->
          <div v-if="activeTab === 'characters'" class="tab-pane">
            <button class="btn-add-unit" @click="openNewCharacterForm">+ 新增{{ characterLabel }}</button>
            <div v-if="showNewCharacter" class="unit-form" style="margin: 0;">
              <div class="form-row">
                <label>{{ characterLabel }}名称 <span class="required">*</span></label>
                <input v-model="newCharName" type="text" :placeholder="`输入${characterLabel}名称`" class="form-input" />
              </div>
              <div class="form-row">
                <label>{{ characterLabel }}类型</label>
                <select v-model="newCharRoleType" class="form-input">
                  <option value="protagonist">{{ projectMode === 'article' ? '核心受众' : '主角' }}</option>
                  <option value="antagonist">{{ projectMode === 'article' ? '反对人群' : '反派' }}</option>
                  <option value="supporting">{{ projectMode === 'article' ? '影响者' : '配角' }}</option>
                  <option value="minor">{{ projectMode === 'article' ? '泛受众' : '路人' }}</option>
                </select>
              </div>
              <div class="form-row">
                <label>{{ projectMode === 'article' ? '细分人群' : '阵营' }}</label>
                <input v-model="newCharFaction" type="text" placeholder="可选" class="form-input" />
              </div>
              <div class="form-row">
                <label>简介</label>
                <textarea v-model="newCharProfile" class="form-textarea" rows="3" :placeholder="projectMode === 'article' ? '受众痛点、需求、决策顾虑' : '角色描述'"></textarea>
              </div>
              <div v-if="newCharError" class="form-error">{{ newCharError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewCharacter">确定</button>
                <button class="btn-cancel" @click="showNewCharacter = false">取消</button>
              </div>
            </div>
            <div v-for="char in characters" :key="char.id" class="character-card">
              <template v-if="editingCharId === char.id">
                <div class="unit-form" style="margin: 0;">
                  <div class="form-row">
                    <label>{{ characterLabel }}名称 <span class="required">*</span></label>
                    <input v-model="editCharName" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>{{ characterLabel }}类型</label>
                    <select v-model="editCharRoleType" class="form-input">
                      <option value="protagonist">{{ projectMode === 'article' ? '核心受众' : '主角' }}</option>
                      <option value="antagonist">{{ projectMode === 'article' ? '反对人群' : '反派' }}</option>
                      <option value="supporting">{{ projectMode === 'article' ? '影响者' : '配角' }}</option>
                      <option value="minor">{{ projectMode === 'article' ? '泛受众' : '路人' }}</option>
                    </select>
                  </div>
                  <div class="form-row">
                    <label>{{ projectMode === 'article' ? '细分人群' : '阵营' }}</label>
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
                  <span class="char-role-type" :class="char.role_type">{{ projectMode === 'article' ? { protagonist: '核心受众', antagonist: '反对人群', supporting: '影响者', minor: '泛受众' }[char.role_type] : { protagonist: '主角', antagonist: '反派', supporting: '配角', minor: '路人' }[char.role_type] }}</span>
                  <span class="outline-actions">
                    <button class="icon-btn" title="编辑" @click.stop="startEditCharacter(char)">&#9998;</button>
                    <button class="icon-btn icon-btn-danger" title="删除" @click.stop="deleteCharacterConfirm(char)">&#10005;</button>
                  </span>
                </div>
                <p v-if="char.faction" class="char-faction">{{ projectMode === 'article' ? '细分人群' : '阵营' }}: {{ char.faction }}</p>
                <p class="char-profile">{{ char.profile }}</p>
                <div v-if="projectMode === 'novel' && char.appearance_count" class="char-appearance">出场次数: {{ char.appearance_count }}</div>
              </template>
            </div>
            <div v-if="!characters.length && !showNewCharacter" class="empty-hint">暂无{{ characterLabel }}</div>
          </div>

          <!-- World -->
          <div v-if="activeTab === 'world'" class="tab-pane">
            <button class="btn-add-unit" @click="openNewWorldEntryForm">+ 新增{{ worldEntryLabel }}</button>
            <div v-if="showNewWorldEntry" class="unit-form" style="margin: 0;">
              <div class="form-row">
                <label>{{ worldEntryLabel }}标题 <span class="required">*</span></label>
                <input v-model="newWETitle" type="text" :placeholder="`输入${worldEntryLabel}标题`" class="form-input" />
              </div>
              <div class="form-row">
                <label>分类</label>
                <input v-model="newWECategory" type="text" :placeholder="projectMode === 'article' ? '可选，如：品牌、产品、案例、数据' : '可选，如：地理、历史、魔法'" class="form-input" />
              </div>
              <div class="form-row">
                <label>内容</label>
                <textarea v-model="newWEContent" class="form-textarea" rows="3" :placeholder="projectMode === 'article' ? '品牌/产品资料、案例、数据或参考材料' : '世界观设定内容'"></textarea>
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
                <div class="unit-form" style="margin: 0;">
                  <div class="form-row">
                    <label>{{ worldEntryLabel }}标题 <span class="required">*</span></label>
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
            <div v-if="!worldEntries.length && !showNewWorldEntry" class="empty-hint">暂无{{ worldLabel }}</div>
          </div>
        </div>
        </div>
      </aside>

      <!-- Center: editor -->
      <main class="editor-area">
        <WritingEditor v-if="currentWritingUnit" :project-id="projectId" :mode="projectMode" />
        <div v-else class="empty-hint" style="padding-top: 120px;">选择一个{{ unitLabel }}开始写作</div>
      </main>

      <!-- Right: agent panel (always in DOM for grid integrity) -->
      <aside class="agent-area" :class="{ 'agent-hidden': !agentOpen }">
        <button class="agent-toggle" @click="agentOpen = !agentOpen" :title="agentOpen ? '收起面板' : '展开 Agent 面板'">
          {{ agentOpen ? '›' : '‹' }}
        </button>
        <div class="agent-content">
          <AgentPanel ref="agentPanelRef" :project-id="projectId" :mode="projectMode" />
        </div>
      </aside>
    </div>
  </div>

  <!-- Mobile layout -->
  <div v-else class="workspace-mobile">
    <!-- Mobile top bar -->
    <header class="mobile-top-bar">
      <a class="top-link" @click.prevent="handleSwitchProject">{{ switchProjectLabel }}</a>
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
      <button :class="{ active: mobilePanel === 'units' }" @click="mobilePanel = 'units'">{{ unitLabel }}</button>
      <button :class="{ active: mobilePanel === 'characters' }" @click="mobilePanel = 'characters'">{{ characterLabel }}</button>
      <button :class="{ active: mobilePanel === 'world' }" @click="mobilePanel = 'world'">{{ worldLabel }}</button>
      <button :class="{ active: mobilePanel === 'editor' }" @click="mobilePanel = 'editor'">编辑</button>
      <button :class="{ active: mobilePanel === 'agent' }" @click="mobilePanel = 'agent'">Agent</button>
    </nav>

    <div v-if="mobilePanel === 'units'" class="mobile-pane">
      <button class="btn-add-unit" @click="openNewUnitForm">+ 新增{{ unitLabel }}</button>
      <div v-if="showNewUnit" class="unit-form">
        <div class="form-row">
          <label>{{ unitLabel }}标题 <span class="required">*</span></label>
          <input v-model="newTitle" type="text" :placeholder="`输入${unitLabel}标题`" class="form-input" />
        </div>
        <div class="form-row">
          <label>{{ unitSequenceLabel }}</label>
          <input v-model.number="newUnitNum" type="number" min="1" class="form-input form-input-sm" />
        </div>
        <div class="form-row">
          <label>{{ unitSummaryLabel }}</label>
          <textarea v-model="newSummary" placeholder="可选" class="form-textarea" rows="2"></textarea>
        </div>
        <div v-if="newUnitError" class="form-error">{{ newUnitError }}</div>
        <div class="form-actions">
          <button class="btn-submit" @click="submitNewUnit">确定</button>
          <button class="btn-cancel" @click="showNewUnit = false">取消</button>
        </div>
      </div>
      <div
        v-for="ch in writingUnits"
        :key="ch.id"
        class="unit-item"
        :class="{ active: unitPosition(ch) === currentUnitNum }"
        @click="selectWritingUnit(unitPosition(ch))"
        @contextmenu.prevent="openUnitMenu($event, ch)"
      >
        <template v-if="editingUnitId === ch.id">
          <span class="unit-num">{{ unitPosition(ch) }}</span>
          <input v-model="editUnitTitle" type="text" class="form-input unit-edit-input" @click.stop @keyup.enter="saveEditUnit(ch)" @keyup.escape="cancelEditUnit" />
          <button class="icon-btn" title="保存" @click.stop="saveEditUnit(ch)">&#10003;</button>
          <button class="icon-btn icon-btn-danger" title="取消" @click.stop="cancelEditUnit">&#10005;</button>
        </template>
        <template v-else>
          <span class="unit-num">{{ unitPosition(ch) }}</span>
          <span class="unit-title">{{ ch.title }}</span>
          <span class="unit-status" :class="ch.status">{{ ch.status === 'final' ? '终稿' : '草稿' }}</span>
        </template>
      </div>
    </div>

    <div v-if="mobilePanel === 'editor'" class="mobile-pane mobile-editor">
      <WritingEditor v-if="currentWritingUnit" :project-id="projectId" :mode="projectMode" />
      <div v-else class="empty-hint">选择一个{{ unitLabel }}开始写作</div>
    </div>

    <div v-if="mobilePanel === 'agent'" class="mobile-pane">
      <AgentPanel ref="agentPanelRef" :project-id="projectId" :mode="projectMode" />
    </div>
  </div>

  <!-- Writing unit context menu -->
  <Teleport to="body">
    <div v-if="unitMenu" class="ctx-overlay" @click="closeUnitMenu" @contextmenu.prevent="closeUnitMenu"></div>
    <div v-if="unitMenu" class="ctx-menu" :style="{ left: unitMenu.x + 'px', top: unitMenu.y + 'px' }">
      <button class="ctx-item" @click="menuEditUnit">&#9998; 编辑标题</button>
      <button class="ctx-item ctx-item-danger" @click="menuDeleteUnit">&#10005; 删除{{ unitLabel }}</button>
    </div>
  </Teleport>

  <ConfirmModal
    v-if="confirmState"
    :message="confirmState.message"
    :confirm-text="confirmState.confirmText"
    :danger="confirmState.danger"
    @confirm="handleConfirmOk"
    @cancel="handleConfirmCancel"
  />
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
  height: 100dvh;
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

/* Writing unit items */
.unit-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  cursor: pointer;
  font-size: var(--text-sm);
  transition: background var(--transition);
}
.unit-item:hover { background: var(--bg-hover); }
.unit-item.active {
  background: var(--accent-subtle);
  color: var(--accent);
  font-weight: 600;
}
.unit-num {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  min-width: 18px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.unit-item.active .unit-num { color: var(--accent); }
.unit-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unit-edit-input {
  flex: 1;
  padding: 2px 6px;
  font-size: var(--text-sm);
  height: 26px;
}
.unit-status {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  white-space: nowrap;
}
.unit-status.draft { background: var(--status-draft-bg); color: var(--status-draft); }
.unit-status.reviewing { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.unit-status.revision { background: var(--status-revision-bg); color: var(--status-revision); }
.unit-status.final { background: var(--status-final-bg); color: var(--status-final); }

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

/* Reusable sidebar form */
.btn-add-unit {
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
.btn-add-unit:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.unit-form {
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
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-panel);
  min-width: 0;
  min-height: 0;
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
  height: 100dvh;
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
  min-height: 0;
}
.mobile-editor {
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
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

/* Context menu */
.ctx-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
}
.ctx-menu {
  position: fixed;
  z-index: 2001;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  padding: var(--sp-1) 0;
  min-width: 140px;
}
.ctx-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border: none;
  background: none;
  font-size: var(--text-sm);
  color: var(--text);
  cursor: pointer;
  text-align: left;
  transition: background var(--transition);
}
.ctx-item:hover { background: var(--bg-hover); }
.ctx-item-danger { color: var(--status-error); }
.ctx-item-danger:hover { background: var(--sev-critical-bg); }
</style>
