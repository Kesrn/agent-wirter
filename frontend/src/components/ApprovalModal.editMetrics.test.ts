// @vitest-environment happy-dom
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ApprovalModal from './ApprovalModal.vue'

describe('ApprovalModal live edit metrics and debouncing', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('displays approximate edit metrics when content differs from originalContent', () => {
    const original = '清晨的古道上，马蹄声撕破了薄雾。'
    const candidate = '清晨的古道上，马蹄声撕破了浓雾。' // 1 char changed out of 16 (6.25% -> 6%)

    const wrapper = mount(ApprovalModal, {
      props: {
        show: true,
        content: candidate,
        originalContent: original,
        mode: 'full_pipeline',
        hasHITL: true,
        projectMode: 'novel',
      },
    })

    const banner = wrapper.find('.edit-metrics-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('已修改约 1 字（约 6%）')
    expect(banner.text()).toContain('最终以保存后的统计为准')
  })

  it('does NOT display edit metrics when unedited and not in edit mode', () => {
    const text = '相同的内容'
    const wrapper = mount(ApprovalModal, {
      props: {
        show: true,
        content: text,
        originalContent: text,
        mode: 'full_pipeline',
        hasHITL: true,
        projectMode: 'novel',
      },
    })

    const banner = wrapper.find('.edit-metrics-banner')
    expect(banner.exists()).toBe(false)
  })

  it('updates metrics with 200ms debounce when modifying text in edit mode', async () => {
    const original = '这是原本的内容'
    const candidate = '这是原本的内容'

    const wrapper = mount(ApprovalModal, {
      props: {
        show: true,
        content: candidate,
        originalContent: original,
        mode: 'full_pipeline',
        hasHITL: true,
        projectMode: 'novel',
      },
    })

    // Initially unedited, no banner
    expect(wrapper.find('.edit-metrics-banner').exists()).toBe(false)

    // Click "手动修改" to enter edit mode
    const editBtn = wrapper.find('.edit-toggle-btn')
    await editBtn.trigger('click')
    await nextTick()

    const textarea = wrapper.find('textarea.modal-textarea')
    expect(textarea.exists()).toBe(true)
    // In edit mode with 0 changes, shows 0
    expect(wrapper.find('.edit-metrics-banner').text()).toContain('已修改约 0 字（约 0%）')

    // Input new text: "这是修改后的全新内容"
    await textarea.setValue('这是修改后的全新内容')
    await textarea.trigger('input')

    // Before debounce (e.g. 100ms), metrics should NOT have updated yet
    vi.advanceTimersByTime(100)
    await nextTick()
    expect(wrapper.find('.edit-metrics-banner').text()).toContain('已修改约 0 字（约 0%）')

    // After 200ms total, debounce completes and metrics update
    vi.advanceTimersByTime(110)
    await nextTick()

    const banner = wrapper.find('.edit-metrics-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('已修改约')
    expect(banner.text()).toContain('最终以保存后的统计为准')
  })
})
