<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useProjectStore, useUiStore, friendlyError } from '../stores'
import { useRouter } from 'vue-router'
import type { ProjectMode } from '../api/types'

const store = useProjectStore()
const ui = useUiStore()
const router = useRouter()

onMounted(() => { store.loadProjects() })

function handleLogout() {
  localStorage.removeItem('ai_write_logged_in')
  localStorage.removeItem('ai_write_token')
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
const newGenre = ref('')
const newStyle = ref('')
const newDescription = ref('')
const newTargetWords = ref<number | null>(null)
const newProjectError = ref('')

function openNewProjectForm() {
  newTitle.value = ''
  newMode.value = 'novel'
  newGenre.value = ''
  newStyle.value = ''
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
    genre: newGenre.value.trim() || undefined,
    style: newStyle.value.trim() || undefined,
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
</script>

<template>
  <div class="project-list-page">
    <header class="page-header">
      <h1>AI 小说创作平台</h1>
      <div class="header-actions">
        <router-link to="/settings" class="btn-settings">设置</router-link>
        <button class="btn-primary" @click="openNewProjectForm">+ 新建项目</button>
        <button class="btn-logout" @click="handleLogout">退出登录</button>
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
          <button :class="{ active: newMode === 'novel' }" @click="newMode = 'novel'">小说</button>
          <button :class="{ active: newMode === 'article' }" @click="newMode = 'article'">文章</button>
        </div>
      </div>
      <div class="form-row">
        <label>题材</label>
        <input v-model="newGenre" type="text" placeholder="可选，如：科幻、悬疑" class="form-input" />
      </div>
      <div class="form-row">
        <label>风格</label>
        <input v-model="newStyle" type="text" placeholder="可选，如：硬核、暗黑" class="form-input" />
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

    <div class="project-grid">
      <div
        v-for="project in store.projects"
        :key="project.id"
        class="project-card"
        @click="router.push(`/projects/${project.id}`)"
      >
        <div class="card-header">
          <h3 class="card-title">{{ project.title }}</h3>
          <span class="status-badge" :class="project.status">{{ statusLabel(project.status) }}</span>
        </div>
        <div class="card-meta">
          <span class="mode-badge" :class="project.mode">{{ modeLabel(project.mode) }}</span>
          <span class="meta-sep">·</span>
          <span class="meta-genre">{{ project.genre }}</span>
          <span class="meta-sep">·</span>
          <span class="meta-style">{{ project.style }}</span>
        </div>
        <div class="card-footer">
          <span class="meta-time">更新于 {{ formatDate(project.updated_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.project-list-page {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--sp-8) var(--sp-6);
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-6);
}
.page-header h1 {
  font-size: var(--text-xl);
  font-weight: 700;
  color: var(--text);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.btn-logout {
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  transition: background var(--transition), border-color var(--transition);
}
.btn-logout:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
}
.btn-settings {
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  text-decoration: none;
  transition: background var(--transition), border-color var(--transition);
}
.btn-settings:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
  color: var(--accent);
}
.btn-primary {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background var(--transition);
}
.btn-primary:hover { background: var(--accent-hover); }

/* New project form */
.new-project-form {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-5);
  margin-bottom: var(--sp-6);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.new-project-form h3 {
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text);
  margin: 0;
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

/* Mode segmented buttons */
.mode-segmented {
  display: flex;
  gap: 0;
}
.mode-segmented button {
  padding: var(--sp-2) var(--sp-4);
  border: 1px solid var(--border);
  background: var(--bg);
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), border-color var(--transition);
}
.mode-segmented button:first-child {
  border-radius: var(--radius) 0 0 var(--radius);
}
.mode-segmented button:last-child {
  border-radius: 0 var(--radius) var(--radius) 0;
  border-left: none;
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
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--sp-4);
}
.project-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-5);
  cursor: pointer;
  transition: box-shadow var(--transition), border-color var(--transition);
}
.project-card:hover {
  border-color: var(--border-focus);
  box-shadow: var(--shadow);
}
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.card-title {
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text);
  margin: 0;
  line-height: 1.3;
}
.status-badge {
  flex-shrink: 0;
  font-size: var(--text-xs);
  font-weight: 500;
  padding: 2px 8px;
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
.card-meta {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-3);
  display: flex;
  align-items: center;
  gap: 0;
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
.meta-sep { margin: 0 var(--sp-1); color: var(--text-tertiary); }
.card-footer {
  border-top: 1px solid var(--border);
  padding-top: var(--sp-2);
}
.meta-time {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
</style>