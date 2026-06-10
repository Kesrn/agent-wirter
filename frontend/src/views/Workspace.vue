<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave } from 'vue-router'
import { useProjectStore, useChapterStore, useDocumentStore, useRelationStore, useWorldEntryStore, useCharacterStore, useOutlineStore, useHiddenThreadStore, useExpertStore, useUiStore, friendlyError } from '../stores'
import { API_BASE_URL, type OutlineItem, type HiddenThread, type Character, type WorldEntry, type WritingUnit, type StructureExtractPayload } from '../api/types'
import { api } from '../api/client'
import WritingEditor from '../components/WritingEditor.vue'
import AgentPanel from '../components/AgentPanel.vue'
import ConfirmModal from '../components/ConfirmModal.vue'
import StructureExtractPreview from '../components/StructureExtractPreview.vue'
import BaseSelect from '../components/BaseSelect.vue'
import { displayChapterNumber, stripChapterNumber } from '../utils/chapterTitle'
import { clearAuthSession, getAuthToken } from '../utils/authSession'

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
const searchPlaceholder = computed(() => projectMode.value === 'article' ? '搜索稿件正文、受众、资料' : '搜索章节正文、角色、设定')
const characterRoleOptions = computed(() => [
  { value: 'protagonist', label: projectMode.value === 'article' ? '核心受众' : '主角' },
  { value: 'antagonist', label: projectMode.value === 'article' ? '反对人群' : '反派' },
  { value: 'supporting', label: projectMode.value === 'article' ? '影响者' : '配角' },
  { value: 'minor', label: projectMode.value === 'article' ? '泛受众' : '路人' },
])
const confidenceOptions = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
]
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

type SearchTab = 'units' | 'outline' | 'characters' | 'world'
type SearchResult = {
  id: string
  targetId: string
  label: string
  detail: string
  badge: string
  tab: SearchTab
  unitNum?: number
}

const searchQuery = ref('')
const searchOpen = ref(false)
const highlightedSearchTarget = ref<string | null>(null)
let searchHighlightTimer: ReturnType<typeof setTimeout> | null = null

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

function unitDisplayTitle(unit: WritingUnit): string {
  if (projectMode.value === 'article') return unit.title
  return stripChapterNumber(unit.title) || unit.title
}

function unitDisplayNum(unit: WritingUnit): number {
  const position = unitPosition(unit)
  if (projectMode.value === 'article') return position
  return displayChapterNumber(position, unit.title)
}

function selectWritingUnit(num: number) {
  if (projectMode.value === 'article') {
    documentStore.setCurrentDocument(num)
  } else {
    chapterStore.setCurrentChapter(num)
  }
  mobilePanel.value = 'editor'
}

function normalizedSearchText(text: string | null | undefined): string {
  return (text ?? '').trim().toLocaleLowerCase()
}

function matchesSearch(text: string | null | undefined, query: string): boolean {
  return normalizedSearchText(text).includes(query)
}

function compactSearchText(text: string | null | undefined): string {
  return (text ?? '').replace(/\s+/g, ' ').trim()
}

function searchSnippet(text: string | null | undefined, query: string): string | null {
  const source = compactSearchText(text)
  if (!source) return null

  const index = source.toLocaleLowerCase().indexOf(query)
  if (index === -1) return null

  const radius = 34
  const start = Math.max(0, index - radius)
  const end = Math.min(source.length, index + query.length + radius)
  const prefix = start > 0 ? '...' : ''
  const suffix = end < source.length ? '...' : ''
  return `${prefix}${source.slice(start, end)}${suffix}`
}

function unitOrdinalText(unit: WritingUnit): string {
  return unitOrdinalNumberText(unitDisplayNum(unit))
}

function unitOrdinalNumberText(num: number): string {
  return projectMode.value === 'article' ? `${unitLabel.value} ${num}` : `第 ${num} 章`
}

