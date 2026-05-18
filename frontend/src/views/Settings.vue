<script setup lang="ts">
import { ref, computed, onMounted, watch, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useLLMSettingsStore, useUiStore, friendlyError } from '../stores'
import type { LLMProviderName, LLMConfigCreatePayload } from '../api/types'

const router = useRouter()
const store = useLLMSettingsStore()
const ui = useUiStore()

const formProvider = ref<LLMProviderName>('mock')
const formApiKey = ref('')
const formBaseUrl = ref('')
const formModelId = ref('')
const showApiKey = ref(false)
const useModelDropdown = ref(false)
const modelDropdownOpen = ref(false)
const modelInputMode = ref(false) // true = manual input, false = dropdown

const PROVIDER_DEFAULTS: Record<string, string> = {
  openai: 'https://api.openai.com/v1',
  deepseek: 'https://api.deepseek.com/v1',
  siliconflow: 'https://api.siliconflow.cn/v1',
  zhipu: 'https://open.bigmodel.cn/api/paas/v4',
  moonshot: 'https://api.moonshot.cn/v1',
  qwen: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  yi: 'https://api.lingyiwanwu.com/v1',
  minimax: 'https://api.minimax.chat/v1',
  custom: '',
}

const PROVIDER_LABELS: Record<string, string> = {
  mock: 'Mock（测试）',
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
  siliconflow: 'SiliconFlow',
  zhipu: '智谱 AI',
  moonshot: '月之暗面',
  qwen: '通义千问',
  yi: '零一万物',
  minimax: 'MiniMax',
  custom: '自定义（OpenAI 兼容）',
}

const isMock = computed(() => formProvider.value === 'mock')

onMounted(() => {
  store.loadConfig().then(() => {
    if (store.config) {
      formProvider.value = store.config.provider
      formBaseUrl.value = store.config.base_url ?? PROVIDER_DEFAULTS[store.config.provider] ?? ''
      formModelId.value = store.config.model_id ?? ''
    }
  })
})

watch(formProvider, (newProvider) => {
  formBaseUrl.value = PROVIDER_DEFAULTS[newProvider] ?? ''
  formModelId.value = ''
  useModelDropdown.value = false
  modelDropdownOpen.value = false
  modelInputMode.value = false
  store.availableModels = []
  if (newProvider === 'mock') {
    formApiKey.value = ''
  }
})

async function handleSave() {
  const payload: LLMConfigCreatePayload = {
    provider: formProvider.value,
    base_url: isMock.value ? null : formBaseUrl.value || null,
    model_id: isMock.value ? null : formModelId.value || null,
  }
  if (!isMock.value && formApiKey.value) {
    payload.api_key = formApiKey.value
  }
  try {
    await store.saveConfig(payload)
    formApiKey.value = ''
    ui.showToast('配置已保存', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '保存失败'), 'error')
  }
}

async function handleFetchModels() {
  if (!formApiKey.value) {
    ui.showToast('请先输入 API Key', 'error')
    return
  }
  try {
    await store.fetchAvailableModels({
      provider: formProvider.value,
      api_key: formApiKey.value,
      base_url: formBaseUrl.value || null,
    })
    useModelDropdown.value = store.availableModels.length > 0
    modelInputMode.value = false
    ui.showToast(`获取到 ${store.availableModels.length} 个模型`, 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '获取模型失败'), 'error')
  }
}

async function handleReset() {
  try {
    await store.resetToDefaults()
    formProvider.value = 'mock'
    formApiKey.value = ''
    formBaseUrl.value = ''
    formModelId.value = ''
    useModelDropdown.value = false
    ui.showToast('已重置为默认配置', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '重置失败'), 'error')
  }
}

function selectModel(id: string) {
  formModelId.value = id
  modelDropdownOpen.value = false
}

function switchToManualInput() {
  modelInputMode.value = true
  modelDropdownOpen.value = false
}

function onClickOutsideModel(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.model-dropdown-wrap')) {
    modelDropdownOpen.value = false
  }
}

onMounted(() => document.addEventListener('click', onClickOutsideModel))
onBeforeUnmount(() => document.removeEventListener('click', onClickOutsideModel))

