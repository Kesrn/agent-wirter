<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import type {
  ApiEvaluationCase,
  ApiEvaluationDataset,
  ApiEvaluationRun,
  ApiProject,
  EvaluationGenerationMode,
} from '../api/types'
import { friendlyError, useUiStore } from '../stores'
import ConfirmModal from '../components/ConfirmModal.vue'
import BaseSelect from '../components/BaseSelect.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const projectId = computed(() => route.params.id as string)
const project = ref<ApiProject | null>(null)
const datasets = ref<ApiEvaluationDataset[]>([])
const cases = ref<ApiEvaluationCase[]>([])
const runs = ref<ApiEvaluationRun[]>([])
const selectedDatasetId = ref('')
const selectedRun = ref<ApiEvaluationRun | null>(null)
const loading = ref(false)
const running = ref(false)

const showDatasetForm = ref(false)
const datasetName = ref('')
const datasetDescription = ref('')
const datasetMode = ref<'creative' | 'regression' | 'prompt' | 'model'>('creative')

const showCaseForm = ref(false)
const caseName = ref('')
const caseTaskType = ref('creative_generation')
const caseInput = ref('')
const caseActualOutput = ref('')
const caseReferenceOutput = ref('')
const caseExpected = ref('')
const caseRubric = ref('')
const caseTags = ref('')
const editingCaseId = ref<string | null>(null)

const runName = ref('')
const runMode = ref<EvaluationGenerationMode>('generate_and_judge')
const pendingDelete = ref<{ type: 'dataset' | 'case'; id: string; name: string } | null>(null)

const datasetModeOptions = [
  { value: 'creative', label: '创作' },
  { value: 'regression', label: '回归' },
  { value: 'prompt', label: 'Prompt' },
  { value: 'model', label: '模型' },
]

const runModeOptions = [
  { value: 'generate_and_judge', label: '生成并评分' },
  { value: 'judge_only', label: '仅评分' },
]

const selectedDataset = computed(() => datasets.value.find(item => item.id === selectedDatasetId.value) ?? null)
const activeCases = computed(() => cases.value.filter(item => item.status === 'active'))
const sortedRuns = computed(() => [...runs.value].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()))

const ARTICLE_RUBRIC: Record<string, string> = {
  requirement_following: '是否遵守 brief、平台、受众和内容目标',
  structure_quality: '标题、开头、主体、收束和行动引导是否清晰',
  audience_match: '语言和论证是否匹配目标受众',
  risk_control: '是否避免事实、合规、夸大或版权风险',
}

const NOVEL_RUBRIC: Record<string, string> = {
  requirement_following: '是否遵守输入里的硬性要求、角色、世界观和章节目标',
  context_consistency: '是否不违背已有设定，是否避免引入无依据的新关键设定',
  plot_progress: '是否推动剧情或信息增量，而不是空转',
  prose_quality: '中文表达、节奏、画面感和可读性',
}

const defaultRubric = computed<Record<string, string>>(() => {
  if (project.value?.mode === 'article') {
    return ARTICLE_RUBRIC
  }
  return NOVEL_RUBRIC
})

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function modeLabel(mode: string): string {
  const map: Record<string, string> = {
    creative: '创作',
    regression: '回归',
    prompt: 'Prompt',
    model: '模型',
    generate_and_judge: '生成并评分',
    judge_only: '仅评分',
  }
  return map[mode] ?? mode
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    active: '启用',
    archived: '归档',
    disabled: '停用',
    running: '运行中',
    completed: '完成',
    partial: '部分完成',
    failed: '失败',
  }
  return map[status] ?? status
}

function resetDatasetForm() {
  datasetName.value = ''
  datasetDescription.value = ''
  datasetMode.value = 'creative'
}

function resetCaseForm() {
  editingCaseId.value = null
  caseName.value = ''
  caseTaskType.value = project.value?.mode === 'article' ? 'article_generation' : 'novel_chapter'
  caseInput.value = ''
  caseActualOutput.value = ''
  caseReferenceOutput.value = ''
  caseExpected.value = ''
  caseRubric.value = JSON.stringify(defaultRubric.value, null, 2)
  caseTags.value = ''
}

function lines(text: string): string[] {
  return text.split('\n').map(line => line.trim()).filter(Boolean)
}

