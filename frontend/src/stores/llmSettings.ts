import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../api/client'
import type { LLMConfigResponse, LLMConfigCreatePayload, ModelListRequest, ModelInfo } from '../api/types'

export const useLLMSettingsStore = defineStore('llmSettings', () => {
  const config = ref<LLMConfigResponse | null>(null)
  const availableModels = ref<ModelInfo[]>([])
  const loading = ref(false)
  const fetchingModels = ref(false)
  const error = ref('')

  async function loadConfig() {
    loading.value = true
    error.value = ''
    try {
      config.value = await api.getLLMConfig()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '加载配置失败'
    } finally {
      loading.value = false
    }
  }

  async function saveConfig(payload: LLMConfigCreatePayload) {
    loading.value = true
    error.value = ''
    try {
      config.value = await api.updateLLMConfig(payload)
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '保存配置失败'
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

  async function resetToDefaults() {
    loading.value = true
    error.value = ''
    try {
      config.value = await api.updateLLMConfig({ provider: 'mock' })
      availableModels.value = []
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '重置失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  return {
    config, availableModels, loading, fetchingModels, error,
    loadConfig, saveConfig, fetchAvailableModels, resetToDefaults,
  }
})