function handleLogout() {
  localStorage.removeItem('ai_write_logged_in')
  localStorage.removeItem('ai_write_token')
  router.push('/login')
}
</script>

<template>
  <div class="settings-page">
    <header class="page-header">
      <h1>LLM 大模型配置</h1>
      <div class="header-actions">
        <router-link to="/projects" class="link-back">← 项目列表</router-link>
        <button class="btn-logout" @click="handleLogout">退出登录</button>
      </div>
    </header>

    <!-- Status bar -->
    <div v-if="store.config" class="status-bar">
      <span class="status-item">
        <span class="status-label">Provider:</span>
        <span class="status-value">{{ PROVIDER_LABELS[store.config.provider] ?? store.config.provider }}</span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-label">Model:</span>
        <span class="status-value">{{ store.config.model_id ?? '未设置' }}</span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-label">API Key:</span>
        <span class="status-value" :class="store.config.api_key_set ? 'key-set' : 'key-unset'">
          {{ store.config.api_key_set ? '已设置' : '未设置' }}
        </span>
      </span>
    </div>

    <div v-if="store.loading && !store.config" class="loading-hint">加载中...</div>
    <div v-else-if="store.error && !store.config" class="error-hint">{{ store.error }}</div>

    <!-- Config form -->
    <div class="config-form">
      <div class="form-row">
        <label>Provider</label>
        <select v-model="formProvider" class="form-input">
          <option value="mock">Mock（测试）</option>
          <option value="openai">OpenAI</option>
          <option value="deepseek">DeepSeek</option>
          <option value="siliconflow">SiliconFlow</option>
          <option value="zhipu">智谱 AI</option>
          <option value="moonshot">月之暗面</option>
          <option value="qwen">通义千问</option>
          <option value="yi">零一万物</option>
          <option value="minimax">MiniMax</option>
          <option value="custom">自定义（OpenAI 兼容）</option>
        </select>
      </div>

      <div class="form-row" :class="{ disabled: isMock }">
        <label>API Key</label>
        <div class="api-key-row">
          <div class="key-status">
            <span v-if="store.config?.api_key_set" class="key-badge key-set">已保存</span>
            <span v-else-if="!isMock" class="key-badge key-unset">未设置</span>
          </div>
          <input
            v-model="formApiKey"
            :type="showApiKey ? 'text' : 'password'"
            :disabled="isMock"
            :placeholder="store.config?.api_key_set ? '输入新密钥以替换' : '输入 API Key'"
            class="form-input"
          />
          <button v-if="!isMock" class="btn-toggle-vis" @click="showApiKey = !showApiKey">
            {{ showApiKey ? '隐藏' : '显示' }}
          </button>
        </div>
      </div>

      <div class="form-row" :class="{ disabled: isMock }">
        <label>Base URL</label>
        <input
          v-model="formBaseUrl"
          :disabled="isMock"
          placeholder="API Base URL"
          class="form-input"
        />
      </div>

      <div class="form-row" :class="{ disabled: isMock }">
        <label>Model</label>
        <div class="model-row">
          <div v-if="useModelDropdown && !modelInputMode" class="model-dropdown-wrap">
            <button
              class="model-dropdown-trigger"
              :disabled="isMock"
              @click.stop="modelDropdownOpen = !modelDropdownOpen"
            >
              <span :class="{ placeholder: !formModelId }">{{ formModelId || '选择模型' }}</span>
              <span class="chevron" :class="{ open: modelDropdownOpen }">▾</span>
            </button>
            <div v-if="modelDropdownOpen" class="model-dropdown-panel">
              <div
                v-for="m in store.availableModels"
                :key="m.id"
                class="model-option"
                :class="{ selected: m.id === formModelId }"
                @click="selectModel(m.id)"
              >
                <span class="model-id">{{ m.id }}</span>
                <span v-if="m.owned_by" class="model-owner">{{ m.owned_by }}</span>
                <span v-if="m.id === formModelId" class="model-check">✓</span>
              </div>
              <div class="model-option model-option-manual" @click="switchToManualInput">
                <span class="model-id">手动输入模型 ID…</span>
              </div>
            </div>
          </div>
          <input
            v-else
            v-model="formModelId"
            :disabled="isMock"
            placeholder="输入或获取模型列表"
            class="form-input"
          />
          <button
            v-if="!isMock"
            class="btn-fetch-models"
            :disabled="store.fetchingModels"
            @click="handleFetchModels"
          >
            {{ store.fetchingModels ? '获取中...' : '获取可用模型' }}
          </button>
        </div>
      </div>

      <div v-if="store.error && store.config" class="form-error">{{ store.error }}</div>

      <div class="form-actions">
        <button class="btn-submit" :disabled="store.loading" @click="handleSave">
          {{ store.loading ? '保存中...' : '保存配置' }}
        </button>
        <button class="btn-reset" :disabled="store.loading" @click="handleReset">
          重置为默认
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  max-width: 640px;
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
.link-back {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.link-back:hover { color: var(--accent); text-decoration: none; }
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

/* Status bar */
.status-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: var(--sp-6);
  font-size: var(--text-sm);
}
.status-label {
  font-weight: 500;
  color: var(--text-secondary);
}
.status-value {
  color: var(--text);
}
.status-sep {
  color: var(--text-tertiary);
}
.key-set { color: var(--status-final); }
.key-unset { color: var(--status-reviewing); }