function parseRubric(): Record<string, string> {
  const raw = caseRubric.value.trim()
  if (!raw) return defaultRubric.value
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]))
    }
  } catch {
    // fallback below
  }
  return Object.fromEntries(lines(raw).map((line, index) => {
    const [key, ...rest] = line.split(':')
    return [(key || `criterion_${index + 1}`).trim(), (rest.join(':') || line).trim()]
  }))
}

async function loadDatasets() {
  datasets.value = await api.listEvaluationDatasets(projectId.value)
  if (!selectedDatasetId.value && datasets.value.length) {
    selectedDatasetId.value = datasets.value[0].id
  }
}

async function loadDatasetDetail() {
  if (!selectedDatasetId.value) {
    cases.value = []
    runs.value = []
    selectedRun.value = null
    return
  }
  const [caseList, runList] = await Promise.all([
    api.listEvaluationCases(projectId.value, selectedDatasetId.value),
    api.listEvaluationRuns(projectId.value, selectedDatasetId.value),
  ])
  cases.value = caseList
  runs.value = runList
  selectedRun.value = runList[0] ?? null
  if (selectedRun.value) {
    selectedRun.value = await api.getEvaluationRun(projectId.value, selectedDatasetId.value, selectedRun.value.id)
  }
}

async function loadAll() {
  loading.value = true
  try {
    project.value = await api.getProject(projectId.value)
    await loadDatasets()
    await loadDatasetDetail()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '加载评测集失败'), 'error')
  } finally {
    loading.value = false
  }
}

async function createDataset() {
  if (!datasetName.value.trim()) {
    ui.showToast('评测集名称不能为空', 'error')
    return
  }
  try {
    const created = await api.createEvaluationDataset(projectId.value, {
      name: datasetName.value.trim(),
      description: datasetDescription.value.trim() || null,
      mode: datasetMode.value,
    })
    resetDatasetForm()
    showDatasetForm.value = false
    await loadDatasets()
    selectedDatasetId.value = created.id
    await loadDatasetDetail()
    ui.showToast('评测集已创建', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '创建评测集失败'), 'error')
  }
}

function editCase(item: ApiEvaluationCase) {
  editingCaseId.value = item.id
  caseName.value = item.name
  caseTaskType.value = item.task_type
  caseInput.value = item.input_text
  caseActualOutput.value = item.actual_output ?? ''
  caseReferenceOutput.value = item.reference_output ?? ''
  caseExpected.value = (item.expected_properties ?? []).join('\n')
  caseRubric.value = JSON.stringify(item.rubric ?? defaultRubric.value, null, 2)
  caseTags.value = (item.tags ?? []).join(', ')
  showCaseForm.value = true
}

async function saveCase() {
  if (!selectedDatasetId.value) return
  if (!caseName.value.trim()) {
    ui.showToast('样本名称不能为空', 'error')
    return
  }
  const payload = {
    name: caseName.value.trim(),
    task_type: caseTaskType.value.trim() || 'creative_generation',
    input_text: caseInput.value,
    actual_output: caseActualOutput.value.trim() || null,
    reference_output: caseReferenceOutput.value.trim() || null,
    expected_properties: lines(caseExpected.value),
    rubric: parseRubric(),
    tags: caseTags.value.split(',').map(tag => tag.trim()).filter(Boolean),
  }
  try {
    if (editingCaseId.value) {
      await api.updateEvaluationCase(projectId.value, selectedDatasetId.value, editingCaseId.value, payload)
      ui.showToast('样本已更新', 'success')
    } else {
      await api.createEvaluationCase(projectId.value, selectedDatasetId.value, payload)
      ui.showToast('样本已创建', 'success')
    }
    resetCaseForm()
    showCaseForm.value = false
    await loadDatasets()
    await loadDatasetDetail()
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存样本失败'), 'error')
  }
}

