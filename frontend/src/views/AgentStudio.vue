<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useExpertStore, useUiStore, friendlyError } from '../stores'
import AgentCreator from '../components/AgentCreator.vue'

const route = useRoute()
const store = useExpertStore()
const ui = useUiStore()
const projectId = computed(() => route.params.id as string)

const builtInExperts = computed(() => store.experts.filter(e => e.is_builtin))
const customExperts = computed(() => store.experts.filter(e => !e.is_builtin))

onMounted(() => {
  if (projectId.value) {
    store.loadExperts(projectId.value)
  }
})

async function handleToggle(expertId: string) {
  try {
    await store.toggleExpert(projectId.value, expertId)
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '更新失败'), 'error')
  }
}

function roleTypeLabel(rt: string): string {
  const map: Record<string, string> = { writer: '写手', critic: '评论家', editor: '编辑', researcher: '研究者', custom: '自定义' }
  return map[rt] ?? rt
}
function workflowLabel(wp: string): string {
  const map: Record<string, string> = { pre_writer: '写手前', post_writer: '写手后', replace_writer: '替代写手', pre_critic: '评论前', replace_critic: '替代评论', post_critic: '评论后', standalone: '独立' }
  return map[wp] ?? wp
}
</script>

<template>
  <div class="studio-page">
    <header class="studio-header">
      <router-link :to="`/projects/${projectId}`" class="back-link">← 返回工作台</router-link>
      <h2>Agent 配置</h2>
    </header>
    <p class="studio-subtitle">管理内置专家和自定义创作角色</p>

    <section class="expert-section">
      <h3>内置专家</h3>
      <div v-if="store.loading" class="loading-hint">加载中...</div>
      <div v-else-if="store.loadError" class="error-hint">{{ store.loadError }}</div>
      <div v-else class="expert-grid">
        <div v-for="expert in builtInExperts" :key="expert.id" class="expert-card" :class="{ disabled: !expert.is_enabled }">
          <div class="card-accent" :style="{ background: expert.color }"></div>
          <div class="card-body">
            <div class="card-top">
              <h4>{{ expert.name }}</h4>
              <label class="toggle-switch">
                <input type="checkbox" :checked="expert.is_enabled" @change="handleToggle(expert.id)" />
                <span class="toggle-track"></span>
              </label>
            </div>
            <p class="expert-desc">{{ expert.description }}</p>
            <div class="expert-tags">
              <span class="tag">{{ roleTypeLabel(expert.role_type) }}</span>
              <span class="tag">{{ workflowLabel(expert.workflow_position) }}</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="expert-section">
      <h3>自定义 Agent</h3>
      <div v-if="customExperts.length" class="expert-grid">
        <div v-for="expert in customExperts" :key="expert.id" class="expert-card" :class="{ disabled: !expert.is_enabled }">
          <div class="card-accent" :style="{ background: expert.color }"></div>
          <div class="card-body">
            <div class="card-top">
              <h4>{{ expert.name }}</h4>
              <label class="toggle-switch">
                <input type="checkbox" :checked="expert.is_enabled" @change="handleToggle(expert.id)" />
                <span class="toggle-track"></span>
              </label>
            </div>
            <p class="expert-desc">{{ expert.description }}</p>
            <div class="expert-tags">
              <span class="tag">{{ roleTypeLabel(expert.role_type) }}</span>
              <span class="tag">{{ workflowLabel(expert.workflow_position) }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty-hint">尚未创建自定义 Agent</div>
    </section>

    <section class="expert-section">
      <AgentCreator />
    </section>
  </div>
</template>

<style scoped>
.studio-page {
  max-width: 860px;
  margin: 0 auto;
  padding: var(--sp-8) var(--sp-6);
}
.studio-header {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  margin-bottom: var(--sp-6);
}
.studio-header h2 {
  font-size: var(--text-xl);
  font-weight: 700;
}
.studio-subtitle {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0 0 var(--sp-6);
}
.back-link {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.back-link:hover { color: var(--accent); text-decoration: none; }

.expert-section {
  margin-bottom: var(--sp-8);
}
.expert-section h3 {
  font-size: var(--text-base);
  font-weight: 600;
  margin-bottom: var(--sp-4);
  color: var(--text);
}
.expert-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--sp-3);
}
.expert-card {
  display: flex;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: box-shadow var(--transition);
}
.expert-card:hover {
  box-shadow: var(--shadow);
}
.expert-card.disabled {
  opacity: 0.5;
}
.expert-card.disabled .card-accent {
  background: var(--text-tertiary) !important;
}
.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.card-accent {
  width: 4px;
  flex-shrink: 0;
}
.card-body {
  padding: var(--sp-3) var(--sp-4);
  flex: 1;
  min-width: 0;
}
.card-body h4 {
  font-size: var(--text-sm);
  font-weight: 600;
  margin: 0 0 var(--sp-1);
}
.expert-desc {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  margin: 0 0 var(--sp-2);
  line-height: 1.5;
}
.expert-tags {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
}
.tag {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.empty-hint {
  color: var(--text-tertiary);
  font-size: var(--text-sm);
  padding: var(--sp-4);
  text-align: center;
}
.loading-hint {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
.error-hint {
  font-size: var(--text-xs);
  color: var(--status-error);
}

/* Toggle switch */
.toggle-switch {
  position: relative;
  display: inline-block;
  width: 32px;
  height: 18px;
  flex-shrink: 0;
  cursor: pointer;
}
.toggle-switch input {
  opacity: 0;
  width: 0;
  height: 0;
  position: absolute;
}
.toggle-track {
  position: absolute;
  inset: 0;
  background: var(--text-tertiary);
  border-radius: 9px;
  transition: background var(--transition);
}
.toggle-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  background: var(--text-inverse);
  border-radius: 50%;
  transition: transform var(--transition);
}
.toggle-switch input:checked + .toggle-track {
  background: var(--accent);
}
.toggle-switch input:checked + .toggle-track::after {
  transform: translateX(14px);
}
</style>
