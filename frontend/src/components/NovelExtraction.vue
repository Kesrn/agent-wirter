<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { api, type ExtractionFailure, type CharacterAppearanceItem } from '../api/client'
import { friendlyError, useUiStore } from '../stores'

const props = defineProps<{
  projectId: string
  sourceId: string
  sourceType?: string
  initialGenre?: 'magic_fantasy' | 'historical' | string
}>()

const ui = useUiStore()

// ── 抽取状态 ──
interface ExtractionStatus {
  job_id: string | null
  status: string
  total_chapters: number
  extracted_count: number
  validated_count: number
  merged_count: number
  failed_count: number
  pending_count?: number
  error_message?: string | null
  provider?: string | null
  is_mock?: boolean
  last_run_outcome?: string  // none | success | partial | failed
  current_chapter_no?: number | null
  last_error?: string | null
  paused_at?: string | null
  cancelled_at?: string | null
  last_run_started_at?: string | null
  last_run_finished_at?: string | null
}
const status = ref<ExtractionStatus | null>(null)
const statusLoading = ref(false)
const actionLoading = ref('')
const splitResult = ref<{ chapter_count: number; split_type: string } | null>(null)
const selectedGenre = ref<'magic_fantasy' | 'historical'>(
  props.initialGenre === 'historical' ? 'historical' : 'magic_fantasy'
)

// ── 结构化结果 ──
type StructTable = 'character_profile' | 'ability_profile' | 'event_timeline' | 'world_rule'
const activeTab = ref<StructTable>('character_profile')
const structData = ref<Record<StructTable, any[]>>({
  character_profile: [],
  ability_profile: [],
  event_timeline: [],
  world_rule: [],
})
const structTotals = ref<Record<StructTable, number>>({
  character_profile: 0,
  ability_profile: 0,
  event_timeline: 0,
  world_rule: 0,
})
const structLoading = ref(false)

// ── 结构化 QA ──
const qaQuestion = ref('')
const qaAnswer = ref('')
const qaCitations = ref<any[]>([])
const qaLoading = ref(false)
const qaQueryPlan = ref<any>(null)

const progressPercent = computed(() => {
  if (!status.value || !status.value.total_chapters) return 0
  return Math.round((status.value.extracted_count / status.value.total_chapters) * 100)
})

const isRunning = computed(() => status.value?.status === 'RUNNING' || status.value?.status === 'PENDING')
// 是否还有未处理章节（用于显示"待继续"提示）
const hasMoreChapters = computed(() => {
  if (!status.value || !status.value.total_chapters) return false
  return status.value.extracted_count < status.value.total_chapters
})

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    NONE: '未开始', PENDING: '等待中', RUNNING: '进行中',
    BATCH_DONE: '本批完成', COMPLETED: '已完成', PARTIAL_FAILED: '部分失败',
    FAILED: '失败', CANCELLED: '已取消',
  }
  return map[status.value?.status ?? 'NONE'] ?? status.value?.status ?? '未开始'
})

// 当前抽取是否使用 mock（测试模型）→ 前端需醒目提示，避免用户误以为抽到真实结果
const isMock = computed(() => status.value?.is_mock === true || status.value?.provider === 'mock')

// 真实模型下本批是否真的产出有效结果。is_mock=false 但 outcome≠success 时需提示
// （真实 provider + 无效 key/调用失败 → 全部 FAILED，但 is_mock 仍为 false）
const realProviderNoResult = computed(() =>
  !isMock.value
  && status.value?.last_run_outcome
  && status.value.last_run_outcome !== 'none'
  && status.value.last_run_outcome !== 'success'
)

const tabLabels: Record<StructTable, string> = {
  character_profile: '人物',
  ability_profile: '能力',
  event_timeline: '事件',
  world_rule: '世界规则',
}

function extractionRequestBody() {
  return {
    genre: selectedGenre.value,
    // 真实 LLM 单章就可能耗时几十秒；每次只推进 1 章，避免页面长时间卡在同一个请求里。
    // mock 很快，保留较大的批量便于测试流程。
    max_chapters_per_run: isMock.value ? 20 : 1,
  }
}

async function loadStatus() {
  statusLoading.value = true
  try {
    status.value = await api.getExtractionStatus(props.projectId, props.sourceId)
  } catch (e) {
    // 接口可能返回 NONE
    status.value = null
  } finally {
    statusLoading.value = false
  }
}