const projectSearchResults = computed<SearchResult[]>(() => {
  const query = normalizedSearchText(searchQuery.value)
  if (!query) return []

  const results: SearchResult[] = []
  for (const unit of writingUnits.value) {
    const title = unitDisplayTitle(unit)
    const titleMatched = matchesSearch(title, query) || matchesSearch(unit.title, query)
    const summarySnippet = 'summary' in unit ? searchSnippet(unit.summary, query) : null
    const draftSnippet = searchSnippet(unit.draft, query) ?? searchSnippet(unit.final_text, query)
    if (titleMatched || summarySnippet || draftSnippet) {
      results.push({
        id: `unit-${unit.id}`,
        targetId: `unit-${unit.id}`,
        label: title,
        detail: titleMatched ? unitOrdinalText(unit) : `${unitOrdinalText(unit)} · ${summarySnippet ?? draftSnippet}`,
        badge: titleMatched ? unitLabel.value : summarySnippet ? '摘要' : '正文',
        tab: 'units',
        unitNum: unitPosition(unit),
      })
    }
  }

  for (const item of outline.value) {
    const titleMatched = matchesSearch(item.title, query)
    const summarySnippet = searchSnippet(item.summary, query)
    const turningPointSnippet = searchSnippet(item.turning_point, query)
    if (titleMatched || summarySnippet || turningPointSnippet) {
      results.push({
        id: `outline-${item.id}`,
        targetId: `outline-${item.id}`,
        label: item.title,
        detail: titleMatched ? `${unitOrdinalNumberText(item.chapter_num)} · ${outlineLabel.value}` : `${outlineLabel.value} · ${summarySnippet ?? turningPointSnippet}`,
        badge: outlineLabel.value,
        tab: 'outline',
      })
    }
  }

  for (const char of characters.value) {
    const nameMatched = matchesSearch(char.name, query)
    const factionSnippet = searchSnippet(char.faction, query)
    const profileSnippet = searchSnippet(char.profile, query)
    if (nameMatched || factionSnippet || profileSnippet) {
      results.push({
        id: `character-${char.id}`,
        targetId: `character-${char.id}`,
        label: char.name,
        detail: nameMatched ? (char.faction || `${characterLabel.value}档案`) : `${characterLabel.value} · ${factionSnippet ?? profileSnippet}`,
        badge: characterLabel.value,
        tab: 'characters',
      })
    }
  }

  for (const entry of worldEntries.value) {
    const titleMatched = matchesSearch(entry.title, query)
    const categorySnippet = searchSnippet(entry.category, query)
    const contentSnippet = searchSnippet(entry.content, query)
    if (titleMatched || categorySnippet || contentSnippet) {
      results.push({
        id: `world-${entry.id}`,
        targetId: `world-${entry.id}`,
        label: entry.title,
        detail: titleMatched ? (entry.category || `${worldLabel.value}${worldEntryLabel.value}`) : `${worldLabel.value} · ${categorySnippet ?? contentSnippet}`,
        badge: worldEntryLabel.value,
        tab: 'world',
      })
    }
  }

  for (const thread of hiddenThreads.value) {
    const nameMatched = matchesSearch(thread.name, query)
    const descriptionSnippet = searchSnippet(thread.description, query)
    if (nameMatched || descriptionSnippet) {
      results.push({
        id: `hidden-${thread.id}`,
        targetId: `hidden-${thread.id}`,
        label: thread.name,
        detail: nameMatched ? hiddenThreadLabel.value : `${hiddenThreadLabel.value} · ${descriptionSnippet}`,
        badge: hiddenThreadLabel.value,
        tab: 'outline',
      })
    }
  }

  return results.slice(0, 20)
})

function closeSearchSoon() {
  window.setTimeout(() => { searchOpen.value = false }, 120)
}

function clearSearch() {
  searchQuery.value = ''
  searchOpen.value = false
}

function highlightSearchTarget(targetId: string) {
  highlightedSearchTarget.value = targetId
  if (searchHighlightTimer) clearTimeout(searchHighlightTimer)
  searchHighlightTimer = setTimeout(() => {
    highlightedSearchTarget.value = null
    searchHighlightTimer = null
  }, 1800)
}

