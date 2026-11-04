<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api/client'
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
const selectedGenre = ref<'magic_fantasy' | 'historical'>(
  props.initialGenre === 'historical' ? 'historical' : 'magic_fantasy'
)

const progressPercent = computed(() => {
  if (!status.value || !status.value.total_chapters) return 0
  return Math.round((status.value.extracted_count / status.value.total_chapters) * 100)
})

const isRunning = computed(() => status.value?.status === 'RUNNING' || status.value?.status === 'PENDING')
// 是否还有未处理章节
const hasMoreChapters = computed(() => {
  if (!status.value || !status.value.total_chapters) return false
  return status.value.extracted_count < status.value.total_chapters
})

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    NONE: '未开始', PENDING: '等待中', RUNNING: '分类中',
    BATCH_DONE: '进行中', COMPLETED: '已分类', PARTIAL_FAILED: '部分失败',
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
  } catch {
    // 接口可能返回 NONE
    status.value = null
  } finally {
    statusLoading.value = false
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
  if (!confirm('确认取消分类任务？已有结果不会删除。')) return
  actionLoading.value = 'cancel'
  try {
    const r = await api.cancelExtraction(props.projectId, props.sourceId)
    status.value = r
    autoAdvance.value = false
    ui.showToast('已取消', 'success')
  } catch (e) {
    ui.showToast(friendlyError(e, '取消失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

async function resetExtraction() {
  if (!confirm('会清空当前资料的分类任务和结果，但不会删除原文资料。确认？')) return
  actionLoading.value = 'reset'
  try {
    await api.resetExtraction(props.projectId, props.sourceId)
    status.value = null
    ui.showToast('已重置，重新分类中...', 'success')
    autoStartExtraction()
  } catch (e) {
    ui.showToast(friendlyError(e, '重置失败'), 'error')
  } finally {
    actionLoading.value = ''
  }
}

// ── 自动连续分类（上传即自动：默认开启，用户零操作） ──
const autoAdvance = ref(true)
let autoAdvanceTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let isAdvancing = false  // 防止轮询推进重叠

async function autoAdvanceLoop() {
  if (!autoAdvance.value) return
  // 暂停/取消/完成时停止
  if (!status.value || !['BATCH_DONE', 'RUNNING'].includes(status.value.status)) {
    autoAdvance.value = false
    return
  }
  if (!hasMoreChapters.value) {
    autoAdvance.value = false
    return
  }
  // 推进 1 章
  if (!isAdvancing) {
    isAdvancing = true
    try {
      const r = await api.startExtraction(props.projectId, props.sourceId, extractionRequestBody())
      status.value = r
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

function pollStatus() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    await loadStatus()
    if (isRunning.value) {
      // RUNNING 状态：只轮询状态，不自动推进
      pollStatus()
    } else {
      // 自动连续分类开启时，完成后继续推进
      if (autoAdvance.value && hasMoreChapters.value) {
        autoAdvanceLoop()
      }
    }
  }, 3000)
}

async function autoStartExtraction() {
  // 自动起跑（跳过 mock 确认弹窗）：无任务时自动开始
  if (actionLoading.value) return
  actionLoading.value = 'extract'
  try {
    const r = await api.startExtraction(props.projectId, props.sourceId, extractionRequestBody())
    status.value = r
    if (r.status === 'RUNNING' || r.status === 'PENDING') {
      pollStatus()
    } else if (autoAdvance.value && hasMoreChapters.value) {
      autoAdvanceLoop()
    }
  } catch {
    // 自动起跑失败（如未配置模型）：保留手动「开始分类」按钮兜底
  } finally {
    actionLoading.value = ''
  }
}

onMounted(() => {
  loadStatus().then(() => {
    if (!status.value || status.value.status === 'NONE') {
      // 上传后首次进入：自动起跑
      autoStartExtraction()
    } else if (autoAdvance.value && hasMoreChapters.value) {
      // 中断的任务（BATCH_DONE / PARTIAL_FAILED 等）自动继续推进
      autoAdvanceLoop()
    }
  })
})

// 组件卸载时清理定时器
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
  if (autoAdvanceTimer) clearTimeout(autoAdvanceTimer)
  autoAdvance.value = false
})
</script>

<template>
  <div class="novel-extraction" v-if="sourceType === 'novel'">
    <h4>小说自动分类</h4>
    <p class="auto-hint">
      导入后自动识别章节、人物、能力、事件，进度无需干预。直接通过右侧「资料问答」了解人物特点、事件与出处章节。
    </p>

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

      <div v-if="isMock" class="mock-warning">
        当前为测试模型（mock），分类结果为模拟数据。建议在设置中配置真实模型后重新分类。
      </div>
      <div v-else-if="status && (status.status === 'RUNNING' || status.status === 'BATCH_DONE')" class="real-model-hint">
        自动分类进行中（真实模型每章约几十秒），无需操作。
      </div>

      <div v-if="realProviderNoResult" class="real-no-result-warning">
        已配置真实模型但本批无有效抽取结果（outcome={{ status?.last_run_outcome }}），可能 API Key / base_url / 模型输出格式有问题。请检查设置与后端日志后重试。
      </div>

      <!-- 控制（兜底）：自动流程失败或需要干预时使用 -->
      <div class="extraction-actions">
        <button
          class="btn-sm"
          v-if="!status || status.status === 'NONE'"
          :disabled="actionLoading === 'extract'"
          @click="startExtraction"
        >
          {{ actionLoading === 'extract' ? '启动中...' : '开始分类' }}
        </button>
        <button
          class="btn-sm"
          v-if="status?.status === 'RUNNING'"
          :disabled="actionLoading === 'pause'"
          @click="pauseExtraction"
        >
          {{ actionLoading === 'pause' ? '暂停中...' : '暂停' }}
        </button>
        <button
          class="btn-sm btn-danger"
          v-if="status && ['RUNNING', 'BATCH_DONE', 'PAUSED'].includes(status.status)"
          :disabled="actionLoading === 'cancel'"
          @click="cancelExtraction"
        >
          {{ actionLoading === 'cancel' ? '取消中...' : '取消' }}
        </button>
        <button
          class="btn-sm btn-danger"
          v-if="status && ['FAILED', 'PARTIAL_FAILED', 'CANCELLED'].includes(status.status)"
          :disabled="actionLoading === 'reset'"
          @click="resetExtraction"
        >
          {{ actionLoading === 'reset' ? '重置中...' : '重新分类' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.novel-extraction { margin-top: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; }
.novel-extraction h4 { margin: 0 0 6px; font-size: 14px; }
.auto-hint { font-size: 12px; color: var(--text-soft); margin: 0 0 10px; line-height: 1.6; }
.extraction-status { margin-bottom: 12px; }
.status-row { display: flex; gap: 12px; align-items: center; font-size: 13px; margin-bottom: 6px; flex-wrap: wrap; }
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
.mock-warning { font-size: 12px; color: #92400e; margin: 6px 0; padding: 6px 10px; background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.4); border-radius: 4px; line-height: 1.5; }
.real-no-result-warning { font-size: 12px; color: #b91c1c; margin: 6px 0; padding: 6px 10px; background: rgba(220,38,38,0.10); border: 1px solid rgba(220,38,38,0.4); border-radius: 4px; line-height: 1.5; }
.extraction-actions { display: flex; gap: 8px; margin-top: 8px; }
.current-chapter { font-size: 13px; color: #2563eb; margin: 4px 0; font-weight: 500; }
.provider-tag { font-size: 11px; padding: 1px 6px; border-radius: 3px; background: var(--bg-soft); color: var(--text-soft); }
.real-model-hint { font-size: 12px; color: var(--text-soft); margin: 6px 0; padding: 6px 8px; background: #eff6ff; border-radius: 4px; border-left: 3px solid #3b82f6; }
.btn-sm { padding: 4px 10px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); cursor: pointer; }
.btn-sm:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-sm.btn-danger { color: #c0392b; border-color: #c0392b; }
</style>
