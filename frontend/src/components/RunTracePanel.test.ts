// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import RunTracePanel from './RunTracePanel.vue'
import { api, ApiError } from '../api/client'
import type { RunTraceResponse } from '../api/types'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
      this.name = 'ApiError'
    }
  }
  return {
    api: {
      getRunTrace: vi.fn(),
    },
    ApiError: MockApiError,
  }
})

describe('RunTracePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const fullMockTrace: RunTraceResponse = {
    run: {
      id: 'run-uuid-1',
      request_id: 'abc123req',
      status: 'COMPLETED',
      run_type: 'generation',
      mode: 'full_pipeline',
      workflow_key: 'novel_writer',
      started_at: '2026-11-04T12:00:00Z',
      finished_at: '2026-11-04T12:00:48Z',
      duration_ms: 48000,
      error_message: null,
    },
    totals: {
      llm_calls: 3,
      input_tokens: 12000,
      output_tokens: 5400,
      total_tokens: 17400,
      cost: 0.031,
      retries: 2,
      estimated_calls: 0,
      structured_repairs: 1,
    },
    failed_step: null,
    truncated: false,
    steps: [
      {
        step_order: 1,
        step_name: 'generate_outline',
        node_key: 'outline_writer',
        agent_name: 'planner',
        status: 'COMPLETED',
        started_at: '2026-11-04T12:00:00Z',
        ended_at: '2026-11-04T12:00:12Z',
        duration_ms: 12000,
        retry_count: 0,
        max_retry: 1,
        error_message: null,
        quality: {
          stage: 'outline',
          passed: true,
          error_codes: [],
        },
        llm_calls: [
          {
            id: 'call-1',
            agent_name: 'planner',
            model: 'gemini-2.5-pro',
            latency_ms: 11800,
            input_tokens: 4000,
            output_tokens: 1400,
            cost: null,
            retries: 0,
            usage_estimated: false,
            structured: { schema: 'outline', repairs: 0, ok: true },
            error_message: null,
            rendered_prompt_snapshot: null,
            prompt_truncated: false,
          },
        ],
      },
      {
        step_order: 2,
        step_name: 'generate_draft',
        node_key: 'chapter_writer',
        agent_name: 'writer',
        status: 'COMPLETED',
        started_at: '2026-11-04T12:00:12Z',
        ended_at: '2026-11-04T12:00:48Z',
        duration_ms: 36000,
        retry_count: 1,
        max_retry: 2,
        error_message: null,
        quality: {
          stage: 'draft',
          passed: false,
          error_codes: ['RULE_MUST_INCLUDE_MISSING', 'RULE_CUSTOM_UNKNOWN'],
        },
        llm_calls: [
          {
            id: 'call-2',
            agent_name: 'writer',
            model: 'gemini-2.5-flash',
            latency_ms: 15000,
            input_tokens: 4000,
            output_tokens: 2000,
            cost: null,
            retries: 0,
            usage_estimated: false,
            structured: null,
            error_message: null,
            rendered_prompt_snapshot: null,
            prompt_truncated: false,
          },
          {
            id: 'call-3',
            agent_name: 'writer',
            model: 'gemini-2.5-flash',
            latency_ms: 20000,
            input_tokens: 4000,
            output_tokens: 2000,
            cost: null,
            retries: 1,
            usage_estimated: true,
            structured: { schema: 'draft', repairs: 1, ok: true },
            error_message: null,
            rendered_prompt_snapshot: null,
            prompt_truncated: false,
          },
        ],
      },
    ],
  }

  it('renders a complete mock response correctly (overview, steps, duration, tokens sum)', async () => {
    vi.mocked(api.getRunTrace).mockResolvedValue(fullMockTrace)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-uuid-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    // Dialog accessibility attributes
    const dialog = wrapper.find('[role="dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.attributes('aria-labelledby')).toBe('run-trace-title')

    // Overview cards
    expect(wrapper.text()).toContain('COMPLETED')
    expect(wrapper.text()).toContain('48s') // 48000ms
    expect(wrapper.text()).toContain('17400') // total tokens
    expect(wrapper.text()).toContain('入 12000 / 出 5400')
    expect(wrapper.text()).toContain('$0.0310') // cost
    expect(wrapper.text()).toContain('abc123req') // request_id

    // Steps timeline
    const steps = wrapper.findAll('.timeline-step')
    expect(steps.length).toBe(2)

    // Step 1: 12000ms out of 48000ms = 25%
    expect(steps[0].text()).toContain('#1')
    expect(steps[0].text()).toContain('generate_outline')
    expect(steps[0].text()).toContain('planner')
    expect(steps[0].text()).toContain('5400 tokens') // call-1: 4000+1400 = 5400
    const bar0 = steps[0].find('.step-duration-bar')
    expect(bar0.attributes('style')).toContain('25%')

    // Step 2: 36000ms out of 48000ms = 75%
    expect(steps[1].text()).toContain('#2')
    expect(steps[1].text()).toContain('generate_draft')
    expect(steps[1].text()).toContain('writer')
    expect(steps[1].text()).toContain('12000 tokens') // call-2 (4000+2000=6000) + call-3 (4000+2000=6000) = 12000
    const bar1 = steps[1].find('.step-duration-bar')
    expect(bar1.attributes('style')).toContain('75%')

    // Quality gate in step 2: RULE_MUST_INCLUDE_MISSING -> 必含事件缺失, RULE_CUSTOM_UNKNOWN -> RULE_CUSTOM_UNKNOWN
    expect(steps[1].text()).toContain('质量门未通过')
    expect(steps[1].text()).toContain('必含事件缺失')
    expect(steps[1].text()).toContain('RULE_CUSTOM_UNKNOWN')
  })

  it('handles failed_step: highlights in top banner and in step list, no v-html injection', async () => {
    const maliciousMsg = '<img src="x" onerror="alert(1)"> 节点发生致命错误'
    const failedTrace: RunTraceResponse = {
      run: {
        id: 'run-fail',
        request_id: 'req-fail',
        status: 'FAILED',
        duration_ms: 15000,
      },
      totals: { llm_calls: 1, total_tokens: 2000 },
      failed_step: {
        step_order: 2,
        step_name: 'writer_node',
        agent_name: 'chapter_writer',
        error_message: maliciousMsg,
      },
      truncated: false,
      steps: [
        {
          step_order: 1,
          step_name: 'planner_node',
          status: 'COMPLETED',
          duration_ms: 5000,
        },
        {
          step_order: 2,
          step_name: 'writer_node',
          status: 'FAILED',
          duration_ms: 10000,
          error_message: maliciousMsg,
        },
      ],
    }

    vi.mocked(api.getRunTrace).mockResolvedValue(failedTrace)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-fail' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    // Top banner
    const failedBanner = wrapper.find('.trace-alert-danger')
    expect(failedBanner.exists()).toBe(true)
    expect(failedBanner.text()).toContain('失败节点：writer_node（chapter_writer）')
    expect(failedBanner.text()).toContain('节点发生致命错误')

    // Security check: must NOT render <img> tag
    expect(wrapper.find('img[src="x"]').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('<img src="x"')

    // Step 2 highlighted
    const steps = wrapper.findAll('.timeline-step')
    expect(steps[0].classes()).not.toContain('is-failed')
    expect(steps[1].classes()).toContain('is-failed')
  })

  it('handles null values robustly (cost: null, duration_ms: null, quality: null, steps: [])', async () => {
    const nullTrace: RunTraceResponse = {
      run: {
        id: 'run-null',
        request_id: 'req-empty',
        status: 'PENDING',
        duration_ms: null,
      },
      totals: {
        cost: null,
        llm_calls: null,
        total_tokens: null,
        input_tokens: null,
        output_tokens: null,
        retries: null,
      },
      failed_step: null,
      truncated: false,
      steps: [],
    }

    vi.mocked(api.getRunTrace).mockResolvedValue(nullTrace)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-null' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('未配置价格')
    expect(wrapper.text()).toContain('未记录任何步骤')
    expect(wrapper.find('.trace-empty').exists()).toBe(true)
  })

  it('handles "show prompts" toggle: confirms before requesting, renders pre snapshot or truncation', async () => {
    vi.mocked(api.getRunTrace).mockResolvedValueOnce(fullMockTrace)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-uuid-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    // Initial call was with includePrompts = false
    expect(api.getRunTrace).toHaveBeenCalledWith('run-uuid-1', { includePrompts: false })

    // Click "显示提示词"
    const toggleBtn = wrapper.find('.btn-toggle-prompts')
    await toggleBtn.trigger('click')
    await wrapper.vm.$nextTick()

    // Confirmation box appears
    expect(wrapper.find('.prompt-confirm-dialog').exists()).toBe(true)
    expect(wrapper.text()).toContain('提示词可能包含你的设定和正文，仅在本机查看')

    // Mock response for include_prompts = true
    const traceWithPrompts: RunTraceResponse = JSON.parse(JSON.stringify(fullMockTrace))
    traceWithPrompts.steps[0].llm_calls![0].rendered_prompt_snapshot = 'System: You are an author.\nUser: Write chapter 1.'
    traceWithPrompts.steps[0].llm_calls![0].prompt_truncated = true

    vi.mocked(api.getRunTrace).mockResolvedValueOnce(traceWithPrompts)

    // Click "确认"
    await wrapper.find('.btn-confirm-ok').trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(api.getRunTrace).toHaveBeenCalledWith('run-uuid-1', { includePrompts: true })
    expect(wrapper.find('.prompt-confirm-dialog').exists()).toBe(false)

    // Expand step 1 to see the prompt snapshot
    const stepHeaders = wrapper.findAll('.step-header')
    await stepHeaders[0].trigger('click')
    await wrapper.vm.$nextTick()

    const snapshotPre = wrapper.find('.snapshot-content')
    expect(snapshotPre.exists()).toBe(true)
    expect(snapshotPre.text()).toBe('System: You are an author.\nUser: Write chapter 1.')
    expect(wrapper.find('.snapshot-truncated').exists()).toBe(true)
    expect(wrapper.find('.snapshot-truncated').text()).toBe('已截断')
  })

  it('handles 404, 500 error states with retry', async () => {
    vi.mocked(api.getRunTrace).mockRejectedValueOnce(new ApiError(404, 'Not Found'))

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-missing' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('找不到这次运行的追踪')
    expect(wrapper.find('.btn-retry').exists()).toBe(false) // 404 has no retry button

    // Test generic error with retry
    vi.mocked(api.getRunTrace).mockRejectedValueOnce(new Error('网络连接超时'))
    const wrapperErr = mount(RunTracePanel, {
      props: { runId: 'run-err' },
    })
    await wrapperErr.vm.$nextTick()
    await wrapperErr.vm.$nextTick()

    expect(wrapperErr.text()).toContain('网络连接超时')
    const retryBtn = wrapperErr.find('.btn-retry')
    expect(retryBtn.exists()).toBe(true)

    vi.mocked(api.getRunTrace).mockResolvedValueOnce(fullMockTrace)
    await retryBtn.trigger('click')
    await wrapperErr.vm.$nextTick()
    await wrapperErr.vm.$nextTick()

    expect(wrapperErr.text()).toContain('COMPLETED')
  })

  it('handles truncated warning banner when trace.truncated is true', async () => {
    const truncatedTrace: RunTraceResponse = {
      ...fullMockTrace,
      truncated: true,
    }
    vi.mocked(api.getRunTrace).mockResolvedValue(truncatedTrace)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-trunc' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.trace-alert-warning').exists()).toBe(true)
    expect(wrapper.text()).toContain('步骤或调用过多，已截断显示')
  })

  it('closes on close button, backdrop click, and Escape key; restores focus', async () => {
    vi.mocked(api.getRunTrace).mockResolvedValue(fullMockTrace)

    // Setup an element that was previously focused
    const triggerBtn = document.createElement('button')
    triggerBtn.id = 'trigger-btn'
    document.body.appendChild(triggerBtn)
    triggerBtn.focus()
    expect(document.activeElement).toBe(triggerBtn)

    const wrapper = mount(RunTracePanel, {
      props: { runId: 'run-close' },
      attachTo: document.body,
    })
    await wrapper.vm.$nextTick()

    // Close button
    await wrapper.find('.trace-close-btn').trigger('click')
    expect(wrapper.emitted('close')).toBeTruthy()

    // Backdrop click
    await wrapper.find('.run-trace-overlay').trigger('click')
    expect(wrapper.emitted('close')?.length).toBe(2)

    // Escape key
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(wrapper.emitted('close')?.length).toBe(3)

    // Unmount and verify focus restored to triggerBtn
    wrapper.unmount()
    expect(document.activeElement).toBe(triggerBtn)
    document.body.removeChild(triggerBtn)
  })
})
