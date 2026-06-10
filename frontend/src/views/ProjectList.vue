<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useProjectStore, useUiStore, friendlyError } from '../stores'
import { useRouter } from 'vue-router'
import type { ProjectMode } from '../api/types'
import { api } from '../api/client'
import { clearAuthSession } from '../utils/authSession'
import ConfirmModal from '../components/ConfirmModal.vue'
import BaseMultiSelect from '../components/BaseMultiSelect.vue'

const store = useProjectStore()
const ui = useUiStore()
const router = useRouter()

onMounted(() => { store.loadProjects() })

function handleLogout() {
  clearAuthSession()
  router.push('/login')
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return '今天'
  if (diffDays === 1) return '昨天'
  if (diffDays < 7) return `${diffDays} 天前`
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

function statusLabel(status: string): string {
  const map: Record<string, string> = { draft: '草稿', writing: '写作中', completed: '已完成', active: '进行中' }
  return map[status] ?? status
}

function modeLabel(mode: string): string {
  return mode === 'article' ? '文章' : '小说'
}

// New project form
const showNewProject = ref(false)
const newTitle = ref('')
const newMode = ref<ProjectMode>('novel')
const newGenres = ref<string[]>([])
const newStyles = ref<string[]>([])
const newDescription = ref('')
const newTargetWords = ref<number | null>(null)
const newProjectError = ref('')

const novelGenreOptions = [
  '玄幻', '仙侠', '奇幻', '都市', '科幻', '悬疑', '历史', '武侠',
  '言情', '穿越重生', '末世', '无限流', '游戏竞技', '轻小说',
  '灵异', '现实', '宫斗宅斗', '职场商战',
].map(label => ({ value: label, label }))

const articleGenreOptions = [
  '产品介绍', '行业分析', '教程指南', '品牌故事', '案例复盘', '观点评论',
  '知识科普', '营销文案', '社媒短文', '新闻解读', '人物访谈', '活动策划',
].map(label => ({ value: label, label }))

const novelStyleOptions = [
  '爽文', '热血', '轻松', '正剧', '甜文', '暗黑', '硬核', '悬疑感',
  '群像', '慢热', '快节奏', '幽默', '治愈', '史诗', '赛博朋克', '国风',
].map(label => ({ value: label, label }))

const articleStyleOptions = [
  '专业', '轻松', '犀利', '克制', '故事化', '数据驱动', '口语化', '高级感',
  '转化导向', '深度分析', '短平快', '社媒感', '科普向', '观点鲜明',
].map(label => ({ value: label, label }))

const genreOptions = computed(() => newMode.value === 'article' ? articleGenreOptions : novelGenreOptions)
const styleOptions = computed(() => newMode.value === 'article' ? articleStyleOptions : novelStyleOptions)

function setNewProjectMode(mode: ProjectMode) {
  if (newMode.value === mode) return
  newMode.value = mode
  newGenres.value = []
  newStyles.value = []
}

const importFileInput = ref<HTMLInputElement | null>(null)
const showTxtImport = ref(false)
const txtImportFile = ref<File | null>(null)
const txtImportTitle = ref('')
const txtImportDescription = ref('')
const txtImportTargetWords = ref<number | null>(null)
const txtImportError = ref('')
const importingTxt = ref(false)
const deletingProjectId = ref<string | null>(null)
const projectPendingDelete = ref<{ id: string; title: string } | null>(null)

function openNewProjectForm() {
  newTitle.value = ''
  newMode.value = 'novel'
  newGenres.value = []
  newStyles.value = []
  newDescription.value = ''
  newTargetWords.value = null
  newProjectError.value = ''
  showNewProject.value = true
}

async function submitNewProject() {
  if (!newTitle.value.trim()) {
    newProjectError.value = '项目标题不能为空'
    return
  }
  const payload = {
    title: newTitle.value.trim(),
    mode: newMode.value,
    genre: newGenres.value.join('、') || undefined,
    style: newStyles.value.join('、') || undefined,
    description: newDescription.value.trim() || undefined,
    target_words: newTargetWords.value ?? undefined,
  }
  try {
    const p = await store.createProject(payload)
    showNewProject.value = false
    ui.showToast('项目创建成功', 'success')
    router.push(`/projects/${p.id}`)
  } catch (e: unknown) {
    const msg = friendlyError(e, '创建项目失败')
    newProjectError.value = msg
    ui.showToast(msg, 'error')
  }
}

function openTxtImportPicker() {
  txtImportError.value = ''
  importFileInput.value?.click()
}

function onTxtFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  input.value = ''
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.txt')) {
    ui.showToast('请选择 TXT 文件', 'error')
    return
  }
  txtImportFile.value = file
  txtImportTitle.value = file.name.replace(/\.txt$/i, '').trim() || '导入小说'
  txtImportDescription.value = ''
  txtImportTargetWords.value = null
  txtImportError.value = ''
  showNewProject.value = false
  showTxtImport.value = true
}

