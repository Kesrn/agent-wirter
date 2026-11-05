// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import EditStatsPanel from './EditStatsPanel.vue'
import { api, ApiError } from '../api/client'
import type { EditStatsResponse } from '../api/types'

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
      getEditStats: vi.fn(),
    },
    ApiError: MockApiError,
  }
})

describe('EditStatsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders explanation subtitle and loading state while fetching stats', async () => {
    let resolvePromise: (val: EditStatsResponse) => void
    const pendingPromise = new Promise<EditStatsResponse>((resolve) => {
      resolvePromise = resolve
    })
    vi.mocked(api.getEditStats).mockReturnValue(pendingPromise)

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })

    expect(wrapper.find('.stats-subtitle').text()).toBe(
      '统计的是 AI 稿被采用后你又改了多少；还没改的章节按 0 计，不代表满意。'
    )
    expect(wrapper.find('.stats-loading').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载统计数据中...')

    resolvePromise!({
      limit: 20,
      offset: 0,
      summary: {
        count: 0,
        edited_count: 0,
        mean_edit_ratio: null,
        median_edit_ratio: null,
        p90_edit_ratio: null,
        total_changed_chars: 0,
        total_original_chars: 0,
      },
      items: [],
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.stats-loading').exists()).toBe(false)
  })

  it('renders error state and retries on retry button click', async () => {
    vi.mocked(api.getEditStats).mockRejectedValueOnce(new ApiError(500, '加载失败，服务器异常'))

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.stats-error').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载失败，服务器异常')

    // Prepare success response for retry
    vi.mocked(api.getEditStats).mockResolvedValueOnce({
      limit: 20,
      offset: 0,
      summary: {
        count: 0,
        edited_count: 0,
        mean_edit_ratio: null,
        median_edit_ratio: null,
        p90_edit_ratio: null,
        total_changed_chars: 0,
        total_original_chars: 0,
      },
      items: [],
    })

    await wrapper.find('.btn-retry').trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.stats-error').exists()).toBe(false)
    expect(wrapper.text()).toContain('还没有已采用的生成记录')
  })

  it('triggers reload when clicking the reload button', async () => {
    vi.mocked(api.getEditStats).mockResolvedValue({
      limit: 20,
      offset: 0,
      summary: {
        count: 0,
        edited_count: 0,
        mean_edit_ratio: null,
        median_edit_ratio: null,
        p90_edit_ratio: null,
        total_changed_chars: 0,
        total_original_chars: 0,
      },
      items: [],
    })

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(api.getEditStats).toHaveBeenCalledTimes(1)

    const reloadBtn = wrapper.find('.stats-reload-btn')
    expect(reloadBtn.exists()).toBe(true)
    expect(reloadBtn.text()).toBe('重新加载')

    await reloadBtn.trigger('click')
    await wrapper.vm.$nextTick()

    expect(api.getEditStats).toHaveBeenCalledTimes(2)
  })

  it('renders empty state when summary.count === 0', async () => {
    vi.mocked(api.getEditStats).mockResolvedValue({
      limit: 20,
      offset: 0,
      summary: {
        count: 0,
        edited_count: 0,
        mean_edit_ratio: null,
        median_edit_ratio: null,
        p90_edit_ratio: null,
        total_changed_chars: 0,
        total_original_chars: 0,
      },
      items: [],
    })

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.stats-empty').exists()).toBe(true)
    expect(wrapper.text()).toContain('还没有已采用的生成记录')
  })

  it('renders summary cards and trend chart with hover tooltip for edited and unedited items', async () => {
    vi.mocked(api.getEditStats).mockResolvedValue({
      limit: 20,
      offset: 0,
      summary: {
        count: 10,
        edited_count: 8,
        mean_edit_ratio: 0.124,
        median_edit_ratio: 0.10,
        p90_edit_ratio: 0.25,
        total_changed_chars: 320,
        total_original_chars: 2500,
      },
      items: [
        {
          generation_record_id: 'rec-2',
          chapter_id: 'chap-1',
          chapter_sequence_number: 2,
          applied_at: '2026-11-04T12:00:00Z',
          final_chars: 345,
          edit_ratio: 0.15,
          changed_chars: 45,
          original_chars: 300,
          edited: true,
        },
        {
          generation_record_id: 'rec-1',
          chapter_id: 'chap-1',
          chapter_sequence_number: 1,
          applied_at: '2026-11-04T11:00:00Z',
          final_chars: 200,
          edit_ratio: 0,
          changed_chars: 0,
          original_chars: 200,
          edited: false,
        },
      ],
    })

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('采用次数')
    expect(wrapper.text()).toContain('10')
    expect(wrapper.text()).toContain('改动过的次数')
    expect(wrapper.text()).toContain('8')
    expect(wrapper.text()).toContain('12.4%') // mean_edit_ratio
    expect(wrapper.text()).toContain('10%')   // median_edit_ratio
    expect(wrapper.text()).toContain('25%')   // p90_edit_ratio
    expect(wrapper.text()).toContain('320 / 2500')

    // SVG chart (rec-1 was older, rec-2 newer -> trendItems is [rec-1, rec-2])
    const dots = wrapper.findAll('.chart-dot')
    expect(dots.length).toBe(2)
    expect(wrapper.find('.chart-line').exists()).toBe(true)

    // Hover unedited dot (rec-1)
    await dots[0].trigger('mouseenter')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.chart-tooltip').exists()).toBe(true)
    expect(wrapper.find('.chart-tooltip').text()).toContain('第 1 章 · 未改动')

    // Hover edited dot (rec-2)
    await dots[1].trigger('mouseenter')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.chart-tooltip').exists()).toBe(true)
    expect(wrapper.find('.chart-tooltip').text()).toContain('第 2 章 · 改动 15%')

    await dots[1].trigger('mouseleave')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.chart-tooltip').exists()).toBe(false)
  })

  it('handles 1 item safely without crashing and supports missing optional fields', async () => {
    vi.mocked(api.getEditStats).mockResolvedValue({
      limit: 20,
      offset: 0,
      summary: {
        count: 1,
        edited_count: 0,
        mean_edit_ratio: 0,
        median_edit_ratio: 0,
        p90_edit_ratio: 0,
        total_changed_chars: 0,
        total_original_chars: 500,
      },
      items: [
        {
          generation_record_id: 'rec-only',
          chapter_id: null,
          chapter_sequence_number: null,
          run_id: null,
          mode: null,
          similarity: null,
          applied_at: '2026-11-04T10:00:00Z',
          final_chars: 500,
          edit_ratio: 0,
          changed_chars: 0,
          original_chars: 500,
          edited: false,
        },
      ],
    })

    const wrapper = mount(EditStatsPanel, {
      props: { projectId: 'project-1' },
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    const dots = wrapper.findAll('.chart-dot')
    expect(dots.length).toBe(1)
    expect(wrapper.find('.chart-line').exists()).toBe(false)

    // Hover item without chapter_sequence_number
    await dots[0].trigger('mouseenter')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.chart-tooltip').text()).toBe('未改动')
  })
})