function scrollSearchTarget(targetId: string) {
  const target = document.querySelector(`[data-search-target="${targetId}"]`) as HTMLElement | null
  target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

function openSearchResult(result: SearchResult) {
  sidebarOpen.value = true
  activeTab.value = result.tab
  if (result.tab === 'characters') mobilePanel.value = 'characters'
  else if (result.tab === 'world') mobilePanel.value = 'world'
  else if (result.tab === 'outline') mobilePanel.value = 'units'

  if (typeof result.unitNum === 'number') {
    selectWritingUnit(result.unitNum)
  }

  searchOpen.value = false
  nextTick(() => {
    scrollSearchTarget(result.targetId)
    highlightSearchTarget(result.targetId)
  })
}

function openFirstSearchResult() {
  const first = projectSearchResults.value[0]
  if (first) openSearchResult(first)
}

const isMobile = ref(window.innerWidth < 900)
function onResize() { isMobile.value = window.innerWidth < 900 }
onMounted(() => {
  window.addEventListener('resize', onResize)
})
onUnmounted(() => window.removeEventListener('resize', onResize))
onUnmounted(() => {
  if (searchHighlightTimer) clearTimeout(searchHighlightTimer)
})

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
    clearAuthSession()
    router.push('/login')
  })
}

// Export
const showExportMenu = ref(false)
const exporting = ref(false)

