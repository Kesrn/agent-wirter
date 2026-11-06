import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { api } from '../api/client'
import type { LLMConfigResponse } from '../api/types'
import { useLLMSettingsStore } from './llmSettings'

function profile(overrides: Partial<LLMConfigResponse>): LLMConfigResponse {
  return {
    id: 'default-id',
    name: '默认配置',
    provider: 'openai',
    api_key_set: true,
    api_key_masked: 'sk-••••••••••••1234',
    base_url: 'https://api.openai.com/v1',
    model_id: 'gpt-test',
    is_active: false,
    source: 'user',
    created_at: null,
    updated_at: null,
    ...overrides,
  }
}

describe('LLM settings store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('keeps exactly one active profile after an update activates another profile', async () => {
    const store = useLLMSettingsStore()
    const first = profile({ id: 'first', name: 'OpenAI', is_active: true })
    const second = profile({ id: 'second', name: 'DeepSeek', provider: 'deepseek' })
    store.configs = [first, second]
    store.config = first

    vi.spyOn(api, 'updateLLMConfig').mockResolvedValue({ ...second, is_active: true })

    await store.updateConfig('second', { is_active: true })

    expect(store.config?.id).toBe('second')
    expect(store.configs.filter(item => item.is_active).map(item => item.id)).toEqual(['second'])
  })
})