async function runDataset() {
  if (!selectedDatasetId.value || running.value) return
  if (!activeCases.value.length) {
    ui.showToast('当前评测集没有启用样本', 'error')
    return
  }
  running.value = true
  try {
    const run = await api.runEvaluationDataset(projectId.value, selectedDatasetId.value, {
      name: runName.value.trim() || null,
      generation_mode: runMode.value,
    })
    runName.value = ''
    selectedRun.value = run
    await loadDatasets()
    await loadDatasetDetail()
    selectedRun.value = await api.getEvaluationRun(projectId.value, selectedDatasetId.value, run.id)
    ui.showToast('评测运行完成', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '运行评测失败'), 'error')
  } finally {
    running.value = false
  }
}

async function openRun(run: ApiEvaluationRun) {
  if (!selectedDatasetId.value) return
  selectedRun.value = await api.getEvaluationRun(projectId.value, selectedDatasetId.value, run.id)
}

function requestDeleteDataset(item: ApiEvaluationDataset) {
  pendingDelete.value = { type: 'dataset', id: item.id, name: item.name }
}

function requestDeleteCase(item: ApiEvaluationCase) {
  pendingDelete.value = { type: 'case', id: item.id, name: item.name }
}

async function confirmDelete() {
  if (!pendingDelete.value || !selectedDatasetId.value) return
  const target = pendingDelete.value
  pendingDelete.value = null
  try {
    if (target.type === 'dataset') {
      await api.deleteEvaluationDataset(projectId.value, target.id)
      if (selectedDatasetId.value === target.id) selectedDatasetId.value = ''
      await loadDatasets()
      await loadDatasetDetail()
    } else {
      await api.deleteEvaluationCase(projectId.value, selectedDatasetId.value, target.id)
      await loadDatasets()
      await loadDatasetDetail()
    }
    ui.showToast('已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除失败'), 'error')
  }
}

watch(selectedDatasetId, () => {
  loadDatasetDetail()
})

onMounted(() => {
  resetCaseForm()
  loadAll()
})
</script>