function getAuthHeaders(): Record<string, string> {
  const token = getAuthToken()
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

// Structure extraction
const extractingStructure = ref(false)
const applyingStructure = ref(false)
const showStructurePreview = ref(false)
const structurePayload = ref<StructureExtractPayload | null>(null)

const canExtractStructure = computed(() => {
  const unit = currentWritingUnit.value
  return projectMode.value === 'novel' && !!unit && !!unit.draft?.trim() && !extractingStructure.value && !applyingStructure.value
})

async function extractCurrentChapterStructure() {
  const unit = currentWritingUnit.value
  if (projectMode.value !== 'novel' || !unit) {
    ui.showToast('请先选择一个小说章节', 'error')
    return
  }
  if (!unit.draft.trim()) {
    ui.showToast('当前章节正文为空，无法提炼', 'error')
    return
  }

  extractingStructure.value = true
  try {
    const result = await api.extractChapterStructure(projectId.value, unitPosition(unit), {
      mode: 'preview',
      targets: ['outlines', 'characters', 'world_entries', 'hidden_threads', 'character_relations'],
      include_existing_context: true,
    })
    structurePayload.value = result.extraction
    showStructurePreview.value = true
  } catch (e: unknown) {
    ui.showToast(structureExtractionError(e), 'error')
  } finally {
    extractingStructure.value = false
  }
}

function structureExtractionError(e: unknown) {
  const message = friendlyError(e, '结构提炼失败')
  if (message.includes('API Key') || message.includes('模型配置')) {
    return '结构提炼失败：模型配置不可用，请到设置里重新保存 API Key 后再试'
  }
  if (message.includes('模型服务') || message.includes('服务器异常') || message.includes('Internal Server Error')) {
    return '结构提炼失败：模型服务暂时不可用，请检查模型配置或稍后重试'
  }
  return `结构提炼失败：${message}`
}

async function applyExtractedStructure(payload: StructureExtractPayload) {
  const unit = currentWritingUnit.value
  if (!unit) return

  applyingStructure.value = true
  try {
    const result = await api.extractChapterStructure(projectId.value, unitPosition(unit), {
      mode: 'apply',
      extraction: payload,
    })
    await Promise.all([
      outlineStore.loadOutlines(projectId.value),
      characterStore.loadCharacters(projectId.value),
      worldEntryStore.loadWorldEntries(projectId.value),
      hiddenThreadStore.loadHiddenThreads(projectId.value),
      relationStore.loadRelations(projectId.value),
    ])
    showStructurePreview.value = false
    structurePayload.value = null
    const count = Object.values(result.applied?.counts ?? {}).reduce((sum, value) => sum + value, 0)
    ui.showToast(`已写入 ${count} 项结构资料`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '写入结构资料失败'), 'error')
  } finally {
    applyingStructure.value = false
  }
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
      <div class="project-search">
        <span class="search-prefix">搜</span>
        <input
          v-model="searchQuery"
          class="project-search-input"
          type="text"
          :placeholder="searchPlaceholder"
          @focus="searchOpen = true"
          @blur="closeSearchSoon"
          @keydown.enter.prevent="openFirstSearchResult"
          @keydown.esc="searchOpen = false"
        />
        <button
          v-if="searchQuery"
          class="search-clear"
          type="button"
          aria-label="清空搜索"
          @mousedown.prevent="clearSearch"
        >
          清空
        </button>
        <div v-if="searchOpen && searchQuery.trim()" class="search-panel">
          <button
            v-for="result in projectSearchResults"
            :key="result.id"
            class="search-result"
            type="button"
            @mousedown.prevent="openSearchResult(result)"
          >
            <span class="search-badge">{{ result.badge }}</span>
            <span class="search-result-main">
              <span class="search-result-title">{{ result.label }}</span>
              <span class="search-result-detail">{{ result.detail }}</span>
            </span>
          </button>
          <div v-if="!projectSearchResults.length" class="search-empty">没有匹配的内容</div>
        </div>
      </div>
      <div class="top-right">
        <router-link class="top-btn top-btn-link" :to="`/projects/${projectId}/evaluations`">评测集</router-link>
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
            <div class="unit-toolbar">
              <button class="btn-add-unit" @click="openNewUnitForm">+ 新增{{ unitLabel }}</button>
              <button
                v-if="projectMode === 'novel'"
                class="btn-extract-structure"
                :disabled="!canExtractStructure"
                @click="extractCurrentChapterStructure"
              >
                {{ extractingStructure ? '提炼中...' : '提炼结构' }}
              </button>
            </div>
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
              :class="{ active: unitPosition(ch) === currentUnitNum, 'search-highlight': highlightedSearchTarget === `unit-${ch.id}` }"
              :data-search-target="`unit-${ch.id}`"
              @click="selectWritingUnit(unitPosition(ch))"
              @contextmenu.prevent="openUnitMenu($event, ch)"
            >
              <template v-if="editingUnitId === ch.id">
                <span class="unit-num">{{ unitDisplayNum(ch) }}</span>
                <input v-model="editUnitTitle" type="text" class="form-input unit-edit-input" @click.stop @keyup.enter="saveEditUnit(ch)" @keyup.escape="cancelEditUnit" />
                <button class="icon-btn" title="保存" @click.stop="saveEditUnit(ch)">&#10003;</button>
                <button class="icon-btn icon-btn-danger" title="取消" @click.stop="cancelEditUnit">&#10005;</button>
              </template>
              <template v-else>
                <span class="unit-num">{{ unitDisplayNum(ch) }}</span>
                <span class="unit-title">{{ unitDisplayTitle(ch) }}</span>
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
            <div
              v-for="item in outline"
              :key="item.chapter_num"
              class="outline-item"
              :class="{ 'search-highlight': highlightedSearchTarget === `outline-${item.id}` }"
              :data-search-target="`outline-${item.id}`"
            >
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
              <div
                v-for="ht in hiddenThreads"
                :key="ht.id"
                class="ht-item"
                :class="{ 'search-highlight': highlightedSearchTarget === `hidden-${ht.id}` }"
                :data-search-target="`hidden-${ht.id}`"
              >
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
                <BaseSelect v-model="newCharRoleType" :options="characterRoleOptions" />
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
            <div
              v-for="char in characters"
              :key="char.id"
              class="character-card"
              :class="{ 'search-highlight': highlightedSearchTarget === `character-${char.id}` }"
              :data-search-target="`character-${char.id}`"
            >
              <template v-if="editingCharId === char.id">
                <div class="unit-form" style="margin: 0;">
                  <div class="form-row">
                    <label>{{ characterLabel }}名称 <span class="required">*</span></label>
                    <input v-model="editCharName" type="text" class="form-input" />
                  </div>
                  <div class="form-row">
                    <label>{{ characterLabel }}类型</label>
                    <BaseSelect v-model="editCharRoleType" :options="characterRoleOptions" />
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
                <BaseSelect v-model="newWEConfidence" :options="confidenceOptions" />
              </div>
              <div v-if="newWEError" class="form-error">{{ newWEError }}</div>
              <div class="form-actions">
                <button class="btn-submit" @click="submitNewWorldEntry">确定</button>
                <button class="btn-cancel" @click="showNewWorldEntry = false">取消</button>
              </div>
            </div>
            <div
              v-for="entry in worldEntries"
              :key="entry.id"
              class="world-block"
              :class="{ 'search-highlight': highlightedSearchTarget === `world-${entry.id}` }"
              :data-search-target="`world-${entry.id}`"
            >
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
                    <BaseSelect v-model="editWEConfidence" :options="confidenceOptions" />
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
      <div class="unit-toolbar">
        <button class="btn-add-unit" @click="openNewUnitForm">+ 新增{{ unitLabel }}</button>
        <button
          v-if="projectMode === 'novel'"
          class="btn-extract-structure"
          :disabled="!canExtractStructure"
          @click="extractCurrentChapterStructure"
        >
          {{ extractingStructure ? '提炼中...' : '提炼结构' }}
        </button>
      </div>
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
        :class="{ active: unitPosition(ch) === currentUnitNum, 'search-highlight': highlightedSearchTarget === `unit-${ch.id}` }"
        :data-search-target="`unit-${ch.id}`"
        @click="selectWritingUnit(unitPosition(ch))"
        @contextmenu.prevent="openUnitMenu($event, ch)"
      >
        <template v-if="editingUnitId === ch.id">
          <span class="unit-num">{{ unitDisplayNum(ch) }}</span>
          <input v-model="editUnitTitle" type="text" class="form-input unit-edit-input" @click.stop @keyup.enter="saveEditUnit(ch)" @keyup.escape="cancelEditUnit" />
          <button class="icon-btn" title="保存" @click.stop="saveEditUnit(ch)">&#10003;</button>
          <button class="icon-btn icon-btn-danger" title="取消" @click.stop="cancelEditUnit">&#10005;</button>
        </template>
        <template v-else>
          <span class="unit-num">{{ unitDisplayNum(ch) }}</span>
          <span class="unit-title">{{ unitDisplayTitle(ch) }}</span>
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
  <StructureExtractPreview
    :show="showStructurePreview"
    :payload="structurePayload"
    :confirm-text="applyingStructure ? '写入中...' : '确认写入'"
    @close="showStructurePreview = false"
    @confirm="applyExtractedStructure"
  />
</template>

<style scoped>
/* ─── Top bar ─── */
.top-bar, .mobile-top-bar {
  position: relative;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  min-height: 66px;
  padding: 12px 24px;
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--bg-panel) 88%, transparent), color-mix(in srgb, var(--bg) 88%, transparent));
  border-bottom: 1px solid color-mix(in srgb, var(--border) 78%, transparent);
  flex-shrink: 0;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.025) inset;
  backdrop-filter: blur(18px);
}
.top-left, .top-right {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.top-left {
  min-width: 0;
  flex: 1 1 300px;
}
.top-right {
  flex: 0 0 auto;
}
.top-link {
  font-size: var(--text-sm);
  color: var(--accent);
  font-weight: 720;
  white-space: nowrap;
}
.top-link:hover { text-decoration: none; opacity: 0.85; }
.top-sep { color: var(--text-tertiary); font-size: var(--text-xs); }
.top-project-name {
  font-size: 0.98rem;
  font-weight: 780;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mode-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 4px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.mode-badge.novel { background: var(--status-final-bg); color: var(--status-final); }
.mode-badge.article { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.top-btn {
  height: 38px;
  padding: 0 14px;
  background: color-mix(in srgb, var(--bg-panel) 72%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 10px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.035) inset;
  transition: background var(--transition), border-color var(--transition), color var(--transition), transform var(--transition);
}
.top-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--text);
  transform: translateY(-1px);
}
.top-btn-logout { color: var(--text-tertiary); }
.top-btn-link {
  text-decoration: none;
  display: inline-flex;
  align-items: center;
}
.export-group { position: relative; }
.export-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: var(--sp-1);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  z-index: 1201;
  min-width: 140px;
  overflow: hidden;
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

.project-search {
  position: relative;
  flex: 0 1 620px;
  min-width: 300px;
  z-index: 1300;
}
.search-prefix {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-weight: 700;
  pointer-events: none;
}
.project-search-input {
  width: 100%;
  height: 42px;
  border: 1px solid color-mix(in srgb, var(--border) 88%, transparent);
  border-radius: 14px;
  background: color-mix(in srgb, var(--bg-input) 86%, transparent);
  color: var(--text);
  font-size: var(--text-sm);
  padding: 0 64px 0 36px;
  outline: none;
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, 0 12px 28px rgba(0,0,0,0.12);
  transition: border-color var(--transition), box-shadow var(--transition), background var(--transition);
}
.project-search-input:focus {
  border-color: var(--border-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  background: var(--bg-panel);
}
.project-search-input::placeholder {
  color: var(--text-tertiary);
}
.search-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  height: 24px;
  padding: 0 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  cursor: pointer;
}
.search-clear:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.search-panel {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% + 8px);
  max-height: 330px;
  overflow-y: auto;
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  background: color-mix(in srgb, var(--bg-panel) 96%, transparent);
  box-shadow: var(--shadow-lg);
}
.search-result {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: 9px var(--sp-2);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}
.search-result:hover {
  background: var(--bg-hover);
}
.search-badge {
  flex: 0 0 auto;
  min-width: 42px;
  padding: 3px 7px;
  border-radius: 8px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  text-align: center;
}
.search-result-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.search-result-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  font-weight: 650;
}
.search-result-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
.search-empty {
  padding: var(--sp-4) var(--sp-3);
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  text-align: center;
}

