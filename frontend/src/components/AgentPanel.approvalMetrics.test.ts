// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { formatApprovalSuccessToast } from '../utils/editMetrics'
import type { HumanEditMetrics } from '../api/types'

describe('Approval done event human_edit_metrics toast formatting', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('formats toast when human_edit_metrics is present with edited=true', () => {
    const baseMessage = '候选内容已采用并保存为草稿'
    const metrics: HumanEditMetrics = {
      method: 'difflib-v1',
      edited: true,
      original_chars: 3120,
      final_chars: 3300,
      changed_chars: 420,
      edit_ratio: 0.1346,
    }

    const toast = formatApprovalSuccessToast(baseMessage, metrics)
    expect(toast).toBe('候选内容已采用并保存为草稿。本次人工改动 13%（改动 420 字 / 原稿 3120 字）')
  })

  it('formats toast when human_edit_metrics is present with edited=false', () => {
    const baseMessage = '候选内容已采用并保存为草稿'
    const metrics: HumanEditMetrics = {
      method: 'difflib-v1',
      edited: false,
      original_chars: 3120,
      final_chars: 3120,
      changed_chars: 0,
      edit_ratio: 0,
    }

    const toast = formatApprovalSuccessToast(baseMessage, metrics)
    expect(toast).toBe('候选内容已采用并保存为草稿（未改动）')
  })

  it('formats toast when human_edit_metrics is null or missing (backward compatible)', () => {
    const baseMessage = '候选内容已采用并保存为草稿'
    expect(formatApprovalSuccessToast(baseMessage, null)).toBe('候选内容已采用并保存为草稿')
    expect(formatApprovalSuccessToast(baseMessage, undefined)).toBe('候选内容已采用并保存为草稿')
  })

  it('formats toast for chapter finalization with metrics', () => {
    const baseMessage = '本章已定稿，下一章创作会自动使用这版正文衔接'
    const metrics: HumanEditMetrics = {
      method: 'difflib-v1',
      edited: true,
      original_chars: 2000,
      final_chars: 2100,
      changed_chars: 100,
      edit_ratio: 0.05,
    }

    const toast = formatApprovalSuccessToast(baseMessage, metrics)
    expect(toast).toBe('本章已定稿，下一章创作会自动使用这版正文衔接。本次人工改动 5%（改动 100 字 / 原稿 2000 字）')
  })
})