/* Form */
.config-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
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
.form-row.disabled {
  opacity: 0.5;
}
.form-row.disabled .form-input,
.form-row.disabled .btn-fetch-models,
.form-row.disabled .btn-toggle-vis {
  pointer-events: none;
}
.form-input {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  background: var(--bg);
  color: var(--text);
  transition: border-color var(--transition);
}
.form-input:focus {
  border-color: var(--border-focus);
  outline: none;
}
.form-input:disabled {
  background: var(--bg-panel);
  color: var(--text-tertiary);
}

/* API Key row */
.api-key-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.key-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 8px;
  white-space: nowrap;
}
.key-badge.key-set { background: var(--status-final-bg); color: var(--status-final); }
.key-badge.key-unset { background: var(--status-reviewing-bg); color: var(--status-reviewing); }
.api-key-row .form-input { flex: 1; }
.btn-toggle-vis {
  padding: var(--sp-1) var(--sp-2);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-toggle-vis:hover { background: var(--bg-hover); }

/* Model row */
.model-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.model-row .form-input { flex: 1; }
.btn-fetch-models {
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
  white-space: nowrap;
}
.btn-fetch-models:hover { background: var(--bg-hover); border-color: var(--border-focus); }
.btn-fetch-models:disabled { opacity: 0.5; cursor: not-allowed; }

/* Custom model dropdown */
.model-dropdown-wrap {
  flex: 1;
  position: relative;
}
.model-dropdown-trigger {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  font-size: var(--text-sm);
  color: var(--text);
  cursor: pointer;
  transition: border-color var(--transition);
  text-align: left;
}
.model-dropdown-trigger:hover { border-color: var(--border-focus); }
.model-dropdown-trigger:disabled {
  background: var(--bg-panel);
  color: var(--text-tertiary);
  cursor: not-allowed;
}
.model-dropdown-trigger .placeholder { color: var(--text-tertiary); }
.chevron {
  font-size: 10px;
  color: var(--text-tertiary);
  transition: transform var(--transition);
}
.chevron.open { transform: rotate(180deg); }
.model-dropdown-panel {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 240px;
  overflow-y: auto;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  z-index: 10;
}
.model-option {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--transition);
}
.model-option:hover { background: var(--bg-hover); }
.model-option.selected { background: var(--status-final-bg); }
.model-id {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.model-owner {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.model-check {
  font-size: var(--text-xs);
  color: var(--status-final);
  font-weight: 600;
}
.model-option-manual {
  border-top: 1px solid var(--border);
}
.model-option-manual .model-id {
  color: var(--text-secondary);
  font-style: italic;
}

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
.btn-submit:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-reset {
  padding: var(--sp-2) var(--sp-3);
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition);
}
.btn-reset:hover { background: var(--bg-hover); }
.btn-reset:disabled { opacity: 0.5; cursor: not-allowed; }

.loading-hint {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
.error-hint {
  font-size: var(--text-xs);
  color: var(--status-error);
}
</style>