<template>
  <div class="eval-page">
    <header class="eval-header">
      <div>
        <span class="page-kicker">Evaluation Lab</span>
        <h1>{{ project?.title ?? '项目' }} · 评测集</h1>
      </div>
      <div class="header-actions">
        <router-link :to="`/projects/${projectId}`" class="btn-secondary">返回工作台</router-link>
        <button class="btn-secondary" @click="router.push('/projects')">项目列表</button>
      </div>
    </header>

    <div v-if="loading" class="loading-hint">加载中...</div>
    <div v-else class="eval-layout">
      <aside class="dataset-pane">
        <div class="pane-head">
          <h2>评测集</h2>
          <button class="icon-action" title="新增评测集" @click="showDatasetForm = !showDatasetForm">+</button>
        </div>

        <div v-if="showDatasetForm" class="form-block">
          <input v-model="datasetName" class="form-input" placeholder="评测集名称" />
          <textarea v-model="datasetDescription" class="form-textarea" rows="2" placeholder="描述"></textarea>
          <BaseSelect v-model="datasetMode" :options="datasetModeOptions" />
          <div class="form-actions">
            <button class="btn-submit" @click="createDataset">保存</button>
            <button class="btn-cancel" @click="showDatasetForm = false">取消</button>
          </div>
        </div>

        <button
          v-for="item in datasets"
          :key="item.id"
          class="dataset-item"
          :class="{ active: item.id === selectedDatasetId }"
          @click="selectedDatasetId = item.id"
        >
          <span class="dataset-title">{{ item.name }}</span>
          <span class="dataset-meta">{{ modeLabel(item.mode) }} · {{ item.case_count }} 样本 · {{ item.run_count }} 运行</span>
        </button>
        <div v-if="!datasets.length" class="empty-hint">暂无评测集</div>
      </aside>

      <main class="case-pane">
        <div class="pane-head">
          <div>
            <h2>{{ selectedDataset?.name ?? '选择评测集' }}</h2>
            <p v-if="selectedDataset?.description" class="pane-desc">{{ selectedDataset.description }}</p>
          </div>
          <div class="head-actions">
            <button v-if="selectedDataset" class="btn-danger-soft" @click="requestDeleteDataset(selectedDataset)">删除评测集</button>
            <button class="btn-primary" :disabled="!selectedDataset" @click="resetCaseForm(); showCaseForm = true">新增样本</button>
          </div>
        </div>

        <div v-if="showCaseForm" class="case-form">
          <div class="form-grid">
            <label>
              样本名称
              <input v-model="caseName" class="form-input" placeholder="例如：章节设定一致性" />
            </label>
            <label>
              任务类型
              <input v-model="caseTaskType" class="form-input" placeholder="novel_chapter / article_generation" />
            </label>
          </div>
          <label>
            输入
            <textarea v-model="caseInput" class="form-textarea mono" rows="5" placeholder="任务输入、上下文、brief、章节大纲等"></textarea>
          </label>
          <label>
            期望属性
            <textarea v-model="caseExpected" class="form-textarea" rows="3" placeholder="每行一个硬性要求"></textarea>
          </label>
          <label>
            候选输出
            <textarea v-model="caseActualOutput" class="form-textarea mono" rows="4" placeholder="仅评分模式需要填写"></textarea>
          </label>
          <label>
            参考输出
            <textarea v-model="caseReferenceOutput" class="form-textarea mono" rows="3" placeholder="可选"></textarea>
          </label>
          <label>
            Rubric JSON
            <textarea v-model="caseRubric" class="form-textarea mono" rows="5"></textarea>
          </label>
          <label>
            标签
            <input v-model="caseTags" class="form-input" placeholder="逗号分隔" />
          </label>
          <div class="form-actions">
            <button class="btn-submit" @click="saveCase">{{ editingCaseId ? '保存修改' : '创建样本' }}</button>
            <button class="btn-cancel" @click="showCaseForm = false; resetCaseForm()">取消</button>
          </div>
        </div>

        <div class="case-list">
          <div v-for="item in cases" :key="item.id" class="case-item">
            <div class="case-main">
              <div class="case-title-row">
                <h3>{{ item.name }}</h3>
                <span class="status-pill" :class="item.status">{{ statusLabel(item.status) }}</span>
              </div>
              <p>{{ item.input_text || '无输入' }}</p>
              <div class="case-tags">
                <span>{{ item.task_type }}</span>
                <span v-for="tag in item.tags ?? []" :key="tag">{{ tag }}</span>
              </div>
            </div>
            <div class="case-actions">
              <button class="btn-secondary" @click="editCase(item)">编辑</button>
              <button class="btn-danger-soft" @click="requestDeleteCase(item)">删除</button>
            </div>
          </div>
          <div v-if="selectedDataset && !cases.length" class="empty-hint">暂无样本</div>
        </div>
      </main>

      <aside class="run-pane">
        <div class="pane-head">
          <h2>运行</h2>
        </div>
        <div class="run-box">
          <input v-model="runName" class="form-input" placeholder="运行名称" />
          <BaseSelect v-model="runMode" :options="runModeOptions" />
          <button class="btn-primary" :disabled="!selectedDataset || running" @click="runDataset">
            {{ running ? '运行中...' : '运行评测' }}
          </button>
        </div>

        <div class="run-list">
          <button
            v-for="run in sortedRuns"
            :key="run.id"
            class="run-item"
            :class="{ active: selectedRun?.id === run.id }"
            @click="openRun(run)"
          >
            <span>{{ run.name }}</span>
            <strong>{{ run.average_score ?? '-' }}</strong>
            <small>{{ statusLabel(run.status) }} · {{ formatDate(run.created_at) }}</small>
          </button>
          <div v-if="!runs.length" class="empty-hint">暂无运行</div>
        </div>

        <div v-if="selectedRun" class="result-panel">
          <div class="score-summary">
            <span>平均分</span>
            <strong>{{ selectedRun.average_score ?? '-' }}</strong>
          </div>
          <p class="run-summary">{{ selectedRun.summary }}</p>
          <div v-for="result in selectedRun.results" :key="result.id" class="result-item">
            <div class="result-top">
              <span class="status-pill" :class="result.passed ? 'active' : 'failed'">{{ result.passed ? '通过' : '未通过' }}</span>
              <strong>{{ result.score ?? '-' }}</strong>
            </div>
            <p v-if="result.feedback">{{ result.feedback }}</p>
            <p v-if="result.error" class="error-text">{{ result.error }}</p>
            <details v-if="result.scores">
              <summary>维度分</summary>
              <div class="score-grid">
                <span v-for="(score, key) in result.scores" :key="key">{{ key }}: {{ score }}</span>
              </div>
            </details>
          </div>
        </div>
      </aside>
    </div>

    <ConfirmModal
      v-if="pendingDelete"
      :message="`确定删除「${pendingDelete.name}」吗？`"
      confirm-text="删除"
      danger
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    />
  </div>
