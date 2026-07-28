import { describe, expect, it } from 'vitest'

import settingsSource from './Settings.vue?raw'

describe('LLM API Key masked reflection', () => {
  it('shows the server-provided mask while keeping the persisted key out of the input value', () => {
    expect(settingsSource).toContain('const savedApiKeyMask')
    expect(settingsSource).toContain('已保存：${savedApiKeyMask.value}')
    expect(settingsSource).toContain('已保存密钥：<code>{{ savedApiKeyMask }}</code>')
    expect(settingsSource).toContain('留空保存不会覆盖原密钥')
    expect(settingsSource).not.toContain('formApiKey.value = store.config.api_key_masked')
  })

  it('only allows visibility toggling for a newly typed replacement key', () => {
    expect(settingsSource).toContain(':disabled="!formApiKey"')
    expect(settingsSource).toContain('已保存的 API Key 只能显示掩码')
  })
})