async function splitChapters() {
  actionLoading.value = 'split'
  try {
    const r = await api.splitChapters(props.projectId, props.sourceId)
    splitResult.value = { chapter_count: r.chapter_count, split_type: r.split_type }
    ui.showToast(`切分完成：${r.chapter_count} 章（${r.split_type === 'chapter_regex' ? '正则' : '长度兜底'}）`, 'success')
  } catch (e) {
    ui.showToast(friendlyError(e, '切分失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

async function startExtraction() {
  // mock 模式下二次确认，避免用户误把模拟数据当成真实抽取结果
  if (isMock.value) {
    if (!confirm('当前为测试模型（mock），结构化抽取结果为模拟数据，不代表真实小说抽取结果。建议在设置中配置真实模型后再正式抽取。是否仍要继续？')) {
      return
    }
  }
  actionLoading.value = 'extract'
  try {
    const r = await api.startExtraction(props.projectId, props.sourceId, extractionRequestBody())
    status.value = r
    ui.showToast(`抽取已启动，处理中...`, 'success')
    // 如果还在运行，轮询
    if (r.status === 'RUNNING' || r.status === 'PENDING') {
      pollStatus()
    }
  } catch (e) {
    ui.showToast(friendlyError(e, '启动抽取失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

async function resetExtraction() {
  if (!confirm('会清空当前资料的抽取任务和结构化结果，但不会删除原文资料。确认？')) return
  actionLoading.value = 'reset'
  try {
    const r = await api.resetExtraction(props.projectId, props.sourceId)
    ui.showToast(`已重置抽取结果（清空 ${r.deleted_staging} 条暂存 + ${r.deleted_characters + r.deleted_abilities + r.deleted_events + r.deleted_world_rules} 条结构化数据）`, 'success')
    // 重置后状态回到 NONE，结构化数据清空
    status.value = null
    splitResult.value = null
    autoAdvance.value = false
    failures.value = []
    for (const key of Object.keys(structData.value) as (keyof typeof structData.value)[]) {
      structData.value[key] = []
      structTotals.value[key] = 0
    }
    qaAnswer.value = ''
    qaCitations.value = []
    qaQueryPlan.value = null
    await loadStatus()
  } catch (e) {
    ui.showToast(friendlyError(e, '重置失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

// ── 任务控制：暂停/取消/失败列表/单章重试/自动连续 ──
const autoAdvance = ref(false)
const failures = ref<ExtractionFailure[]>([])

async function pauseExtraction() {
  actionLoading.value = 'pause'
  try {
    const r = await api.pauseExtraction(props.projectId, props.sourceId)
    status.value = r
    autoAdvance.value = false
    ui.showToast('已暂停', 'success')
  } catch (e) {
    ui.showToast(friendlyError(e, '暂停失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

async function cancelExtraction() {
  if (!confirm('确认取消抽取任务？已有结果不会删除。')) return
  actionLoading.value = 'cancel'
  try {
    const r = await api.cancelExtraction(props.projectId, props.sourceId)
    status.value = r
    autoAdvance.value = false
    ui.showToast('已取消', 'success')
    await loadFailures()
  } catch (e) {
    ui.showToast(friendlyError(e, '取消失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

async function loadFailures() {
  try {
    const r = await api.listExtractionFailures(props.projectId, props.sourceId)
    failures.value = r.items
  } catch {
    // 静默失败
  }
}

async function retryChapter(chapterNo: number) {
  actionLoading.value = `retry-${chapterNo}`
  try {
    const r = await api.retryExtractionChapter(props.projectId, props.sourceId, chapterNo)
    if (r.chapter_status === 'MERGED') {
      ui.showToast(`第${chapterNo}章重试成功`, 'success')
    } else {
      ui.showToast(`第${chapterNo}章重试完成（状态：${r.chapter_status}）`, 'info')
    }
    await loadFailures()
    await loadStatus()
    await loadStructuredData()
  } catch (e) {
    ui.showToast(friendlyError(e, '重试失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

function onAutoAdvanceToggle() {
  if (autoAdvance.value) {
    // 开启自动连续抽取：启动推进循环
    autoAdvanceLoop()
  }
}

let autoAdvanceTimer: ReturnType<typeof setTimeout> | null = null

async function autoAdvanceLoop() {
  if (!autoAdvance.value) return
  // 暂停/取消/完成时停止
  if (!status.value || !['BATCH_DONE', 'RUNNING'].includes(status.value.status)) {
    autoAdvance.value = false
    return
  }
  if (!hasMoreChapters.value) {
    autoAdvance.value = false
    ui.showToast('全部章节已处理完', 'success')
    return
  }
  // 推进 1 章
  if (!isAdvancing) {
    isAdvancing = true
    try {
      const r = await api.startExtraction(props.projectId, props.sourceId, extractionRequestBody())
      status.value = r
      if (r.failed_count > 0) await loadFailures()
      // 完成后刷新结构化数据
      if (!['RUNNING', 'PENDING', 'BATCH_DONE'].includes(r.status)) {
        await loadStructuredData()
      }
    } catch {
      // 推进失败不中断，下次再试
    } finally {
      isAdvancing = false
    }
  }
  // 间隔 2 秒继续
  if (autoAdvance.value && hasMoreChapters.value) {
    autoAdvanceTimer = setTimeout(() => autoAdvanceLoop(), 2000)
  }
}

let pollTimer: ReturnType<typeof setTimeout> | null = null
let isAdvancing = false  // 防止轮询推进重叠

function pollStatus() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    await loadStatus()
    if (isRunning.value) {
      // RUNNING 状态：只轮询状态，不自动推进（自动推进由 autoAdvance 开关控制）
      pollStatus()
    } else {
      // 完成后刷新结构化数据和失败列表
      await loadStructuredData()
      if (status.value?.failed_count) await loadFailures()
      // 如果自动连续抽取开启，继续推进
      if (autoAdvance.value && hasMoreChapters.value) {
        autoAdvanceLoop()
      }
    }
  }, 3000)
}

// 组件卸载时清理定时器
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
  if (autoAdvanceTimer) clearTimeout(autoAdvanceTimer)
  autoAdvance.value = false
})

async function loadStructuredData() {
  structLoading.value = true
  try {
    const tables: StructTable[] = ['character_profile', 'ability_profile', 'event_timeline', 'world_rule']
    await Promise.all(tables.map(async (t) => {
      const r = await api.listStructuredKnowledge<any>(props.projectId, t, { limit: 100 })
      structData.value[t] = r.items
      structTotals.value[t] = r.total
    }))
  } catch (e) {
    // 静默失败，结构化数据可能为空
  } finally {
    structLoading.value = false
  }
}

async function askStructured() {
  if (!qaQuestion.value.trim()) return
  qaLoading.value = true
  qaAnswer.value = ''
  qaCitations.value = []
  qaQueryPlan.value = null
  try {
    const r = await api.structuredQA(props.projectId, qaQuestion.value)
    qaAnswer.value = r.answer
    qaCitations.value = r.citations || []
    qaQueryPlan.value = r.query_plan
  } catch (e) {
    qaAnswer.value = friendlyError(e, '查询失败')
  } finally {
    qaLoading.value = false
  }
}

// ── 结构化知识人工修正（增删改查） ──
const editing = ref(false)
const editingRecord = ref<Record<string, any> | null>(null)
const editingId = ref<string | null>(null) // null = 新增
const editSaving = ref(false)

// 各表编辑字段配置
const editFieldConfig: Record<StructTable, { key: string; label: string; type?: 'text' | 'textarea' | 'number' }[]> = {
  character_profile: [
    { key: 'name', label: '姓名' },
    { key: 'aliases', label: '别名（逗号分隔）' },
    { key: 'identity_desc', label: '身份', type: 'textarea' },
    { key: 'status_desc', label: '状态', type: 'textarea' },
  ],
  ability_profile: [
    { key: 'character_name', label: '人物名' },
    { key: 'ability_type', label: '类型' },
    { key: 'ability_name', label: '能力名' },
    { key: 'level_desc', label: '等级' },
    { key: 'status', label: '状态' },
  ],
  event_timeline: [
    { key: 'event_title', label: '标题' },
    { key: 'event_desc', label: '描述', type: 'textarea' },
    { key: 'characters', label: '人物（逗号分隔）' },
    { key: 'location_desc', label: '地点' },
    { key: 'importance', label: '重要度(1-5)', type: 'number' },
  ],
  world_rule: [
    { key: 'category', label: '分类' },
    { key: 'rule_text', label: '规则文本', type: 'textarea' },
    { key: 'priority', label: '优先级(high/medium/low)' },
  ],
}

function openCreate() {
  editingRecord.value = {}
  editingId.value = null
  editing.value = true
}

function openEdit(item: Record<string, any>) {
  // 深拷贝，aliases/characters 转为逗号字符串方便编辑
  const copy: Record<string, any> = { ...item }
  if (Array.isArray(copy.aliases)) copy.aliases = copy.aliases.join('、')
  if (Array.isArray(copy.characters)) copy.characters = copy.characters.join('、')
  editingRecord.value = copy
  editingId.value = item.id
  editing.value = true
}

async function saveEdit() {
  if (!editingRecord.value) return
  editSaving.value = true
  try {
    const body: Record<string, any> = { ...editingRecord.value }
    // 删除不应提交的字段
    delete body.id
    delete body.source_id
    delete body.canon_level
    delete body.origin
    delete body.source_priority
    delete body.evidence
    delete body.created_at
    delete body.chapter_no
    // 数组字段从逗号字符串转回
    if (typeof body.aliases === 'string') body.aliases = body.aliases.split(/[、,，]/).map((s: string) => s.trim()).filter(Boolean)
    if (typeof body.characters === 'string') body.characters = body.characters.split(/[、,，]/).map((s: string) => s.trim()).filter(Boolean)
    if (body.importance != null) body.importance = Number(body.importance)

    if (editingId.value) {
      await api.updateStructuredRecord(props.projectId, activeTab.value, editingId.value, body)
      ui.showToast('已保存（手动修正，优先级最高）', 'success')
    } else {
      await api.createStructuredRecord(props.projectId, activeTab.value, body)
      ui.showToast('已新增（手动记录）', 'success')
    }
    editing.value = false
    editingRecord.value = null
    await loadStructuredData()
  } catch (e) {
    ui.showToast(friendlyError(e, '保存失败'), 'error')
  } finally {
    editSaving.value = false
  }
}

// ── 人物出场记录 ──
const appearanceChar = ref<{ id: string; name: string } | null>(null)
const appearances = ref<CharacterAppearanceItem[]>([])
const appearanceLoading = ref(false)

async function loadAppearances(char: { id: string; name: string }) {
  appearanceChar.value = char
  appearanceLoading.value = true
  appearances.value = []
  try {
    const r = await api.listCharacterAppearances(props.projectId, char.id, 100)
    appearances.value = r.items
  } catch {
    // 静默
  } finally {
    appearanceLoading.value = false
  }
}

function closeAppearances() {
  appearanceChar.value = null
  appearances.value = []
}

async function deleteRecord(item: Record<string, any>) {
  if (!confirm(`确认删除「${item.name || item.ability_name || item.event_title || item.rule_text || ''}」？`)) return
  try {
    await api.deleteStructuredRecord(props.projectId, activeTab.value, item.id)
    ui.showToast('已删除', 'success')
    await loadStructuredData()
  } catch (e) {
    ui.showToast(friendlyError(e, '删除失败'), 'error')
  }
}

watch(() => props.sourceId, () => {
  // source 切换时同步 genre（从 metadata 读取）
  if (props.initialGenre === 'historical') {
    selectedGenre.value = 'historical'
  } else if (props.initialGenre === 'magic_fantasy') {
    selectedGenre.value = 'magic_fantasy'
  }
  // 否则保持当前选择（无 metadata 时不强制重置）
  loadStatus()
  loadStructuredData()
}, { immediate: false })

onMounted(() => {
  loadStatus()
  loadStructuredData()
})

function abilityTypeLabel(t: string) {
  const m: Record<string, string> = {
    magic_element: '魔法系别', spell: '法术', cultivation_level: '境界',
    martial_art: '武技', item: '物品', bloodline: '血脉', skill: '技能', unknown: '其它',
  }
  return m[t] ?? t
}
function priorityLabel(p: string) {
  return { high: '高', medium: '中', low: '低' }[p] ?? p
}
function evidenceKindLabel(k: string) {
  const m: Record<string, string> = {
    exact_quote: '原文引用', summary: '摘要证据', manual_note: '手动备注', inferred: '推断',
  }
  return m[k] ?? k
}
function evidenceText(ev: any) {
  if (ev == null) return ''
  if (typeof ev === 'string') return ev
  if (typeof ev === 'object') return ev.text ?? ''
  return String(ev)
}
</script>

<template>
  <div class="novel-extraction" v-if="sourceType === 'novel'">
    <h4>结构化抽取</h4>

    <!-- 抽取状态区 -->
    <div class="extraction-status">
      <div class="status-row">
        <span class="status-badge" :class="status?.status ?? 'NONE'">{{ statusLabel }}</span>
        <span v-if="status && status.total_chapters">章节 {{ status.extracted_count }}/{{ status.total_chapters }}</span>
        <span v-if="status && status.merged_count">已合并 {{ status.merged_count }}</span>
        <span v-if="status && status.failed_count" class="failed-count">失败 {{ status.failed_count }}</span>
        <span v-if="status?.provider" class="provider-tag">{{ status.provider }}</span>
      </div>

      <div v-if="status?.current_chapter_no" class="current-chapter">
        {{ status.status === 'RUNNING' ? '正在处理' : '上次处理' }}第 {{ status.current_chapter_no }} 章
      </div>

      <div v-if="status && status.total_chapters" class="progress-bar">
        <div class="progress-fill" :style="{ width: progressPercent + '%' }"></div>
        <span class="progress-text">{{ progressPercent }}%</span>
      </div>

      <div v-if="status?.last_error" class="error-msg">最近错误：{{ status.last_error }}</div>
      <div v-if="status?.error_message" class="error-msg">{{ status.error_message }}</div>
      <div v-if="splitResult" class="split-info">已切分 {{ splitResult.chapter_count }} 章</div>

      <div v-if="isMock" class="mock-warning">
        当前为 mock，结果仅用于流程测试。
      </div>
      <div v-else class="real-model-hint">
        真实模型每章可能需要几十秒。当前采用单章推进，避免页面长时间无响应。
      </div>

      <div v-if="realProviderNoResult" class="real-no-result-warning">
        已配置真实模型但本批无有效抽取结果（outcome={{ status?.last_run_outcome }}），可能 API Key / base_url / 模型输出格式有问题。请检查设置与后端日志后重试。
      </div>

      <div class="genre-row">
        <label class="genre-label">小说类型：</label>
        <select v-model="selectedGenre" class="genre-select">
          <option value="magic_fantasy">魔法玄幻</option>
          <option value="historical">历史</option>
        </select>
        <span class="genre-hint">抽取重点按类型调整</span>
      </div>

      <div class="extraction-actions">
        <button class="btn-sm" :disabled="actionLoading === 'split'" @click="splitChapters">
          {{ actionLoading === 'split' ? '切分中...' : '切分章节' }}
        </button>
        <!-- 未开始 / BATCH_DONE / PAUSED → 开始/继续抽取 -->
        <button
          class="btn-sm"
          v-if="!status || status.status === 'NONE' || status.status === 'BATCH_DONE' || status.status === 'PAUSED'"
          :disabled="actionLoading === 'extract'"
          @click="startExtraction"
        >
          {{ actionLoading === 'extract' ? (isMock ? '启动中...' : '抽取中（约1章）...') : (status?.status === 'BATCH_DONE' ? '继续抽取下一章' : status?.status === 'PAUSED' ? '继续抽取' : '开始抽取') }}
        </button>
        <!-- 运行中 → 暂停 + 取消 -->
        <button class="btn-sm" v-if="status?.status === 'RUNNING'" :disabled="actionLoading === 'pause'" @click="pauseExtraction">
          {{ actionLoading === 'pause' ? '暂停中...' : '暂停' }}
        </button>
        <!-- BATCH_DONE → 暂停 + 取消 -->
        <button class="btn-sm" v-if="status?.status === 'BATCH_DONE'" :disabled="actionLoading === 'pause'" @click="pauseExtraction">
          {{ actionLoading === 'pause' ? '暂停中...' : '暂停' }}
        </button>
        <!-- 非终态 → 取消 -->
        <button class="btn-sm btn-danger" v-if="status && ['RUNNING', 'BATCH_DONE', 'PAUSED'].includes(status.status)" :disabled="actionLoading === 'cancel'" @click="cancelExtraction">
          {{ actionLoading === 'cancel' ? '取消中...' : '取消' }}
        </button>
        <!-- 终态 → 重置 -->
        <button class="btn-sm btn-danger" v-if="status && ['COMPLETED', 'FAILED', 'PARTIAL_FAILED', 'CANCELLED'].includes(status.status)" :disabled="actionLoading === 'reset'" @click="resetExtraction">
          {{ actionLoading === 'reset' ? '重置中...' : '重置抽取' }}
        </button>
      </div>

      <!-- 自动连续抽取开关（默认关闭） -->
      <div v-if="status && ['BATCH_DONE', 'RUNNING'].includes(status.status) && hasMoreChapters && !isMock" class="auto-advance-row">
        <label class="auto-advance-toggle">
          <input type="checkbox" v-model="autoAdvance" @change="onAutoAdvanceToggle" />
          自动连续抽取
        </label>
        <span class="auto-advance-hint" v-if="autoAdvance">已开启，每章间隔 2 秒自动推进</span>
      </div>

      <div v-if="status?.status === 'BATCH_DONE' && hasMoreChapters" class="batch-hint">
        已处理 {{ status.extracted_count }} / {{ status.total_chapters }} 章。本批已完成，可继续抽取下一章。
      </div>
    </div>

    <!-- 失败章节面板 -->
    <div v-if="failures.length > 0" class="failures-panel">
      <div class="failures-title">失败章节（{{ failures.length }}）</div>
      <div v-for="f in failures" :key="f.chapter_no" class="failure-item">
        <span class="failure-chapter">第{{ f.chapter_no }}章 {{ f.chapter_title || '' }}</span>
        <span class="failure-status">{{ f.status }}</span>
        <span class="failure-error" v-if="f.error_message">{{ f.error_message }}</span>
        <button class="btn-sm btn-retry" :disabled="actionLoading === `retry-${f.chapter_no}`" @click="retryChapter(f.chapter_no)">
          {{ actionLoading === `retry-${f.chapter_no}` ? '重试中...' : '重试' }}
        </button>
      </div>
    </div>

    <!-- 结构化结果 tabs -->
    <div class="struct-results" v-if="!structLoading || structTotals[activeTab] > 0">
      <div class="struct-tabs">
        <button
          v-for="(label, key) in tabLabels" :key="key"
          class="struct-tab" :class="{ active: activeTab === key }"
          @click="activeTab = key"
        >
          {{ label }} <span class="tab-count">{{ structTotals[key as StructTable] }}</span>
        </button>
        <button class="btn-add" @click="openCreate">+ 新增{{ tabLabels[activeTab] }}</button>
      </div>

      <div class="struct-list">
        <template v-if="structData[activeTab].length === 0">
          <div class="empty-list">暂无{{ tabLabels[activeTab] }}数据，请先抽取</div>
        </template>

        <!-- 人物 -->
        <template v-else-if="activeTab === 'character_profile'">
          <div v-for="c in structData.character_profile" :key="c.id" class="struct-item">
            <div class="item-header">
              <strong>{{ c.name }}</strong>
              <span class="badge" :class="c.canon_level">{{ c.canon_level }}</span>
              <span class="confidence">置信度 {{ (c.confidence * 100).toFixed(0) }}%</span>
              <span class="item-actions">
                <button class="btn-icon" title="查看出场记录" @click="loadAppearances(c)">📋</button>
                <button class="btn-icon" title="编辑" @click="openEdit(c)">✏️</button>
                <button class="btn-icon" title="删除" @click="deleteRecord(c)">🗑️</button>
              </span>
            </div>
            <div v-if="c.identity_desc" class="item-field">身份：{{ c.identity_desc }}</div>
            <div v-if="c.status_desc" class="item-field">状态：{{ c.status_desc }}</div>
            <div v-if="c.aliases?.length" class="item-field">别名：{{ c.aliases.join('、') }}</div>
            <div v-if="c.appearance_count" class="item-stats">
              <span v-if="c.first_seen_chapter">首次：第{{ c.first_seen_chapter }}章</span>
              <span v-if="c.last_seen_chapter">最近：第{{ c.last_seen_chapter }}章</span>
              <span>出场：{{ c.appearance_count }}次</span>
            </div>
            <div v-if="c.evidence?.length" class="item-evidence">关键证据：{{ evidenceText(c.evidence[0]) }}</div>
          </div>
        </template>

        <!-- 能力 -->
        <template v-else-if="activeTab === 'ability_profile'">
          <div v-for="a in structData.ability_profile" :key="a.id" class="struct-item">
            <div class="item-header">
              <strong>{{ a.character_name }} - {{ a.ability_name }}</strong>
              <span class="badge" :class="a.canon_level">{{ a.canon_level }}</span>
              <span class="type-tag">{{ abilityTypeLabel(a.ability_type) }}</span>
              <span class="item-actions">
                <button class="btn-icon" title="编辑" @click="openEdit(a)">✏️</button>
                <button class="btn-icon" title="删除" @click="deleteRecord(a)">🗑️</button>
              </span>
            </div>
            <div v-if="a.level_desc" class="item-field">等级：{{ a.level_desc }}</div>
            <div v-if="a.status" class="item-field">状态：{{ a.status }}</div>
            <div v-if="a.chapter_no" class="item-field">首次出现：第{{ a.chapter_no }}章</div>
            <div v-if="a.evidence?.length" class="item-evidence">证据：{{ evidenceText(a.evidence[0]) }}</div>
          </div>
        </template>

        <!-- 事件 -->
        <template v-else-if="activeTab === 'event_timeline'">
          <div v-for="e in structData.event_timeline" :key="e.id" class="struct-item">
            <div class="item-header">
              <strong>{{ e.event_title }}</strong>
              <span v-if="e.chapter_no" class="chapter-tag">第{{ e.chapter_no }}章</span>
              <span class="importance-tag">重要度 {{ e.importance }}</span>
              <span class="item-actions">
                <button class="btn-icon" title="编辑" @click="openEdit(e)">✏️</button>
                <button class="btn-icon" title="删除" @click="deleteRecord(e)">🗑️</button>
              </span>
            </div>
            <div v-if="e.event_desc" class="item-field">{{ e.event_desc }}</div>
            <div v-if="e.characters?.length" class="item-field">人物：{{ e.characters.join('、') }}</div>
            <div v-if="e.evidence?.length" class="item-evidence">证据：{{ evidenceText(e.evidence[0]) }}</div>
          </div>
        </template>

        <!-- 世界规则 -->
        <template v-else>
          <div v-for="w in structData.world_rule" :key="w.id" class="struct-item">
            <div class="item-header">
              <strong>{{ w.category }}</strong>
              <span class="priority-tag" :class="w.priority">{{ priorityLabel(w.priority) }}</span>
              <span class="item-actions">
                <button class="btn-icon" title="编辑" @click="openEdit(w)">✏️</button>
                <button class="btn-icon" title="删除" @click="deleteRecord(w)">🗑️</button>
              </span>
            </div>
            <div class="item-field">{{ w.rule_text }}</div>
            <div v-if="w.evidence?.length" class="item-evidence">证据：{{ evidenceText(w.evidence[0]) }}</div>
          </div>
        </template>
      </div>
    </div>

    <!-- 结构化 QA 测试入口 -->
    <div class="struct-qa">
      <h4>结构化问答</h4>
      <div class="qa-input-row">
        <input
          v-model="qaQuestion"
          placeholder="如：莫凡有什么系别？/ 发生了什么事件？/ 魔法等级怎么划分？"
          @keyup.enter="askStructured"
        />
        <button class="btn-sm" :disabled="qaLoading || !qaQuestion.trim()" @click="askStructured">
          {{ qaLoading ? '查询中...' : '提问' }}
        </button>
      </div>

      <div v-if="qaAnswer" class="qa-result">
        <div v-if="qaQueryPlan" class="qa-source-tag">
          结构化知识 · {{ qaQueryPlan.intent }} · {{ (qaQueryPlan.tables || []).join(', ') }}
        </div>
        <pre class="qa-answer">{{ qaAnswer }}</pre>
        <div v-if="qaCitations.length" class="qa-citations">
          <div class="citations-title">引用（{{ qaCitations.length }}）：</div>
          <div v-for="(c, i) in qaCitations" :key="i" class="citation-item">
            <span class="citation-table">{{ c.table || '事实' }}</span>
            <span v-if="c.evidence_kind" class="citation-kind" :class="c.evidence_kind">{{ evidenceKindLabel(c.evidence_kind) }}</span>
            <span v-if="c.chapter_no" class="citation-chapter">第{{ c.chapter_no }}章</span>
            <span class="citation-snippet">{{ c.snippet }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑/新增弹窗 -->
    <div v-if="editing" class="edit-modal-overlay">
      <div class="edit-modal">
        <h4>{{ editingId ? '编辑' : '新增' }}{{ tabLabels[activeTab] }}</h4>
        <div v-for="field in editFieldConfig[activeTab]" :key="field.key" class="edit-field">
          <label>{{ field.label }}</label>
          <textarea
            v-if="field.type === 'textarea'"
            v-model="editingRecord![field.key]"
            rows="2"
          ></textarea>
          <input
            v-else
            v-model="editingRecord![field.key]"
            :type="field.type === 'number' ? 'number' : 'text'"
          />
        </div>
        <div class="edit-field">
          <label>手动备注（可选）</label>
          <textarea v-model="editingRecord!.manual_note" rows="2" placeholder="修正说明，会追加到 evidence"></textarea>
        </div>
        <div class="edit-actions">
          <button class="btn-sm" @click="editing = false">取消</button>
          <button class="btn-sm btn-primary" :disabled="editSaving" @click="saveEdit">
            {{ editSaving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 出场记录面板 -->
    <div v-if="appearanceChar" class="edit-modal-overlay">
      <div class="edit-modal">
        <h4>{{ appearanceChar.name }} 的出场记录（{{ appearances.length }}）</h4>
        <div v-if="appearanceLoading" class="loading-hint">加载中...</div>
        <div v-else-if="appearances.length === 0" class="loading-hint">暂无出场记录</div>
        <div v-else class="appearance-list">
          <div v-for="a in appearances" :key="a.chapter_no" class="appearance-item">
            <div class="appearance-header">
              <span class="appearance-chapter">第{{ a.chapter_no }}章 {{ a.chapter_title || '' }}</span>
              <span class="appearance-importance">重要度 {{ a.importance }}</span>
            </div>
            <div v-if="a.summary" class="appearance-summary">{{ a.summary }}</div>
            <div class="appearance-evidence">{{ a.evidence_text }}</div>
          </div>
        </div>
        <div class="edit-actions">
          <button class="btn-sm" @click="closeAppearances">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.novel-extraction { margin-top: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; }
.novel-extraction h4 { margin: 0 0 8px; font-size: 14px; }
.extraction-status { margin-bottom: 12px; }
.status-row { display: flex; gap: 12px; align-items: center; font-size: 13px; margin-bottom: 6px; }
.status-badge { padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.status-badge.COMPLETED { background: #2d8a4e; color: #fff; }
.status-badge.BATCH_DONE { background: #2563eb; color: #fff; }
.status-badge.RUNNING, .status-badge.PENDING { background: #d9a300; color: #fff; }
.status-badge.FAILED, .status-badge.PARTIAL_FAILED { background: #c0392b; color: #fff; }
.status-badge.NONE { background: var(--bg-soft); color: var(--text-soft); }
.failed-count { color: #c0392b; }
.progress-bar { position: relative; height: 18px; background: var(--bg-soft); border-radius: 4px; margin: 6px 0; overflow: hidden; }
.progress-fill { height: 100%; background: #3b82f6; transition: width 0.3s; }
.progress-text { position: absolute; top: 0; left: 50%; transform: translateX(-50%); font-size: 11px; line-height: 18px; color: #fff; }
.error-msg { color: #c0392b; font-size: 12px; margin: 4px 0; }
.split-info { font-size: 12px; color: var(--text-soft); }
.mock-warning { font-size: 12px; color: #92400e; margin: 6px 0; padding: 6px 10px; background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.4); border-radius: 4px; line-height: 1.5; }
.real-no-result-warning { font-size: 12px; color: #b91c1c; margin: 6px 0; padding: 6px 10px; background: rgba(220,38,38,0.10); border: 1px solid rgba(220,38,38,0.4); border-radius: 4px; line-height: 1.5; }
.batch-hint { font-size: 12px; color: #2563eb; margin-top: 6px; padding: 4px 8px; background: rgba(37,99,235,0.1); border-radius: 4px; }
.extraction-actions { display: flex; gap: 8px; margin-top: 8px; }
.current-chapter { font-size: 13px; color: #2563eb; margin: 4px 0; font-weight: 500; }
.provider-tag { font-size: 11px; padding: 1px 6px; border-radius: 3px; background: var(--bg-soft); color: var(--text-soft); }
.real-model-hint { font-size: 12px; color: var(--text-soft); margin: 6px 0; padding: 6px 8px; background: #eff6ff; border-radius: 4px; border-left: 3px solid #3b82f6; }
.auto-advance-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 13px; }
.auto-advance-toggle { display: flex; align-items: center; gap: 4px; cursor: pointer; }
.auto-advance-toggle input { cursor: pointer; }
.auto-advance-hint { font-size: 12px; color: var(--text-soft); }
.failures-panel { margin-top: 12px; padding: 8px; border: 1px solid #fca5a5; border-radius: 6px; background: #fef2f2; }
.failures-title { font-size: 13px; font-weight: 600; color: #b91c1c; margin-bottom: 6px; }
.failure-item { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 12px; border-bottom: 1px solid #fecaca; }
.failure-item:last-child { border-bottom: none; }
.failure-chapter { flex-shrink: 0; min-width: 120px; }
.failure-status { font-size: 11px; padding: 1px 6px; border-radius: 3px; background: #fee2e2; color: #991b1b; }
.failure-error { flex: 1; color: #7f1d1d; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn-retry { padding: 2px 8px; font-size: 12px; }
.genre-row { display: flex; align-items: center; gap: 6px; margin: 8px 0; font-size: 12px; }
.genre-label { color: var(--text-soft); }
.genre-select { padding: 2px 6px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); }
.genre-hint { color: var(--text-soft); font-size: 11px; }
.btn-sm { padding: 4px 10px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); cursor: pointer; }
.btn-sm:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-sm.btn-danger { color: #c0392b; border-color: #c0392b; }
.struct-results { margin-top: 12px; }
.struct-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); }
.struct-tab { padding: 4px 12px; font-size: 12px; border: none; background: none; cursor: pointer; border-bottom: 2px solid transparent; }
.struct-tab.active { border-bottom-color: #3b82f6; color: #3b82f6; }
.tab-count { font-size: 10px; opacity: 0.6; }
.struct-list { max-height: 360px; overflow-y: auto; padding: 8px 0; }
.empty-list { color: var(--text-soft); font-size: 13px; padding: 12px 0; }
.struct-item { padding: 8px; border-bottom: 1px solid var(--border); font-size: 13px; }
.item-header { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
.badge { padding: 1px 6px; border-radius: 3px; font-size: 10px; }
.badge.manual { background: #8b5cf6; color: #fff; }
.badge.fanfic { background: #ec4899; color: #fff; }
.badge.original { background: #6b7280; color: #fff; }
.confidence { font-size: 11px; color: var(--text-soft); }
.item-field { font-size: 12px; color: var(--text-soft); margin: 2px 0; }
.item-evidence { font-size: 11px; color: var(--text-soft); margin-top: 4px; padding: 2px 6px; background: var(--bg-soft); border-radius: 3px; }
.type-tag, .chapter-tag, .importance-tag, .priority-tag { font-size: 10px; padding: 1px 5px; border-radius: 3px; background: var(--bg-soft); }
.priority-tag.high { background: #c0392b; color: #fff; }
.priority-tag.medium { background: #d9a300; color: #fff; }
.priority-tag.low { background: #6b7280; color: #fff; }
.struct-qa { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.qa-input-row { display: flex; gap: 8px; }
.qa-input-row input { flex: 1; padding: 6px 8px; font-size: 13px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); }
.qa-result { margin-top: 8px; }
.qa-source-tag { font-size: 11px; color: #3b82f6; margin-bottom: 4px; }
.qa-answer { white-space: pre-wrap; font-size: 13px; line-height: 1.5; padding: 8px; background: var(--bg-soft); border-radius: 4px; margin: 0; }
.qa-citations { margin-top: 8px; }
.citations-title { font-size: 12px; color: var(--text-soft); margin-bottom: 4px; }
.citation-item { font-size: 11px; padding: 3px 0; border-bottom: 1px dashed var(--border); display: flex; gap: 6px; }
.citation-table { color: #3b82f6; flex-shrink: 0; }
.citation-kind { font-size: 10px; padding: 1px 5px; border-radius: 3px; flex-shrink: 0; }
.citation-kind.exact_quote { background: #2d8a4e; color: #fff; }
.citation-kind.summary { background: #d9a300; color: #fff; }
.citation-kind.manual_note { background: #8b5cf6; color: #fff; }
.citation-kind.inferred { background: #6b7280; color: #fff; }
.citation-chapter { color: var(--text-soft); flex-shrink: 0; }
.citation-snippet { color: var(--text-soft); }
.btn-add { margin-left: auto; padding: 4px 10px; font-size: 12px; border: 1px dashed var(--border); border-radius: 4px; background: none; cursor: pointer; color: #3b82f6; }
.btn-add:hover { background: var(--bg-soft); }
.item-actions { margin-left: auto; display: flex; gap: 4px; }
.btn-icon { padding: 2px 6px; font-size: 13px; border: none; background: none; cursor: pointer; border-radius: 3px; }
.btn-icon:hover { background: var(--bg-soft); }
.btn-sm.btn-primary { color: #fff; background: #2563eb; border-color: #2563eb; }
.edit-modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.edit-modal { background: var(--bg); border-radius: 8px; padding: 20px; width: 480px; max-width: 90vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 4px 20px rgba(0,0,0,0.15); }
.edit-modal h4 { margin: 0 0 12px; }
.edit-field { margin-bottom: 10px; }
.edit-field label { display: block; font-size: 12px; color: var(--text-soft); margin-bottom: 4px; }
.edit-field input, .edit-field textarea { width: 100%; padding: 6px 8px; font-size: 13px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); box-sizing: border-box; }
.edit-field textarea { resize: vertical; }
.edit-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.item-stats { display: flex; gap: 12px; font-size: 12px; color: var(--text-soft); margin: 4px 0; }
.appearance-list { max-height: 400px; overflow-y: auto; }
.appearance-item { padding: 6px 0; border-bottom: 1px solid var(--border); }
.appearance-header { display: flex; justify-content: space-between; font-size: 13px; }
.appearance-chapter { font-weight: 500; }
.appearance-importance { font-size: 11px; color: var(--text-soft); }
.appearance-summary { font-size: 12px; color: var(--text); margin: 2px 0; }
.appearance-evidence { font-size: 12px; color: var(--text-soft); margin-top: 2px; }
.loading-hint { text-align: center; padding: 20px; color: var(--text-soft); }
</style>
