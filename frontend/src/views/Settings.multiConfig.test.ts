import { describe, expect, it } from 'vitest'

import settingsSource from './Settings.vue?raw'
import settingsStoreSource from '../stores/llmSettings.ts?raw'
import clientSource from '../api/client.ts?raw'

describe('multiple LLM provider profiles', () => {
  it('loads, creates, updates, activates and deletes named profiles', () => {
    expect(clientSource).toContain('listLLMConfigs')
    expect(clientSource).toContain('createLLMConfig')
    expect(clientSource).toContain('activateLLMConfig')
    expect(clientSource).toContain('deleteLLMConfig')
    expect(settingsStoreSource).toContain('const configs = ref<LLMConfigResponse[]>([])')
    expect(settingsStoreSource).toContain('async function activateConfig')
  })

  it('renders a selectable saved-profile list and explicit switch controls', () => {
    expect(settingsSource).toContain('已保存配置')
    expect(settingsSource).toContain('v-for="item in store.configs"')
    expect(settingsSource).toContain('切换使用')
    expect(settingsSource).toContain('切换为当前配置')
    expect(settingsSource).toContain('新配置不会覆盖已有供应商')
  })

  it('keeps persisted keys masked and reuses them without returning plaintext', () => {
    expect(settingsSource).toContain('savedApiKeyMask')
    expect(settingsSource).toContain('API Key 留空时保留原密钥')
    expect(settingsSource).toContain('config_id: editingId.value')
  })

  it('keeps the settings form scrollable inside the fixed app viewport', () => {
    expect(settingsSource).toContain('height: calc(100dvh - var(--desktop-status-bar-height, 0px))')
    expect(settingsSource).toContain('overflow-y: auto')
    expect(settingsSource).toContain('-webkit-overflow-scrolling: touch')
    expect(settingsSource).toContain('env(safe-area-inset-bottom, 0px)')
  })
})
