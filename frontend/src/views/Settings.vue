<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useLLMSettingsStore, useUiStore, friendlyError } from '../stores'
import type { LLMConfigCreatePayload, LLMConfigResponse, LLMConfigUpdatePayload, LLMProviderName } from '../api/types'
import BaseSelect from '../components/BaseSelect.vue'
import { clearAuthSession } from '../utils/authSession'

const router = useRouter()
const store = useLLMSettingsStore()
const ui = useUiStore()

const formName = ref('')
const formProvider = ref<LLMProviderName>('openai')
const formApiKey = ref('')
const formBaseUrl = ref('')
const formModelId = ref('')
const editingId = ref<string | null>(null)
const activateAfterSave = ref(true)
const showApiKey = ref(false)
const useModelDropdown = ref(false)
const modelDropdownOpen = ref(false)
const modelInputMode = ref(false)
let syncingForm = false

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
  mock: 'Mock（已禁用）',
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
  siliconflow: 'SiliconFlow',
  zhipu: '智谱 AI',
  moonshot: '月之暗面',
  qwen: '通义千问',
  yi: '零一万物',
  minimax: 'MiniMax',
  custom: '自定义兼容接口',
}

const providerOptions: { value: LLMProviderName; label: string }[] = [
  'openai', 'deepseek', 'siliconflow', 'zhipu', 'moonshot', 'qwen', 'yi', 'minimax', 'custom',
].map(value => ({ value: value as LLMProviderName, label: PROVIDER_LABELS[value] }))

const currentProfile = computed(() =>
  editingId.value ? store.configs.find(item => item.id === editingId.value) ?? null : null,
)
const savedApiKeyMask = computed(() => currentProfile.value?.api_key_masked ?? '')
const isCreating = computed(() => !editingId.value)
const canSave = computed(() => Boolean(formName.value.trim() && formProvider.value && formModelId.value.trim()))
const apiKeyPlaceholder = computed(() => savedApiKeyMask.value
  ? `已保存：${savedApiKeyMask.value}`
  : '输入 API Key')

function resetModelPicker() {
  store.availableModels = []
  useModelDropdown.value = false
  modelDropdownOpen.value = false
  modelInputMode.value = false
}

async function fillForm(config: LLMConfigResponse) {
  syncingForm = true
  editingId.value = config.id
  store.selectedConfigId = config.id
  formName.value = config.name
  formProvider.value = config.provider === 'mock' ? 'openai' : config.provider
  formApiKey.value = ''
  formBaseUrl.value = config.base_url ?? PROVIDER_DEFAULTS[formProvider.value] ?? ''
  formModelId.value = config.model_id ?? ''
  activateAfterSave.value = config.is_active
  showApiKey.value = false
  resetModelPicker()
  await nextTick()
  syncingForm = false
}

function startNewConfig() {
  syncingForm = true
  editingId.value = null
  store.selectedConfigId = null
  formName.value = `新配置 ${store.configs.length + 1}`
  formProvider.value = 'openai'
  formApiKey.value = ''
  formBaseUrl.value = PROVIDER_DEFAULTS.openai
  formModelId.value = ''
  activateAfterSave.value = true
  showApiKey.value = false
  resetModelPicker()
  nextTick(() => { syncingForm = false })
}

onMounted(async () => {
  await store.loadConfigs()
  const selected = store.configs.find(item => item.id === store.selectedConfigId)
    ?? store.configs.find(item => item.is_active)
    ?? store.configs[0]
  if (selected) await fillForm(selected)
  else startNewConfig()
})

watch(formProvider, provider => {
  if (syncingForm) return
  formBaseUrl.value = PROVIDER_DEFAULTS[provider] ?? ''
  formModelId.value = ''
  resetModelPicker()
  if (isCreating.value && /^新配置\s+\d+$/.test(formName.value)) {
    formName.value = `${PROVIDER_LABELS[provider]} 配置`
  }
})

