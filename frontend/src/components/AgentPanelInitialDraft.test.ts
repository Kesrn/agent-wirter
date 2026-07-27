import { describe, expect, it } from 'vitest'

import agentPanelSource from './AgentPanel.vue?raw'
import approvalModalSource from './ApprovalModal.vue?raw'
import expertStoreSource from '../stores/index.ts?raw'

describe('AI 初稿对照展示', () => {
  it('保留写手原文，并仅在最终审核弹窗中传入对照', () => {
    expect(expertStoreSource).toContain('initialDraft: string')
    expect(agentPanelSource).toContain('initial_draft')
    expect(agentPanelSource).toContain(':original-content="initialDraftPreview"')
    expect(agentPanelSource).toContain('当前候选稿（最新版本）')
    expect(agentPanelSource).toContain("expertStore.setDraft(pid.value, payload.content)")
  })

  it('审核弹窗先展示原始稿，再展示编辑后的候选稿', () => {
    expect(approvalModalSource).toContain('AI 初稿（写手原文）')
    expect(approvalModalSource).toContain('编辑后的候选稿')
    expect(approvalModalSource.indexOf('AI 初稿（写手原文）')).toBeLessThan(
      approvalModalSource.indexOf('编辑后的候选稿'),
    )
  })

  it('只在小说的最终审核里提供定稿本章操作', () => {
    expect(agentPanelSource).toContain(':can-finalize="canFinalizeCandidate"')
    expect(agentPanelSource).toContain('@finalize="handleFinalizeChapter"')
    expect(approvalModalSource).toContain('定稿本章')
    expect(approvalModalSource).toContain("emit('finalize')")
  })
})
