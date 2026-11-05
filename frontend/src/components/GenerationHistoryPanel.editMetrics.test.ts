// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import GenerationHistoryPanel from './GenerationHistoryPanel.vue'
import { api } from '../api/client'

describe('GenerationHistoryPanel edit metric badge', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValue([])
    vi.spyOn(api, 'listDocumentGenerations').mockResolvedValue([])
  })

  it('renders "改动 13%" badge for applied records with edited=true', async () => {
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValueOnce([
      {
        id: 'rec-1',
        project_id: 'proj-1',
        chapter_id: 'chap-1',
        document_id: null,
        mode: 'full_pipeline',
        expert_id: null,
        direction: null,
        word_count: 3000,
        status: 'applied',
        created_at: '2026-11-04T08:00:00Z',
        human_edit_metrics: {
          edited: true,
          original_chars: 3000,
          final_chars: 3100,
          changed_chars: 400,
          edit_ratio: 0.1333,
        },
      },
    ])

    const wrapper = mount(GenerationHistoryPanel, {
      props: {
        projectId: 'proj-1',
        mode: 'novel',
        sequenceNumber: 1,
      },
    })

    await new Promise(r => setTimeout(r, 10))

    const metricBadge = wrapper.find('.generation-edit-metric')
    expect(metricBadge.exists()).toBe(true)
    expect(metricBadge.text()).toBe('改动 13%')
    expect(metricBadge.classes()).not.toContain('metric-clean')
    expect(metricBadge.attributes('title')).toBe('采用后你到目前为止改动的比例，随编辑自动更新；刚采用还没改过时显示‘未改动’')
  })

  it('renders "未改动" badge for applied records with edited=false and verifies title tooltip', async () => {
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValueOnce([
      {
        id: 'rec-2',
        project_id: 'proj-1',
        chapter_id: 'chap-1',
        document_id: null,
        mode: 'full_pipeline',
        expert_id: null,
        direction: null,
        word_count: 3000,
        status: 'applied',
        created_at: '2026-11-04T08:00:00Z',
        human_edit_metrics: {
          edited: false,
          original_chars: 3000,
          final_chars: 3000,
          changed_chars: 0,
          edit_ratio: 0,
        },
      },
    ])

    const wrapper = mount(GenerationHistoryPanel, {
      props: {
        projectId: 'proj-1',
        mode: 'novel',
        sequenceNumber: 1,
      },
    })

    await new Promise(r => setTimeout(r, 10))

    const metricBadge = wrapper.find('.generation-edit-metric')
    expect(metricBadge.exists()).toBe(true)
    expect(metricBadge.text()).toBe('未改动')
    expect(metricBadge.classes()).toContain('metric-clean')
    expect(metricBadge.attributes('title')).toBe('采用后你到目前为止改动的比例，随编辑自动更新；刚采用还没改过时显示‘未改动’')
  })

  it('handles HumanEditMetrics with new optional fields or missing optional fields gracefully', async () => {
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValueOnce([
      {
        id: 'rec-new-fields',
        project_id: 'proj-1',
        chapter_id: 'chap-1',
        document_id: null,
        mode: 'full_pipeline',
        expert_id: null,
        direction: null,
        word_count: 2500,
        status: 'applied',
        created_at: '2026-11-04T09:00:00Z',
        human_edit_metrics: {
          method: 'char_diff',
          edited: true,
          anchor_version_id: 'v-1',
          human_version_id: 'v-2',
          original_chars: 2500,
          final_chars: 2700,
          changed_chars: 200,
          changed_original_chars: 100,
          inserted_chars: 100,
          similarity: 0.95,
          edit_ratio: 0.08,
          computed_at: '2026-11-04T09:05:00Z',
        },
      },
    ])

    const wrapper = mount(GenerationHistoryPanel, {
      props: {
        projectId: 'proj-1',
        mode: 'novel',
        sequenceNumber: 1,
      },
    })

    await new Promise(r => setTimeout(r, 10))

    const metricBadge = wrapper.find('.generation-edit-metric')
    expect(metricBadge.exists()).toBe(true)
    expect(metricBadge.text()).toBe('改动 8%')
  })

  it('does NOT render badge for records without humanEditMetrics', async () => {
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValueOnce([
      {
        id: 'rec-3',
        project_id: 'proj-1',
        chapter_id: 'chap-1',
        document_id: null,
        mode: 'full_pipeline',
        expert_id: null,
        direction: null,
        word_count: 3000,
        status: 'applied',
        created_at: '2026-11-04T08:00:00Z',
      },
    ])

    const wrapper = mount(GenerationHistoryPanel, {
      props: {
        projectId: 'proj-1',
        mode: 'novel',
        sequenceNumber: 1,
      },
    })

    await new Promise(r => setTimeout(r, 10))

    expect(wrapper.find('.generation-edit-metric').exists()).toBe(false)
  })

  it('does NOT render badge for non-applied records even if metrics exist', async () => {
    vi.spyOn(api, 'listChapterGenerations').mockResolvedValueOnce([
      {
        id: 'rec-4',
        project_id: 'proj-1',
        chapter_id: 'chap-1',
        document_id: null,
        mode: 'full_pipeline',
        expert_id: null,
        direction: null,
        word_count: 3000,
        status: 'candidate',
        created_at: '2026-11-04T08:00:00Z',
        human_edit_metrics: {
          edited: true,
          original_chars: 3000,
          final_chars: 3100,
          changed_chars: 400,
          edit_ratio: 0.13,
        },
      },
    ])

    const wrapper = mount(GenerationHistoryPanel, {
      props: {
        projectId: 'proj-1',
        mode: 'novel',
        sequenceNumber: 1,
      },
    })

    await new Promise(r => setTimeout(r, 10))

    expect(wrapper.find('.generation-edit-metric').exists()).toBe(false)
  })
})