async function handleSave() {
  if (!canSave.value) {
    ui.showToast('请填写配置名称和模型 ID', 'error')
    return
  }
  const common = {
    name: formName.value.trim(),
    provider: formProvider.value,
    base_url: formBaseUrl.value.trim() || null,
    model_id: formModelId.value.trim() || null,
  }
  try {
    if (editingId.value) {
      const payload: LLMConfigUpdatePayload = { ...common }
      if (formApiKey.value) payload.api_key = formApiKey.value
      await store.updateConfig(editingId.value, payload)
      if (activateAfterSave.value && !currentProfile.value?.is_active) {
        await store.activateConfig(editingId.value)
      }
      const updated = store.configs.find(item => item.id === editingId.value)
      if (updated) await fillForm(updated)
      ui.showToast('配置已更新', 'success')
    } else {
      const payload: LLMConfigCreatePayload = {
        ...common,
        api_key: formApiKey.value,
        is_active: activateAfterSave.value,
      }
      const created = await store.createConfig(payload)
      const saved = store.configs.find(item => item.id === created.id) ?? created
      await fillForm(saved)
      ui.showToast('配置已新增', 'success')
    }
  } catch (error: unknown) {
    ui.showToast(friendlyError(error, '保存失败'), 'error')
  }
}

async function handleActivate(config: LLMConfigResponse) {
  if (!config.id || config.is_active) return
  try {
    await store.activateConfig(config.id)
    await fillForm(store.configs.find(item => item.id === config.id) ?? config)
    ui.showToast(`已切换到“${config.name}”`, 'success')
  } catch (error: unknown) {
    ui.showToast(friendlyError(error, '切换失败'), 'error')
  }
}

async function handleDelete(config: LLMConfigResponse) {
  if (!config.id || !window.confirm(`确定删除配置“${config.name}”吗？`)) return
  try {
    await store.deleteConfig(config.id)
    const next = store.configs.find(item => item.is_active) ?? store.configs[0]
    if (next) await fillForm(next)
    else startNewConfig()
    ui.showToast('配置已删除', 'success')
  } catch (error: unknown) {
    ui.showToast(friendlyError(error, '删除失败'), 'error')
  }
}

