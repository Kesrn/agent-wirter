<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
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
  error_message?: string | null
  provider?: string | null
  is_mock?: boolean
  last_run_outcome?: string  // none | success | partial | failed
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
const canAdvance = computed(() => {
  if (!status.value) return false
  // BATCH_DONE 表示本批完成但全书还有未处理章节，可继续推进
  return ['RUNNING', 'PENDING', 'PARTIAL_FAILED', 'BATCH_DONE'].includes(status.value.status)
})
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
    const r = await api.startExtraction(props.projectId, props.sourceId, {
      genre: selectedGenre.value,
      max_chapters_per_run: 20,
    })
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

async function advanceExtraction() {
  actionLoading.value = 'advance'
  try {
    const r = await api.startExtraction(props.projectId, props.sourceId, {
      genre: selectedGenre.value,
      max_chapters_per_run: 20,
    })
    status.value = r
    if (r.status === 'RUNNING' || r.status === 'PENDING') {
      pollStatus()
    } else {
      // 完成后刷新结构化数据
      await loadStructuredData()
    }
  } catch (e) {
    ui.showToast(friendlyError(e, '推进失败'), 'error')
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

let pollTimer: ReturnType<typeof setTimeout> | null = null
let isAdvancing = false  // 防止轮询推进重叠

function pollStatus() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    await loadStatus()
    if (isRunning.value) {
      // 自动推进下一批章节（轮询推进模式，不依赖用户手动点）
      if (!isAdvancing) {
        isAdvancing = true
        try {
          const r = await api.startExtraction(props.projectId, props.sourceId, {
            genre: selectedGenre.value,
            max_chapters_per_run: 20,
          })
          status.value = r
        } catch (e) {
          // 推进失败不中断轮询，下次再试
        } finally {
          isAdvancing = false
        }
      }
      pollStatus()
    } else {
      // 完成后刷新结构化数据
      await loadStructuredData()
    }
  }, 3000)
}

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
      </div>

      <div v-if="status && status.total_chapters" class="progress-bar">
        <div class="progress-fill" :style="{ width: progressPercent + '%' }"></div>
        <span class="progress-text">{{ progressPercent }}%</span>
      </div>

      <div v-if="status?.error_message" class="error-msg">{{ status.error_message }}</div>
      <div v-if="splitResult" class="split-info">已切分 {{ splitResult.chapter_count }} 章</div>

      <div v-if="isMock" class="mock-warning">
        当前为测试模型，结构化抽取结果为模拟数据，不代表真实小说抽取结果。请在设置中配置真实模型后再进行正式抽取。
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
        <button class="btn-sm" :disabled="actionLoading === 'extract'" @click="startExtraction">
          {{ actionLoading === 'extract' ? '启动中...' : (status?.status === 'BATCH_DONE' ? '继续抽取下一批' : '开始抽取') }}
        </button>
        <button class="btn-sm" v-if="canAdvance && status?.status !== 'BATCH_DONE'" :disabled="actionLoading === 'advance'" @click="advanceExtraction">
          {{ actionLoading === 'advance' ? '推进中...' : '继续推进' }}
        </button>
        <button class="btn-sm btn-danger" v-if="status && status.status !== 'NONE'" :disabled="actionLoading === 'reset'" @click="resetExtraction">
          {{ actionLoading === 'reset' ? '重置中...' : '重置抽取' }}
        </button>
      </div>

      <div v-if="status?.status === 'BATCH_DONE' && hasMoreChapters" class="batch-hint">
        已处理 {{ status.extracted_count }} / {{ status.total_chapters }} 章。本批已完成，可继续抽取下一批。
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
                <button class="btn-icon" title="编辑" @click="openEdit(c)">✏️</button>
                <button class="btn-icon" title="删除" @click="deleteRecord(c)">🗑️</button>
              </span>
            </div>
            <div v-if="c.identity_desc" class="item-field">身份：{{ c.identity_desc }}</div>
            <div v-if="c.status_desc" class="item-field">状态：{{ c.status_desc }}</div>
            <div v-if="c.aliases?.length" class="item-field">别名：{{ c.aliases.join('、') }}</div>
            <div v-if="c.evidence?.length" class="item-evidence">证据：{{ c.evidence[0] }}</div>
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
            <div v-if="a.evidence?.length" class="item-evidence">证据：{{ a.evidence[0] }}</div>
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
            <div v-if="e.evidence?.length" class="item-evidence">证据：{{ e.evidence[0] }}</div>
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
            <div v-if="w.evidence?.length" class="item-evidence">证据：{{ w.evidence[0] }}</div>
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
            <span v-if="c.chapter_no" class="citation-chapter">第{{ c.chapter_no }}章</span>
            <span class="citation-snippet">{{ c.snippet }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑/新增弹窗 -->
    <div v-if="editing" class="edit-modal-overlay" @click.self="editing = false">
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
</style>