.mobile-top-bar {
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  flex-wrap: wrap;
}

/* ─── Desktop ─── */
.workspace-wrapper {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--desktop-status-bar-height, 0px));
  height: calc(100dvh - var(--desktop-status-bar-height, 0px));
  background:
    radial-gradient(circle at 54% 0, color-mix(in srgb, var(--accent) 10%, transparent), transparent 34%),
    linear-gradient(180deg, color-mix(in srgb, var(--bg-panel) 26%, var(--bg)) 0%, var(--bg) 240px),
    var(--bg);
}
.workspace {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 360px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  transition: grid-template-columns 220ms ease;
}
.workspace.sidebar-collapsed { grid-template-columns: 0px minmax(0, 1fr) 360px; }
.workspace.agent-collapsed { grid-template-columns: 280px minmax(0, 1fr) 0px; }
.workspace.sidebar-collapsed.agent-collapsed { grid-template-columns: 0px 1fr 0px; }

/* Sidebar & agent toggle buttons */
.sidebar-toggle, .agent-toggle {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  z-index: 10;
  background: color-mix(in srgb, var(--bg-panel) 90%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 999px;
  width: 28px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), opacity 220ms ease;
  padding: 0;
  box-shadow: var(--shadow);
  backdrop-filter: blur(12px);
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
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--bg-panel) 20%, var(--bg-sidebar)), var(--bg-sidebar));
  border-right: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
  display: flex;
  flex-direction: column;
  overflow: visible;
  position: relative;
  min-width: 0;
  min-height: 0;
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
  overflow: hidden;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.sidebar.sidebar-hidden .sidebar-inner {
  visibility: hidden;
}
.sidebar-tabs {
  display: flex;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 76%, transparent);
  flex-shrink: 0;
  gap: 4px;
  padding: 12px;
}
.sidebar-tabs button {
  flex: 1;
  min-height: 36px;
  padding: 0 var(--sp-1);
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  font-size: var(--text-xs);
  font-weight: 650;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}