</template>

<style scoped>
.eval-page {
  min-height: 100vh;
  background: var(--bg);
  padding: 32px var(--sp-6) 48px;
}
.eval-header {
  max-width: 1440px;
  margin: 0 auto var(--sp-6);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
}
.page-kicker {
  display: block;
  margin-bottom: var(--sp-1);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.eval-header h1 {
  margin: 0;
  font-size: 1.6rem;
  color: var(--text);
}
.header-actions,
.head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.eval-layout {
  max-width: 1440px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 340px;
  gap: var(--sp-4);
  align-items: start;
}
.dataset-pane,
.case-pane,
.run-pane {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: var(--sp-4);
}
.case-pane {
  min-width: 0;
}
.pane-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.pane-head h2 {
  margin: 0;
  font-size: var(--text-lg);
  color: var(--text);
}
.pane-desc {
  margin: var(--sp-1) 0 0;
  color: var(--text-tertiary);
  font-size: var(--text-sm);
}
.dataset-item,
.run-item {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  padding: var(--sp-3);
  color: var(--text);
  cursor: pointer;
}
.dataset-item:hover,
.run-item:hover {
  background: var(--bg-hover);
}
.dataset-item.active,
.run-item.active {
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
  background: var(--accent-subtle);
}
.dataset-title {
  font-weight: 700;
}
.dataset-meta,
.run-item small {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}
.form-block,
.case-form,
.run-box {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-sidebar);
  margin-bottom: var(--sp-3);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--sp-3);
}
label {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 650;
}
.form-input,
.form-textarea {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-input);
  color: var(--text);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--text-sm);
}
.form-textarea {
  resize: vertical;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
}
.btn-primary,
.btn-submit,
.btn-secondary,
.btn-cancel,
.btn-danger-soft,
.icon-action {
  border-radius: var(--radius);
  border: 1px solid var(--border);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--text-sm);
  font-weight: 650;
  cursor: pointer;
}
.btn-primary,
.btn-submit {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-inverse);
}
.btn-primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.btn-secondary,
.btn-cancel,
.icon-action {
  background: var(--bg-panel);
  color: var(--text-secondary);
}
.btn-danger-soft {
  background: var(--status-error-bg, #fef2f2);
  border-color: color-mix(in srgb, var(--status-error, #ef4444) 28%, var(--border));
  color: var(--status-error, #ef4444);
}
.case-list,
.run-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.case-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
}
.case-title-row,
.result-top {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  justify-content: space-between;
}
.case-title-row h3 {
  margin: 0;
  font-size: var(--text-base);
}
.case-main p,
.result-item p,
.run-summary {
  color: var(--text-secondary);
  font-size: var(--text-sm);
  line-height: 1.6;
  margin: var(--sp-1) 0;
}
.case-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.case-tags span,
.status-pill {
  font-size: 10px;
  font-weight: 700;
  padding: 3px 7px;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-tertiary);
}
.status-pill.active,
.status-pill.completed {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.status-pill.failed {
  background: var(--status-error-bg, #fef2f2);
  color: var(--status-error, #ef4444);
}
.case-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.run-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
}
.run-item small {
  grid-column: 1 / -1;
}
.result-panel {
  margin-top: var(--sp-3);
  border-top: 1px solid var(--border);
  padding-top: var(--sp-3);
}
.score-summary {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--sp-3);
  border-radius: var(--radius);
  background: var(--accent-subtle);
  color: var(--accent);
}
.score-summary strong {
  font-size: 2rem;
}
.result-item {
  margin-top: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.score-grid {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: var(--sp-2);
  color: var(--text-secondary);
  font-size: var(--text-xs);
}
.error-text {
  color: var(--status-error, #ef4444) !important;
}
.empty-hint,
.loading-hint {
  padding: var(--sp-6);
  text-align: center;
  color: var(--text-tertiary);
}
@media (max-width: 1100px) {
  .eval-layout {
    grid-template-columns: 1fr;
  }
}
</style>