async function handleFetchModels() {
  if (!formApiKey.value && !savedApiKeyMask.value) {
    ui.showToast('请先输入 API Key', 'error')
    return
  }
  try {
    await store.fetchAvailableModels({
      provider: formProvider.value,
      api_key: formApiKey.value || undefined,
      base_url: formBaseUrl.value.trim() || null,
      config_id: editingId.value,
    })
    useModelDropdown.value = store.availableModels.length > 0
    modelInputMode.value = false
    ui.showToast(`获取到 ${store.availableModels.length} 个模型`, 'success')
  } catch (error: unknown) {
    ui.showToast(friendlyError(error, '获取模型失败'), 'error')
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

function onClickOutsideModel(event: MouseEvent) {
  if (!(event.target as HTMLElement).closest('.model-dropdown-wrap')) modelDropdownOpen.value = false
}

onMounted(() => document.addEventListener('click', onClickOutsideModel))
onBeforeUnmount(() => document.removeEventListener('click', onClickOutsideModel))

function handleLogout() {
  clearAuthSession()
  router.push('/login')
}
</script>

<template>
  <div class="settings-page">
    <header class="page-header">
      <div>
        <h1>LLM 大模型配置</h1>
        <p>保存多个供应商档案，并随时切换当前用于生成的配置。</p>
      </div>
      <div class="header-actions">
        <router-link to="/projects" class="link-back">← 项目列表</router-link>
        <button class="btn-logout" @click="handleLogout">退出登录</button>
      </div>
    </header>

    <section v-if="store.config" class="active-summary">
      <span class="active-dot"></span>
      <div>
        <span class="active-label">当前启用</span>
        <strong>{{ store.config.name }}</strong>
        <span>{{ PROVIDER_LABELS[store.config.provider] ?? store.config.provider }}</span>
        <span>{{ store.config.model_id || '未设置模型' }}</span>
        <code v-if="store.config.api_key_masked">{{ store.config.api_key_masked }}</code>
      </div>
    </section>

    <div class="settings-layout">
      <aside class="profile-panel">
        <div class="profile-panel-header">
          <div>
            <h2>已保存配置</h2>
            <span>{{ store.configs.length }} 条</span>
          </div>
          <button class="btn-new" @click="startNewConfig">+ 新增</button>
        </div>

        <div v-if="store.loading && !store.configs.length" class="loading-hint">加载中...</div>
        <div v-else-if="!store.configs.length" class="empty-profiles">
          暂无保存配置，点击“新增”创建第一条。
        </div>
        <div v-else class="profile-list">
          <article
            v-for="item in store.configs"
            :key="item.id || item.name"
            class="profile-card"
            :class="{ selected: item.id === editingId, active: item.is_active }"
            @click="fillForm(item)"
          >
            <div class="profile-card-head">
              <strong>{{ item.name }}</strong>
              <span v-if="item.is_active" class="active-badge">当前</span>
            </div>
            <div class="profile-meta">
              <span>{{ PROVIDER_LABELS[item.provider] ?? item.provider }}</span>
              <span>{{ item.model_id || '未设置模型' }}</span>
            </div>
            <code v-if="item.api_key_masked" class="profile-key">{{ item.api_key_masked }}</code>
            <div class="profile-actions" @click.stop>
              <button v-if="!item.is_active" class="btn-switch" :disabled="store.loading" @click="handleActivate(item)">切换使用</button>
              <button class="btn-delete" :disabled="store.loading" @click="handleDelete(item)">删除</button>
            </div>
          </article>
        </div>
      </aside>

      <main class="editor-panel">
        <div class="editor-header">
          <div>
            <h2>{{ isCreating ? '新增供应商配置' : '编辑供应商配置' }}</h2>
            <p>{{ isCreating ? '新配置不会覆盖已有供应商。' : 'API Key 留空时保留原密钥。' }}</p>
          </div>
          <span v-if="currentProfile?.is_active" class="active-badge">当前启用</span>
        </div>

        <div class="config-form">
          <div class="form-row">
            <label>配置名称</label>
            <input v-model="formName" class="form-input" placeholder="例如：OpenAI 主账号、DeepSeek 备用" />
          </div>

          <div class="form-row">
            <label>Provider</label>
            <BaseSelect v-model="formProvider" :options="providerOptions" />
          </div>

          <div class="form-row">
            <label>API Key</label>
            <div class="api-key-row">
              <span :class="savedApiKeyMask ? 'key-badge key-set' : 'key-badge key-unset'">
                {{ savedApiKeyMask ? '已保存' : '未设置' }}
              </span>
              <input
                v-model="formApiKey"
                :type="showApiKey ? 'text' : 'password'"
                :placeholder="apiKeyPlaceholder"
                class="form-input"
              />
              <button
                class="btn-secondary"
                :disabled="!formApiKey"
                :title="formApiKey ? '显示或隐藏本次输入' : '为安全起见，已保存的 API Key 只能显示掩码'"
                @click="showApiKey = !showApiKey"
              >
                {{ showApiKey ? '隐藏输入' : '显示输入' }}
              </button>
            </div>
            <p v-if="savedApiKeyMask && !formApiKey" class="form-hint">
              已保存密钥：<code>{{ savedApiKeyMask }}</code>，留空保存不会覆盖原密钥。
            </p>
          </div>

          <div class="form-row">
            <label>Base URL</label>
            <input v-model="formBaseUrl" class="form-input" placeholder="API Base URL" />
          </div>

          <div class="form-row">
            <label>Model</label>
            <div class="model-row">
              <div v-if="useModelDropdown && !modelInputMode" class="model-dropdown-wrap">
                <button class="model-dropdown-trigger" @click.stop="modelDropdownOpen = !modelDropdownOpen">
                  <span>{{ formModelId || '选择模型' }}</span><span>▾</span>
                </button>
                <div v-if="modelDropdownOpen" class="model-dropdown-panel">
                  <button v-for="model in store.availableModels" :key="model.id" class="model-option" @click="selectModel(model.id)">
                    {{ model.id }}
                  </button>
                  <button class="model-option" @click="switchToManualInput">手动输入模型 ID…</button>
                </div>
              </div>
              <input v-else v-model="formModelId" class="form-input" placeholder="模型 ID" />
              <button class="btn-secondary" :disabled="store.fetchingModels" @click="handleFetchModels">
                {{ store.fetchingModels ? '获取中...' : '获取可用模型' }}
              </button>
            </div>
          </div>

          <label class="activate-option">
            <input v-model="activateAfterSave" type="checkbox" />
            <span>{{ isCreating ? '保存后立即切换为当前配置' : '保存后设为当前配置' }}</span>
          </label>

          <div v-if="store.error" class="form-error">{{ store.error }}</div>
          <div class="form-actions">
            <button class="btn-submit" :disabled="store.loading || !canSave" @click="handleSave">
              {{ store.loading ? '保存中...' : (isCreating ? '新增配置' : '保存修改') }}
            </button>
            <button v-if="!isCreating && !currentProfile?.is_active" class="btn-switch-large" :disabled="store.loading" @click="currentProfile && handleActivate(currentProfile)">
              切换为当前配置
            </button>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  width: 100%;
  max-width: 1100px;
  height: calc(100dvh - var(--desktop-status-bar-height, 0px));
  margin: 0 auto;
  padding: var(--sp-8) var(--sp-6) calc(var(--sp-8) + env(safe-area-inset-bottom, 0px));
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}
.page-header { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:24px; }
.page-header h1 { margin:0; font-size:var(--text-xl); color:var(--text); }
.page-header p { margin:6px 0 0; color:var(--text-secondary); font-size:var(--text-sm); }
.header-actions { display:flex; align-items:center; gap:12px; }
.link-back { color:var(--text-secondary); font-size:var(--text-sm); }
.btn-logout,.btn-secondary,.btn-delete,.btn-switch,.btn-new,.btn-switch-large { border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-panel); color:var(--text-secondary); cursor:pointer; }
.btn-logout { padding:9px 14px; }
.active-summary { display:flex; align-items:center; gap:12px; padding:14px 18px; margin-bottom:18px; border:1px solid color-mix(in srgb,var(--status-final) 42%,var(--border)); border-radius:var(--radius); background:var(--status-final-bg); }
.active-summary>div { display:flex; align-items:center; flex-wrap:wrap; gap:10px; color:var(--text-secondary); font-size:var(--text-sm); }
.active-summary strong { color:var(--text); }
.active-summary code { color:var(--status-final); }
.active-dot { width:9px; height:9px; border-radius:50%; background:var(--status-final); box-shadow:0 0 0 4px color-mix(in srgb,var(--status-final) 18%,transparent); }
.active-label { color:var(--status-final); font-weight:700; }
.settings-layout { display:grid; grid-template-columns:minmax(270px,340px) minmax(0,1fr); gap:18px; align-items:start; }
.profile-panel,.editor-panel { border:1px solid var(--border); border-radius:16px; background:var(--bg-panel); }
.profile-panel { padding:16px; position:sticky; top:16px; }
.profile-panel-header,.editor-header { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.profile-panel-header h2,.editor-header h2 { margin:0; color:var(--text); font-size:var(--text-base); }
.profile-panel-header span,.editor-header p { color:var(--text-tertiary); font-size:var(--text-xs); margin:4px 0 0; }
.btn-new { padding:8px 12px; color:var(--accent); border-color:color-mix(in srgb,var(--accent) 45%,var(--border)); }
.profile-list { display:flex; flex-direction:column; gap:10px; margin-top:14px; }
.profile-card { padding:13px; border:1px solid var(--border); border-radius:12px; background:var(--bg); cursor:pointer; transition:.18s ease; }
.profile-card:hover,.profile-card.selected { border-color:var(--border-focus); transform:translateY(-1px); }
.profile-card.active { box-shadow:inset 3px 0 0 var(--status-final); }
.profile-card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.profile-card-head strong { color:var(--text); font-size:var(--text-sm); }
.active-badge { padding:2px 8px; border-radius:999px; background:var(--status-final-bg); color:var(--status-final); font-size:11px; white-space:nowrap; }
.profile-meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; color:var(--text-secondary); font-size:var(--text-xs); }
.profile-key { display:block; margin-top:7px; color:var(--text-tertiary); font-size:11px; }
.profile-actions { display:flex; gap:8px; margin-top:10px; }
.btn-switch,.btn-delete { padding:5px 9px; font-size:var(--text-xs); }
.btn-switch { color:var(--accent); }
.btn-delete { color:var(--status-draft); }
.empty-profiles,.loading-hint { margin-top:14px; padding:18px 8px; color:var(--text-tertiary); font-size:var(--text-sm); text-align:center; }
.editor-panel { padding:22px; }
.editor-header { margin-bottom:20px; }
.config-form { display:flex; flex-direction:column; gap:16px; }
.form-row { display:flex; flex-direction:column; gap:6px; }
.form-row label { color:var(--text-secondary); font-size:var(--text-xs); font-weight:600; }
.form-input { width:100%; min-width:0; padding:10px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg); color:var(--text); font-size:var(--text-sm); box-sizing:border-box; }
.form-input:focus { outline:none; border-color:var(--border-focus); box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 12%,transparent); }
.api-key-row,.model-row { display:flex; align-items:center; gap:9px; }
.api-key-row .form-input,.model-row .form-input,.model-dropdown-wrap { flex:1; }
.key-badge { padding:3px 8px; border-radius:999px; font-size:11px; white-space:nowrap; }
.key-set { color:var(--status-final); background:var(--status-final-bg); }
.key-unset { color:var(--status-reviewing); background:var(--status-reviewing-bg); }
.btn-secondary { padding:9px 12px; white-space:nowrap; }
button:disabled { opacity:.5; cursor:not-allowed; }
.form-hint { margin:0; color:var(--text-tertiary); font-size:var(--text-xs); }
.form-hint code { color:var(--text-secondary); }
.model-dropdown-wrap { position:relative; }
.model-dropdown-trigger { width:100%; display:flex; justify-content:space-between; padding:10px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg); color:var(--text); }
.model-dropdown-panel { position:absolute; z-index:20; top:calc(100% + 5px); left:0; right:0; max-height:260px; overflow:auto; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-panel); box-shadow:var(--shadow-lg); }
.model-option { display:block; width:100%; padding:9px 12px; border:0; background:none; color:var(--text); text-align:left; cursor:pointer; }
.model-option:hover { background:var(--bg-hover); }
.activate-option { display:flex; align-items:center; gap:8px; color:var(--text-secondary); font-size:var(--text-sm); cursor:pointer; }
.activate-option input { accent-color:var(--accent); }
.form-actions { display:flex; justify-content:flex-end; gap:10px; padding-top:4px; }
.btn-submit,.btn-switch-large { padding:10px 18px; font-size:var(--text-sm); font-weight:700; }
.btn-submit { border:1px solid var(--accent); border-radius:var(--radius); background:var(--accent); color:var(--text-inverse); cursor:pointer; }
.btn-switch-large { color:var(--accent); }
.form-error { color:var(--status-draft); font-size:var(--text-sm); }
@media (max-width:800px) {
  .settings-page{padding:18px 14px calc(24px + env(safe-area-inset-bottom, 0px))}
  .page-header{flex-direction:column}
  .settings-layout{grid-template-columns:1fr}
  .profile-panel{position:static}
  .api-key-row,.model-row{align-items:stretch;flex-direction:column}
  .api-key-row>* ,.model-row>*{width:100%}
  .form-actions{flex-direction:column}
  .btn-submit,.btn-switch-large{width:100%}
}
</style>
