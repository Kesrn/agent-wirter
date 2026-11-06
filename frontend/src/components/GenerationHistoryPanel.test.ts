import { describe, expect, it } from 'vitest'
import panelSource from './GenerationHistoryPanel.vue?raw'
import clientSource from '../api/client.ts?raw'
import typesSource from '../api/types.ts?raw'

const scriptBlock = panelSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''
const templateBlock = panelSource.match(/<template>([\s\S]*?)<\/template>/)?.[1] ?? ''
const styleBlock = panelSource.match(/<style scoped>([\s\S]*?)<\/style>/)?.[1] ?? ''

describe('GenerationHistoryPanel context snapshot', () => {
  it('exposes a context action for records with runId', () => {
    expect(templateBlock).toContain('上下文')
    expect(templateBlock).toContain(':disabled="!record.runId"')
    expect(scriptBlock).toContain('openContextSnapshot(record: GenerationRecord)')
    expect(scriptBlock).toContain('api.getRunContext(record.runId)')
  })

  it('renders context text and optional prompt snapshot in a modal', () => {
    expect(templateBlock).toContain('生成上下文快照')
    expect(templateBlock).toContain('call.context_text')
    expect(templateBlock).toContain('Prompt 快照')
    expect(templateBlock).toContain('context-modal-overlay')
  })

  it('has API and type contracts for run context snapshots', () => {
    expect(clientSource).toContain('getRunContext')
    expect(clientSource).toContain('/ai-runs/${runId}/context')
    expect(typesSource).toContain('export interface ApiRunContext')
    expect(typesSource).toContain('export interface ApiRunContextCall')
    expect(typesSource).toContain('runId: string | null')
  })

  it('keeps the history panel as a bounded side panel', () => {
    expect(styleBlock).toContain('flex: 0 0 clamp(340px, 28vw, 440px)')
    expect(styleBlock).toContain('max-width: 45vw')
    expect(styleBlock).toContain('overflow-wrap: anywhere')
  })
})
