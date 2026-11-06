import { describe, expect, it } from 'vitest'

import agentPanelSource from './AgentPanel.vue?raw'
import approvalModalSource from './ApprovalModal.vue?raw'
import expertStoreSource from '../stores/index.ts?raw'

describe('AI 初稿对照展示', () => {
  it('保留写手原文，并仅在最终审核弹窗中传入对照', () => {
    expect(expertStoreSource).toContain('initialDraft: string')
    expect(agentPanelSource).toContain('initial_draft')
    expect(agentPanelSource).toContain(':original-content="comparisonOriginalContent"')
    expect(agentPanelSource).toContain('当前候选稿（最新版本）')
    expect(agentPanelSource).toContain("expertStore.setDraft(pid.value, payload.content)")
  })

  it('审核弹窗先展示原始稿，再展示编辑后的候选稿', () => {
    expect(approvalModalSource).toContain('AI 初稿（写手原文）')
    expect(approvalModalSource).toContain('编辑后的候选稿')
    expect(approvalModalSource.indexOf('AI 初稿（写手原文）')).toBeLessThan(
      approvalModalSource.indexOf('编辑后的候选稿'),
    )
    expect(approvalModalSource).toContain('comparison-grid.has-comparison')
    expect(approvalModalSource).toContain('grid-template-columns: repeat(2, minmax(0, 1fr))')
    expect(approvalModalSource).toContain('diff-removed')
    expect(approvalModalSource).toContain('diff-added')
    expect(approvalModalSource).toContain('差异标记说明')
  })

  it('润色和改写使用生成前原文进行双栏对照', () => {
    expect(agentPanelSource).toContain('enhanceSourceDraft')
    expect(agentPanelSource).toContain('comparisonOriginalContent')
    expect(agentPanelSource).toContain('startEnhanceGeneration')
    expect(approvalModalSource).toContain('润色前原文')
    expect(approvalModalSource).toContain('润色后的候选稿')
    expect(approvalModalSource).toContain('改写前原文')
    expect(approvalModalSource).toContain('改写后的候选内容')
  })

  it('只在小说的最终审核里提供定稿本章操作', () => {
    expect(agentPanelSource).toContain(':can-finalize="canFinalizeCandidate"')
    expect(agentPanelSource).toContain('@finalize="handleFinalizeChapter"')
    expect(agentPanelSource).toContain("resumeHumanReviewTerminal('finalize')")
    expect(agentPanelSource).not.toContain('chapterStore.finalizeCurrentChapter')
    expect(agentPanelSource).not.toContain('closePausedWorkflow')
    expect(approvalModalSource).toContain('定稿本章')
    expect(approvalModalSource).toContain("emit('finalize')")
  })

  it('人工采用候选稿只发送 approve，不再用 reject 关闭暂停流程', () => {
    expect(agentPanelSource).toContain("resumeHumanReviewTerminal('approve')")
    expect(agentPanelSource).toContain("resumeHumanReviewTerminal('reject')")
    expect(approvalModalSource).toContain('采用为草稿')
    expect(approvalModalSource).not.toContain('需手动保存')
  })

  it('任务卡确认后的 done 事件仍会打开最终审核弹窗', () => {
    expect(agentPanelSource).toContain('showApprovalAfterTaskCardResumeDone')
    expect(agentPanelSource).toContain('showApprovalAfterTaskCardResumeDone = true')
    expect(agentPanelSource).toContain("typeof progressData.content === 'string'")
    expect(agentPanelSource).toContain("typeof doneData.content === 'string'")
    expect(agentPanelSource).toContain('const shouldShowFinalReview = !!eventContent || !hitlResuming || showApprovalAfterTaskCardResumeDone')
    expect(agentPanelSource).toContain('生成流程已完成，但没有收到候选正文，无法打开最终审核')
  })
})