async function submitTxtImport() {
  if (!txtImportFile.value) {
    txtImportError.value = '请选择 TXT 文件'
    return
  }
  if (!txtImportTitle.value.trim()) {
    txtImportError.value = '项目标题不能为空'
    return
  }
  importingTxt.value = true
  txtImportError.value = ''
  try {
    const result = await api.importTxtProject({
      file: txtImportFile.value,
      title: txtImportTitle.value.trim(),
      description: txtImportDescription.value.trim() || undefined,
      target_words: txtImportTargetWords.value ?? undefined,
    })
    showTxtImport.value = false
    await store.loadProjects()
    ui.showToast(`导入成功：${result.import_meta.chapter_count} 个章节`, 'success')
    router.push(`/projects/${result.project.id}`)
  } catch (e: unknown) {
    const msg = friendlyError(e, '导入 TXT 失败')
    txtImportError.value = msg
    ui.showToast(msg, 'error')
  } finally {
    importingTxt.value = false
  }
}

function requestDeleteProject(projectId: string, title: string) {
  if (deletingProjectId.value) return
  projectPendingDelete.value = { id: projectId, title }
}

async function confirmDeleteProject() {
  const target = projectPendingDelete.value
  if (!target || deletingProjectId.value) return
  projectPendingDelete.value = null
  await deleteProject(target.id)
}

async function deleteProject(projectId: string) {
  if (deletingProjectId.value) return

  deletingProjectId.value = projectId
  try {
    await store.deleteProjectRemote(projectId)
    ui.showToast('项目已删除', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '删除项目失败'), 'error')
  } finally {
    deletingProjectId.value = null
  }
}
</script>