.sidebar-tabs button:hover { color: var(--text-secondary); }
.sidebar-tabs button.active {
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  border-color: color-mix(in srgb, var(--border) 86%, transparent);
  color: var(--text);
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, var(--shadow-sm);
}
.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: 14px;
  min-width: 0;
  min-height: 0;
  overscroll-behavior: contain;
}
.tab-pane {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-height: min-content;
}

/* Writing unit items */
.unit-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-height: 48px;
  padding: 9px 12px;
  border-radius: 12px;
  cursor: pointer;
  font-size: var(--text-sm);
  border: 1px solid transparent;
  transition: background var(--transition), border-color var(--transition), box-shadow var(--transition);
}
.unit-item:hover {
  background: color-mix(in srgb, var(--bg-hover) 80%, transparent);
  border-color: color-mix(in srgb, var(--border) 76%, transparent);
}
.unit-item.active {
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-panel));
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 12%, transparent), var(--shadow-sm);
  color: var(--accent);
  font-weight: 600;
}
.unit-item.search-highlight,
.outline-item.search-highlight,
.character-card.search-highlight,
.world-block.search-highlight,
.ht-item.search-highlight {
  border-color: color-mix(in srgb, var(--accent) 58%, var(--border));
  background: color-mix(in srgb, var(--accent-subtle) 52%, var(--bg-panel));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent), var(--shadow-sm);
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
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 8px;
  white-space: nowrap;
}
.unit-status.draft { background: var(--status-draft-bg); color: var(--status-draft); }
.unit-status.reviewing { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.unit-status.revision { background: var(--status-revision-bg); color: var(--status-revision); }
.unit-status.final { background: var(--status-final-bg); color: var(--status-final); }

/* Outline */
.outline-item {
  padding: var(--sp-3);
  border-radius: 14px;
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  background: color-mix(in srgb, var(--bg-panel) 86%, transparent);
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, var(--shadow-sm);
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
  background: color-mix(in srgb, var(--accent-subtle) 72%, var(--bg-panel));
  color: var(--accent);
  padding: 2px 7px;
  border-radius: 8px;
}

/* Characters */
.character-card {
  padding: var(--sp-3);
  background: color-mix(in srgb, var(--bg-panel) 86%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 14px;
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, var(--shadow-sm);
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
  background: color-mix(in srgb, var(--bg-panel) 86%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 14px;
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, var(--shadow-sm);
}

.empty-hint {
  text-align: center;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  padding: var(--sp-8) var(--sp-4);
}

/* Reusable sidebar form */
.unit-toolbar {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--sp-2);
}
.btn-add-unit {
  width: 100%;
  min-height: 44px;
  padding: 10px var(--sp-3);
  background: color-mix(in srgb, var(--bg-panel) 54%, transparent);
  border: 1px dashed color-mix(in srgb, var(--accent) 34%, var(--border));
  border-radius: 14px;
  font-size: var(--text-sm);
  font-weight: 650;
  color: var(--accent);
  cursor: pointer;
  transition: border-color var(--transition), color var(--transition);
}
.btn-add-unit:hover {
  border-color: var(--accent);
  background: var(--accent-subtle);
}
.btn-extract-structure {
  width: 100%;
  padding: 9px var(--sp-3);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 14px;
  color: var(--text-inverse);
  font-size: var(--text-sm);
  font-weight: 650;
  transition: background var(--transition), border-color var(--transition), opacity var(--transition);
}
.btn-extract-structure:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.btn-extract-structure:disabled {
  cursor: not-allowed;
  opacity: 0.48;
}
.unit-form {
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 14px;
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  box-shadow: var(--shadow-sm);
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
  background: var(--bg-input);
  color: var(--text);
  transition: border-color var(--transition), box-shadow var(--transition), background var(--transition);
}
.form-input:focus, .form-textarea:focus {
  border-color: var(--border-focus);
  outline: none;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
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
  font-weight: 650;
  cursor: pointer;
  transition: background var(--transition);
}
.btn-submit:hover { background: var(--accent-hover); }
.btn-cancel {
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-panel);
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
  background:
    radial-gradient(circle at 50% 0, color-mix(in srgb, var(--accent) 8%, transparent), transparent 42%),
    var(--paper-stage);
  min-width: 0;
  min-height: 0;
}

/* Agent area */
.agent-area {
  border-left: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--bg-panel) 18%, var(--bg-sidebar)), var(--bg-sidebar));
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
  padding: 20px;
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
  height: calc(100vh - var(--desktop-status-bar-height, 0px));
  height: calc(100dvh - var(--desktop-status-bar-height, 0px));
  background: var(--bg);
}
.mobile-nav {
  display: flex;
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
  flex-shrink: 0;
  gap: 2px;
  padding: var(--sp-2);
}
.mobile-nav button {
  flex: 1;
  padding: var(--sp-2);
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  font-size: var(--text-sm);
  font-weight: 650;
  color: var(--text-tertiary);
  cursor: pointer;
}
.mobile-nav button.active {
  background: var(--accent-subtle);
  color: var(--accent);
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
  border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
  background: var(--bg-panel);
}
.ht-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ht-name {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--accent);
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
