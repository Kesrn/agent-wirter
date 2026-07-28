import { describe, expect, it } from 'vitest'
import agentPanelSource from './AgentPanel.vue?raw'
import contextPickerSource from './ContextPicker.vue?raw'

const scriptBlock = agentPanelSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''
const contextPickerScript = contextPickerSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''

describe('chapter generation requirements wiring', () => {
  it('stores ContextPicker userNote with the pending generation context', () => {
    expect(scriptBlock).toContain('targetWords: number; userNote: string; includeKnowledgeSources: boolean; includePreviousSummary: boolean')
    expect(scriptBlock).toContain('function handleContextConfirm(outlineIds: string[], characterIds: string[], worldEntryIds: string[], hiddenThreadIds: string[], targetWords: number, userNote: string, includeKnowledgeSources: boolean, includePreviousSummary: boolean)')
    expect(scriptBlock).toContain('pendingContextPick = { outlineIds, characterIds, worldEntryIds, hiddenThreadIds, targetWords, userNote, includeKnowledgeSources, includePreviousSummary }')
  })

  it('passes full_pipeline userNote as GenerateRequest.user_note', () => {
    expect(scriptBlock).toContain('runGenerateStream(outlineIds, characterIds, worldEntryIds, hiddenThreadIds, targetWords, undefined, undefined, userNote, undefined, includeKnowledgeSources, includePreviousSummary)')
    expect(scriptBlock).toContain('user_note: userNote')
  })

  it('passes the knowledge source opt-in flag as GenerateRequest.include_knowledge_sources', () => {
    expect(scriptBlock).toContain('include_knowledge_sources: includeKnowledgeSources')
    expect(scriptBlock).toContain('pendingContextPick.includeKnowledgeSources')
  })

  it('passes the previous summary opt-in flag as GenerateRequest.include_previous_summary', () => {
    expect(scriptBlock).toContain('include_previous_summary: includePreviousSummary')
    expect(scriptBlock).toContain('pendingContextPick.includePreviousSummary')
  })

  it('renders a default-off previous summary checkbox in ContextPicker', () => {
    expect(contextPickerSource).toContain('加入前文摘要')
    expect(contextPickerScript).toContain('const includePreviousSummary = ref(false)')
    expect(contextPickerScript).toContain('includePreviousSummary.value')
  })

  it('preserves the note when a direction picker flow skips AI directions', () => {
    expect(scriptBlock).toContain('pendingContextPick.userNote')
    expect(scriptBlock).toContain("const combinedUserNote = [pendingContextPick.userNote, userNote.trim()]")
  })
})