<template>
  <div class="project-list-page">
    <header class="page-header">
      <div class="page-heading">
        <span class="page-kicker">Creative Workspace</span>
        <h1>AI 创作平台</h1>
      </div>
      <div class="header-actions">
        <router-link to="/settings" class="btn-settings">设置</router-link>
        <button class="btn-secondary-action" @click="openTxtImportPicker">导入 TXT</button>
        <button class="btn-primary" @click="openNewProjectForm">+ 新建项目</button>
        <button class="btn-logout" @click="handleLogout">退出登录</button>
        <input ref="importFileInput" class="hidden-file-input" type="file" accept=".txt,text/plain" @change="onTxtFileSelected" />
      </div>
    </header>

    <!-- New project form -->
    <div v-if="showNewProject" class="new-project-form">
      <h3>新建项目</h3>
      <div class="form-row">
        <label>项目标题 <span class="required">*</span></label>
        <input v-model="newTitle" type="text" placeholder="输入项目标题" class="form-input" />
      </div>
      <div class="form-row">
        <label>创作模式</label>
        <div class="mode-segmented">
          <button :class="{ active: newMode === 'novel' }" @click="setNewProjectMode('novel')">小说</button>
          <button :class="{ active: newMode === 'article' }" @click="setNewProjectMode('article')">文章</button>
        </div>
      </div>
      <div class="form-row">
        <label>题材</label>
        <BaseMultiSelect
          v-model="newGenres"
          :options="genreOptions"
          :placeholder="newMode === 'article' ? '可选，如：行业分析、教程指南' : '可选，如：科幻、悬疑'"
        />
      </div>
      <div class="form-row">
        <label>风格</label>
        <BaseMultiSelect
          v-model="newStyles"
          :options="styleOptions"
          :placeholder="newMode === 'article' ? '可选，如：专业、故事化' : '可选，如：硬核、暗黑'"
        />
      </div>
      <div class="form-row">
        <label>简介</label>
        <textarea v-model="newDescription" placeholder="可选" class="form-textarea" rows="2"></textarea>
      </div>
      <div class="form-row">
        <label>目标字数</label>
        <input v-model.number="newTargetWords" type="number" placeholder="可选" class="form-input form-input-sm" />
      </div>
      <div v-if="newProjectError" class="form-error">{{ newProjectError }}</div>
      <div class="form-actions">
        <button class="btn-submit" @click="submitNewProject">创建</button>
        <button class="btn-cancel" @click="showNewProject = false">取消</button>
      </div>
    </div>

    <div v-if="showTxtImport" class="new-project-form">
      <h3>导入 TXT 小说</h3>
      <div class="import-file-row">
        <span class="import-file-name">{{ txtImportFile?.name }}</span>
        <button class="btn-cancel" :disabled="importingTxt" @click="openTxtImportPicker">重新选择</button>
      </div>
      <div class="form-row">
        <label>项目标题 <span class="required">*</span></label>
        <input v-model="txtImportTitle" type="text" placeholder="导入后显示的小说标题" class="form-input" />
      </div>
      <div class="form-row">
        <label>简介</label>
        <textarea v-model="txtImportDescription" placeholder="可选" class="form-textarea" rows="2"></textarea>
      </div>
      <div class="form-row">
        <label>目标字数</label>
        <input v-model.number="txtImportTargetWords" type="number" placeholder="可选" class="form-input form-input-sm" />
      </div>
      <div v-if="txtImportError" class="form-error">{{ txtImportError }}</div>
      <div class="form-actions">
        <button class="btn-submit" :disabled="importingTxt" @click="submitTxtImport">{{ importingTxt ? '导入中...' : '导入为小说项目' }}</button>
        <button class="btn-cancel" :disabled="importingTxt" @click="showTxtImport = false">取消</button>
      </div>
    </div>

    <div class="project-grid">
      <div
        v-for="project in store.projects"
        :key="project.id"
        class="project-card"
        @click="router.push(`/projects/${project.id}`)"
      >
        <div class="card-header">
          <h3 class="card-title">{{ project.title }}</h3>
          <div class="card-actions" @click.stop>
            <span class="status-badge" :class="project.status">{{ statusLabel(project.status) }}</span>
            <button
              class="btn-card-secondary"
              title="评测集"
              @click.stop="router.push(`/projects/${project.id}/evaluations`)"
            >
              评测集
            </button>
            <button
              class="btn-card-delete"
              :disabled="deletingProjectId === project.id"
              title="删除项目"
              @click.stop="requestDeleteProject(project.id, project.title)"
            >
              {{ deletingProjectId === project.id ? '删除中' : '删除' }}
            </button>
          </div>
        </div>
        <div class="card-meta">
          <span class="mode-badge" :class="project.mode">{{ modeLabel(project.mode) }}</span>
          <template v-if="project.genre">
            <span class="meta-sep">·</span>
            <span class="meta-genre">{{ project.genre }}</span>
          </template>
          <template v-if="project.style">
            <span class="meta-sep">·</span>
            <span class="meta-style">{{ project.style }}</span>
          </template>
        </div>
        <div class="card-footer">
          <span class="meta-time">更新于 {{ formatDate(project.updated_at) }}</span>
        </div>
      </div>
    </div>

    <ConfirmModal
      v-if="projectPendingDelete"
      :message="`确定删除项目「${projectPendingDelete.title}」吗？此操作会删除项目下的章节、角色、大纲、世界观和生成历史，且无法恢复。`"
      confirm-text="删除项目"
      danger
      @confirm="confirmDeleteProject"
      @cancel="projectPendingDelete = null"
    />
  </div>
</template>

