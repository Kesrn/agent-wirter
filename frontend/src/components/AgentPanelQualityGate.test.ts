import { describe, expect, it } from 'vitest'

import agentPanelSource from './AgentPanel.vue?raw'
import workflowSource from './AgentWorkflow.vue?raw'
import approvalModalSource from './ApprovalModal.vue?raw'
import expertStoreSource from '../stores/index.ts?raw'

describe('章节质量门前端链路', () => {
  it('展示三层质量节点而不是把失败流程显示成 100%', () => {
    expect(agentPanelSource).toContain("{ id: 'draft_validator'")
    expect(agentPanelSource).toContain("{ id: 'edited_validator'")
    expect(agentPanelSource).toContain("{ id: 'final_quality'")
    expect(workflowSource).toContain("{ id: 'draft_validator', label: '初稿验收' }")
    expect(workflowSource).toContain("{ id: 'edited_validator', label: '编辑稿验收' }")
    expect(workflowSource).toContain("{ id: 'final_quality', label: '最终质量门' }")
    expect(workflowSource).toContain("steps.filter(s => s === 'success').length")
  })

  it('质量失败时标红对应节点并禁止打开审核弹窗', () => {
    expect(agentPanelSource).toContain("payload.status === 'failed'")
    expect(agentPanelSource).toContain("? 'error'")
    expect(agentPanelSource).toContain('showApproval.value = false')
    expect(agentPanelSource).toContain("latestQualityReport.value?.status === 'failed'")
    expect(agentPanelSource).toContain('质量验收未通过，已阻止候选稿进入人工审核')
  })

  it('篇幅偏差作为警告进入最终审核，由用户选择保留或压缩', () => {
    expect(agentPanelSource).toContain(':quality-report="latestQualityReport"')
    expect(agentPanelSource).toContain("payload.status === 'warning'")
    expect(agentPanelSource).toContain("decision === 'compress'")
    expect(agentPanelSource).toContain('将当前候选稿压缩到约 ${targetWords} 字')
    expect(approvalModalSource).toContain("qualityReport.stage === 'final'")
    expect(approvalModalSource).toContain("return '需要你确认'")
    expect(approvalModalSource).toContain('目标 {{ qualityReport.target_chars }} 字 / 实际 {{ qualityReport.actual_chars }} 字')
    expect(approvalModalSource).toContain("issue.code === 'CONTENT_TOO_LONG'")
    expect(approvalModalSource).toContain("emit('decision', 'compress')")
    expect(approvalModalSource).toContain('让 AI 压缩')
  })

  it('初稿重试后以通过质量门的 Writer 稿作为对照基线', () => {
    expect(agentPanelSource).toContain("payload.stage === 'draft' && (payload.status === 'passed' || payload.status === 'warning')")
    expect(agentPanelSource).toContain('expertStore.setInitialDraft(pid.value, acceptedWriterDraft)')
  })

  it('SSE 失败后持久显示失败原因，而不是停留在普通百分比', () => {
    expect(expertStoreSource).toContain('generationError: string')
    expect(expertStoreSource).toContain("s.generationError = ''")
    expect(agentPanelSource).toContain('expertStore.setGenerationError(pid.value, payload.message)')
    expect(workflowSource).toContain("hasGenerationError ? '生成失败'")
    expect(workflowSource).toContain('本次生成已停止')
    expect(workflowSource).toContain('projectState.generationError')
    expect(workflowSource).toContain('重新点击“章节生成”')
  })
})
