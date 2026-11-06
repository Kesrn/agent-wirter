<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useExpertStore, useUiStore, friendlyError } from '../stores'
import { api } from '../api/client'
import AgentCreator from '../components/AgentCreator.vue'

const route = useRoute()
const store = useExpertStore()
const ui = useUiStore()
const projectId = computed(() => route.params.id as string)

// v2 创作链 expert_key 白名单
const CREATIVE_CHAIN_KEYS = new Set([
  'chapter-architect', 'chapter-writer', 'structural-critic',
  'narrative-editor', 'continuity-checker',
])
const MEMORY_KEYS = new Set(['story-recorder'])

const builtInExperts = computed(() => store.experts.filter(e => e.is_builtin))
const customExperts = computed(() => store.experts.filter(e => !e.is_builtin))

// v2 专家分组：创作链 + 剧情记忆
const creativeChainExperts = computed(() =>
  builtInExperts.value.filter(e => e.expert_key && CREATIVE_CHAIN_KEYS.has(e.expert_key))
)
const memoryExperts = computed(() =>
  builtInExperts.value.filter(e => e.expert_key && MEMORY_KEYS.has(e.expert_key))
)
// 旧版已废弃专家（builtin + deprecated）
const deprecatedExperts = computed(() =>
  builtInExperts.value.filter(e => e.deprecated)
)
// 其他 builtin（既非 v2 也非 deprecated，如未来新增）
const otherBuiltinExperts = computed(() =>
  builtInExperts.value.filter(e => !e.deprecated && !e.expert_key)
)

const showDeprecated = ref(false)
const syncing = ref(false)

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

async function handleSyncV2() {
  syncing.value = true
  try {
    const result = await api.syncV2Experts(projectId.value)
    ui.showToast(`已补齐 v2 专家：新增 ${result.created}，标记旧专家 ${result.deprecated_marked}`, 'success')
    await store.loadExperts(projectId.value)
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '同步失败'), 'error')
  } finally {
    syncing.value = false
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
      <button class="sync-btn" :disabled="syncing || store.loading" @click="handleSyncV2">
        {{ syncing ? '同步中...' : '同步 v2 专家' }}
      </button>
    </header>
    <p class="studio-subtitle">管理内置专家和自定义创作角色</p>

    <div v-if="store.loading" class="loading-hint">加载中...</div>
    <div v-else-if="store.loadError" class="error-hint">{{ store.loadError }}</div>
    <template v-else>
      <!-- 创作链专家 -->
      <section v-if="creativeChainExperts.length" class="expert-section">
        <h3>创作链专家</h3>
        <div class="expert-grid">
          <div v-for="expert in creativeChainExperts" :key="expert.id" class="expert-card" :class="{ disabled: !expert.is_enabled }">
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

      <!-- 剧情记忆专家 -->
      <section v-if="memoryExperts.length" class="expert-section">
        <h3>剧情记忆专家</h3>
        <div class="expert-grid">
          <div v-for="expert in memoryExperts" :key="expert.id" class="expert-card" :class="{ disabled: !expert.is_enabled }">
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

      <!-- 其他内置专家（非 v2 非 deprecated） -->
      <section v-if="otherBuiltinExperts.length" class="expert-section">
        <h3>内置专家</h3>
        <div class="expert-grid">
          <div v-for="expert in otherBuiltinExperts" :key="expert.id" class="expert-card" :class="{ disabled: !expert.is_enabled }">
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

      <!-- 旧版专家（已废弃，默认折叠） -->
      <section v-if="deprecatedExperts.length" class="expert-section">
        <div class="deprecated-header" @click="showDeprecated = !showDeprecated">
          <span class="chevron" :class="{ open: showDeprecated }">▾</span>
          <h3>旧版专家（已废弃）</h3>
          <span class="count-badge">{{ deprecatedExperts.length }}</span>
        </div>
        <div v-if="showDeprecated" class="expert-grid deprecated-grid">
          <div v-for="expert in deprecatedExperts" :key="expert.id" class="expert-card deprecated" :class="{ disabled: !expert.is_enabled }">
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

      <!-- 自定义 Agent -->
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
    </template>
  </div>
</template>

<style scoped>
.studio-page {
  height: calc(100vh - var(--desktop-status-bar-height, 0px));
  overflow-y: auto;
  max-width: none;
  margin: 0;
  padding: var(--sp-8) max(var(--sp-6), calc((100vw - 1440px) / 2 + var(--sp-6))) 96px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--accent) 38%, var(--border)) transparent;
}
.studio-page::-webkit-scrollbar {
  width: 8px;
}
.studio-page::-webkit-scrollbar-track {
  background: transparent;
}
.studio-page::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--accent) 34%, var(--border));
  border-radius: 999px;
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

.sync-btn {
  margin-left: auto;
  padding: var(--sp-1) var(--sp-3);
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--accent);
  background: var(--accent-subtle);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition);
}
.sync-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 15%, var(--accent-subtle));
}
.sync-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

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
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--sp-4);
}
.expert-card {
  display: flex;
  min-height: 108px;
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
.expert-card.deprecated {
  opacity: 0.55;
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

/* Deprecated section */
.deprecated-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  cursor: pointer;
  user-select: none;
  margin-bottom: var(--sp-4);
}
.deprecated-header h3 {
  color: var(--text-tertiary);
  margin: 0;
}
.chevron {
  font-size: 10px;
  color: var(--text-tertiary);
  transition: transform var(--transition);
}
.chevron.open { transform: rotate(180deg); }
.count-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-tertiary);
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

@media (max-width: 760px) {
  .studio-page {
    padding: var(--sp-6) var(--sp-4) 88px;
  }

  .studio-header {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--sp-2);
  }

  .sync-btn {
    margin-left: 0;
  }

  .expert-grid {
    grid-template-columns: 1fr;
  }
}
</style>