<style scoped>
.project-list-page {
  --project-page-bg:
    linear-gradient(180deg, rgba(21, 28, 38, 0.96) 0%, rgba(13, 17, 23, 0.98) 48%, rgba(18, 21, 26, 1) 100%),
    radial-gradient(circle at 18% 0%, rgba(59, 157, 255, 0.16), transparent 32%),
    radial-gradient(circle at 78% 8%, rgba(45, 212, 191, 0.1), transparent 30%);
  --project-panel-bg: linear-gradient(180deg, rgba(28, 33, 41, 0.98), rgba(22, 27, 34, 0.98));
  --project-panel-strong-bg: linear-gradient(180deg, rgba(27, 32, 40, 0.96), rgba(20, 25, 32, 0.96));
  --project-button-bg: rgba(30, 35, 43, 0.84);
  --project-border: rgba(148, 163, 184, 0.18);
  --project-inner-bg: rgba(13, 17, 23, 0.42);
  --project-shadow: 0 18px 44px rgba(0, 0, 0, 0.26), 0 1px 0 rgba(255, 255, 255, 0.04) inset;
  --bg-panel: #181a1f;
  --bg-hover: #24272e;
  --bg-input: #12151b;
  --border: #2b3038;
  --text: #f3f6fb;
  --text-secondary: #b7beca;
  --text-tertiary: #737d8c;
  --text-inverse: #06111f;
  --status-draft: #a8a29e;
  --status-draft-bg: rgba(168, 162, 158, 0.14);
  --status-reviewing: #fbbf24;
  --status-reviewing-bg: rgba(251, 191, 36, 0.14);
  --status-final: #34d399;
  --status-final-bg: rgba(52, 211, 153, 0.14);
  --status-error: #f87171;
  --accent-subtle: rgba(59, 157, 255, 0.14);
  height: calc(100vh - var(--desktop-status-bar-height, 0px));
  overflow-y: auto;
  max-width: none;
  margin: 0;
  padding: 46px max(var(--sp-8), calc((100vw - 1520px) / 2 + var(--sp-8))) 72px;
  background: var(--project-page-bg);
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--accent) 40%, var(--border)) transparent;
}
:global(:root:not(.desktop-runtime)[data-theme="light"]) .project-list-page {
  --project-page-bg:
    linear-gradient(180deg, #f7f9fd 0%, #eef3f9 52%, #f4f6fb 100%),
    radial-gradient(circle at 18% 0%, rgba(49, 89, 217, 0.1), transparent 32%),
    radial-gradient(circle at 78% 8%, rgba(5, 150, 105, 0.08), transparent 30%);
  --project-panel-bg: rgba(255, 255, 255, 0.92);
  --project-panel-strong-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(248, 251, 255, 0.96));
  --project-button-bg: rgba(255, 255, 255, 0.86);
  --project-border: rgba(120, 136, 160, 0.28);
  --project-inner-bg: rgba(238, 242, 247, 0.7);
  --project-shadow: 0 18px 44px rgba(17, 24, 39, 0.08), 0 1px 0 rgba(255, 255, 255, 0.9) inset;
  --bg-panel: #ffffff;
  --bg-hover: #edf3ff;
  --bg-input: #ffffff;
  --border: #d8e0ec;
  --text: #111827;
  --text-secondary: #5d6675;
  --text-tertiary: #96a0ae;
  --text-inverse: #ffffff;
  --status-draft: #6b7280;
  --status-draft-bg: #f3f4f6;
  --status-reviewing: #d97706;
  --status-reviewing-bg: #fffbeb;
  --status-final: #059669;
  --status-final-bg: #ecfdf5;
  --status-error: #dc2626;
  --accent-subtle: #edf3ff;
}
.project-list-page::-webkit-scrollbar {
  width: 8px;
}
.project-list-page::-webkit-scrollbar-track {
  background: transparent;
}
.project-list-page::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--accent) 38%, var(--border));
  border-radius: 999px;
}
.page-header,
.new-project-form,
.project-grid,
.empty-state,
.loading-state {
  max-width: 1520px;
  margin-left: auto;
  margin-right: auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-5);
  margin-bottom: var(--sp-8);
}
.page-heading {
  min-width: 0;
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
.page-header h1 {
  margin: 0;
  font-size: 1.85rem;
  font-weight: 760;
  letter-spacing: 0;
  color: var(--text);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.btn-logout {
  padding: var(--sp-2) var(--sp-3);
  background: var(--project-button-bg);
  border: 1px solid var(--project-border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  box-shadow: var(--shadow-sm);
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.btn-logout:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--text);
}
.btn-settings {
  padding: var(--sp-2) var(--sp-3);
  background: var(--project-button-bg);
  border: 1px solid var(--project-border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  text-decoration: none;
  box-shadow: var(--shadow-sm);
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.btn-settings:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--accent);
}
.btn-secondary-action {
  padding: var(--sp-2) var(--sp-3);
  background: var(--project-button-bg);
  border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
  border-radius: var(--radius);
  font-size: var(--text-xs);
  font-weight: 650;
  color: var(--accent);
  box-shadow: var(--shadow-sm);
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.btn-secondary-action:hover {
  background: var(--accent-subtle);
  border-color: var(--accent);
}
.hidden-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}
.btn-primary {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 650;
  box-shadow: 0 10px 24px color-mix(in srgb, var(--accent) 24%, transparent);
  transition: background var(--transition), transform var(--transition), box-shadow var(--transition);
}
.btn-primary:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
  box-shadow: 0 14px 32px color-mix(in srgb, var(--accent) 28%, transparent);
}

/* New project form */
.new-project-form {
  background: var(--project-panel-strong-bg);
  border: 1px solid var(--project-border);
  border-radius: var(--radius-lg);
  padding: var(--sp-6);
  margin-bottom: var(--sp-8);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  box-shadow: var(--project-shadow);
}
.new-project-form h3 {
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text);
  margin: 0;
}
.import-file-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--project-border);
  border-radius: var(--radius);
  background: var(--project-inner-bg);
}
.import-file-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 650;
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
.form-input-sm { width: 120px; }
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
  transition: background var(--transition), transform var(--transition);
}
.btn-submit:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
.btn-cancel {
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.btn-cancel:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--text);
}

