import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../api/client'
import type {
  LLMConfigCreatePayload,
  LLMConfigResponse,
  LLMConfigUpdatePayload,
  ModelInfo,
  ModelListRequest,
} from '../api/types'

export const useLLMSettingsStore = defineStore('llmSettings', () => {
  const config = ref<LLMConfigResponse | null>(null)
  const configs = ref<LLMConfigResponse[]>([])
  const selectedConfigId = ref<string | null>(null)
  const availableModels = ref<ModelInfo[]>([])
  const loading = ref(false)
  const fetchingModels = ref(false)
  const error = ref('')

  function replaceConfig(updated: LLMConfigResponse) {
    if (!updated.id) return
    const index = configs.value.findIndex(item => item.id === updated.id)
    if (index >= 0) configs.value[index] = updated
    else configs.value.unshift(updated)
  }

  async function loadConfigs() {
    loading.value = true
    error.value = ''
    try {
      const [active, list] = await Promise.all([
        api.getLLMConfig(),
        api.listLLMConfigs(),
      ])
      config.value = active
      configs.value = list
      if (!selectedConfigId.value || !list.some(item => item.id === selectedConfigId.value)) {
        selectedConfigId.value = active.source === 'user' ? active.id : (list[0]?.id ?? null)
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '加载配置失败'
    } finally {
      loading.value = false
    }
  }

  async function createConfig(payload: LLMConfigCreatePayload) {
    loading.value = true
    error.value = ''
    try {
      const created = await api.createLLMConfig(payload)
      await loadConfigs()
      selectedConfigId.value = created.id
      return created
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '新增配置失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function updateConfig(configId: string, payload: LLMConfigUpdatePayload) {
    loading.value = true
    error.value = ''
    try {
      const updated = await api.updateLLMConfig(configId, payload)
      if (updated.is_active) {
        configs.value = configs.value.map(item => ({
          ...item,
          is_active: item.id === configId,
        }))
      }
      replaceConfig(updated)
      if (updated.is_active) config.value = updated
      return updated
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '保存配置失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function activateConfig(configId: string) {
    loading.value = true
    error.value = ''
    try {
      const activated = await api.activateLLMConfig(configId)
      configs.value = configs.value.map(item => ({
        ...item,
        is_active: item.id === configId,
      }))
      replaceConfig(activated)
      config.value = activated
      selectedConfigId.value = configId
      return activated
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '切换配置失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function deleteConfig(configId: string) {
    loading.value = true
    error.value = ''
    try {
      await api.deleteLLMConfig(configId)
      if (selectedConfigId.value === configId) selectedConfigId.value = null
      await loadConfigs()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '删除配置失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function fetchAvailableModels(req: ModelListRequest) {
    fetchingModels.value = true
    error.value = ''
    try {
      availableModels.value = await api.fetchModels(req)
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '获取模型列表失败'
      throw e
    } finally {
      fetchingModels.value = false
    }
  }

  return {
    config,
    configs,
    selectedConfigId,
    availableModels,
    loading,
    fetchingModels,
    error,
    loadConfigs,
    createConfig,
    updateConfig,
    activateConfig,
    deleteConfig,
    fetchAvailableModels,
  }
})