/* Mode segmented buttons */
.mode-segmented {
  display: flex;
  gap: 0;
  width: fit-content;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-sidebar);
}
.mode-segmented button {
  padding: var(--sp-2) var(--sp-4);
  border: 1px solid transparent;
  background: transparent;
  font-size: var(--text-sm);
  font-weight: 650;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), border-color var(--transition);
}
.mode-segmented button:first-child {
  border-radius: var(--radius-sm);
}
.mode-segmented button:last-child {
  border-radius: var(--radius-sm);
}
.mode-segmented button.active {
  background: var(--accent);
  color: var(--text-inverse);
  border-color: var(--accent);
}
.mode-segmented button.active + button {
  border-left-color: var(--accent);
}

.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--sp-5);
}
.project-card {
  position: relative;
  overflow: hidden;
  background: var(--project-panel-bg);
  border: 1px solid var(--project-border);
  border-radius: var(--radius-lg);
  padding: var(--sp-6);
  cursor: pointer;
  box-shadow: var(--project-shadow);
  transition: box-shadow var(--transition), border-color var(--transition), transform var(--transition);
}
.project-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 3px;
  background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 40%, var(--status-final)));
  opacity: 0.75;
}
.project-card:hover {
  border-color: var(--border-focus);
  box-shadow: 0 22px 54px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(59, 157, 255, 0.1) inset;
  transform: translateY(-2px);
}
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.card-actions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.card-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  line-height: 1.3;
}
.status-badge {
  flex-shrink: 0;
  font-size: var(--text-xs);
  font-weight: 650;
  padding: 3px 9px;
  border-radius: 10px;
  white-space: nowrap;
}
.status-badge.draft, .status-badge.active {
  background: var(--status-draft-bg);
  color: var(--status-draft);
}
.status-badge.writing {
  background: var(--status-reviewing-bg);
  color: var(--status-reviewing);
}
.status-badge.completed {
  background: var(--status-final-bg);
  color: var(--status-final);
}
.btn-card-delete {
  padding: 3px 8px;
  border: 1px solid color-mix(in srgb, var(--status-error, #ef4444) 36%, var(--border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--status-error-bg, #fef2f2) 72%, transparent);
  color: var(--status-error, #ef4444);
  font-size: var(--text-xs);
  font-weight: 650;
  transition: opacity var(--transition), background var(--transition), border-color var(--transition);
}
.btn-card-secondary {
  padding: 3px 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--border));
  border-radius: 9px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 650;
  transition: background var(--transition), border-color var(--transition);
}
.btn-card-secondary:hover {
  background: color-mix(in srgb, var(--accent-subtle) 70%, var(--bg-panel));
  border-color: color-mix(in srgb, var(--accent) 64%, var(--border));
}
.btn-card-delete:hover:not(:disabled) {
  background: color-mix(in srgb, var(--status-error, #ef4444) 12%, var(--bg-panel));
  border-color: color-mix(in srgb, var(--status-error, #ef4444) 64%, var(--border));
}
.btn-card-delete:disabled {
  cursor: not-allowed;
  opacity: 0.65;
}
.card-meta {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-5);
  display: flex;
  align-items: center;
  gap: 0;
  min-height: 24px;
  flex-wrap: wrap;
}
.mode-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 3px 8px;
  border-radius: 10px;
  white-space: nowrap;
}
.mode-badge.novel { background: var(--status-final-bg); color: var(--status-final); }
.mode-badge.article { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.meta-sep { margin: 0 var(--sp-1); color: var(--text-tertiary); }
.card-footer {
  border-top: 1px solid var(--project-border);
  padding-top: var(--sp-3);
}
.meta-time {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}

@media (max-width: 720px) {
  .project-list-page {
    padding: var(--sp-6) var(--sp-4) var(--sp-8);
  }
  .page-header {
    align-items: flex-start;
    flex-direction: column;
  }
  .header-actions {
    width: 100%;
    flex-wrap: wrap;
  }
}
</style>
