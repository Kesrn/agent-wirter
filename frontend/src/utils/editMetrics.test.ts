import { describe, expect, it } from 'vitest'
import { estimateEditMetrics, formatApprovalSuccessToast } from './editMetrics'

describe('estimateEditMetrics', () => {
  it('returns 0 for identical texts', () => {
    const text = '第一章 风起云涌\n清晨的古道上，马蹄声撕破了薄雾。'
    const result = estimateEditMetrics(text, text)
    expect(result).toEqual({
      changedChars: 0,
      originalChars: 23,
      ratio: 0,
    })
  })

  it('ignores whitespace-only differences', () => {
    const original = '第一章 风起云涌\n\n清晨的古道上，马蹄声撕破了薄雾。'
    const edited = '  第一章   风起云涌\n清晨的古道上，马蹄声撕破了薄雾。  \n'
    const result = estimateEditMetrics(original, edited)
    expect(result.changedChars).toBe(0)
    expect(result.ratio).toBe(0)
    expect(result.originalChars).toBe(23)
  })

  it('correctly handles small changes in text', () => {
    const original = '清晨的古道上，马蹄声撕破了薄雾。'
    const edited = '清晨的古道上，马蹄声撕破了浓雾。' // '薄' -> '浓' (1 char changed)
    const result = estimateEditMetrics(original, edited)
    expect(result.changedChars).toBe(1)
    expect(result.originalChars).toBe(16)
    expect(result.ratio).toBeCloseTo(1 / 16, 4)
  })

  it('correctly handles appended content', () => {
    const original = '清晨的古道上，马蹄声撕破了薄雾。'
    const edited = '清晨的古道上，马蹄声撕破了薄雾。远处传来钟声。' // appended 7 non-ws chars
    const result = estimateEditMetrics(original, edited)
    expect(result.changedChars).toBe(7)
    expect(result.originalChars).toBe(16)
    expect(result.ratio).toBeCloseTo(7 / 16, 4)
  })

  it('correctly handles prepended content', () => {
    const original = '马蹄声撕破了薄雾。'
    const edited = '清晨的古道上，马蹄声撕破了薄雾。'
    const result = estimateEditMetrics(original, edited)
    expect(result.changedChars).toBe(7)
    expect(result.originalChars).toBe(9)
    expect(result.ratio).toBeCloseTo(7 / 9, 4)
  })

  it('handles completely rewritten text: ratio is close to 1 and does not exceed 1', () => {
    const original = '第一章 风起云涌\n清晨的古道上，马蹄声撕破了薄雾。少年勒住缰绳，举目远眺。'
    const edited = '天地初开，万物混沌，上古纪元的一场神魔大战改变了世间格局。'
    const result = estimateEditMetrics(original, edited)
    expect(result.ratio).toBeLessThanOrEqual(1)
    expect(result.ratio).toBeGreaterThan(0.8)
  })

  it('handles empty inputs properly', () => {
    expect(estimateEditMetrics('', '')).toEqual({
      changedChars: 0,
      originalChars: 0,
      ratio: 0,
    })
    expect(estimateEditMetrics('   \n\t', '')).toEqual({
      changedChars: 0,
      originalChars: 0,
      ratio: 0,
    })
    expect(estimateEditMetrics('', '新写的一段文字')).toEqual({
      changedChars: 7,
      originalChars: 0,
      ratio: 1,
    })
    expect(estimateEditMetrics('原本的文字', '')).toEqual({
      changedChars: 5,
      originalChars: 5,
      ratio: 1,
    })
  })

  it('executes under 100ms for 30,000-character text with 10% modifications', () => {
    // Generate 120 paragraphs with ~250 characters each (~30,000 non-whitespace characters)
    const baseParagraph = '夜色苍茫如水，北境长城的垛口凝结着厚厚的白霜。戍卒们紧握长矛，目光警惕地注视着黑魆魆的荒原。风声呼啸，像是古老战歌在天地间回荡。远方狼烟突起，三道刺目的火光冲上云霄。校尉长剑出鞘，下达了最高警戒令。各营弓弩手迅速登城，箭矢上弦，机括铮鸣作响。城门内侧重骑兵列队完毕，玄甲铁骑肃穆如铁铸的丛林。沉重的攻城木撞击声自关隘下方隐隐传来，震得砖石缝隙中的尘土簌簌落下。烽火照亮了半边夜空，这注定是一场载入史册的生死决战。血与火的序幕就此拉开，无人知晓明朝黎明属于何方。'
    const paragraphs: string[] = []
    for (let i = 0; i < 130; i++) {
      paragraphs.push(`【第${i + 1}节】${baseParagraph}`)
    }
    const original = paragraphs.join('\n\n')

    // Modify 10% of paragraphs (13 paragraphs changed)
    const editedParagraphs = [...paragraphs]
    for (let i = 0; i < 13; i++) {
      const idx = i * 10
      editedParagraphs[idx] = `【第${idx + 1}节·修订】${baseParagraph.replace('夜色苍茫如水', '晨曦微露如霜').replace('生死决战', '宿命鏖战')}`
    }
    const edited = editedParagraphs.join('\n\n')

    expect(original.replace(/\s+/g, '').length).toBeGreaterThan(30000)

    const startTime = performance.now()
    const result = estimateEditMetrics(original, edited)
    const duration = performance.now() - startTime

    expect(result.originalChars).toBeGreaterThan(30000)
    expect(result.changedChars).toBeGreaterThan(0)
    expect(result.ratio).toBeGreaterThan(0)
    expect(result.ratio).toBeLessThan(0.15) // around 10%
    expect(duration).toBeLessThan(100) // Performance requirement: < 100ms
  })

  describe('formatApprovalSuccessToast', () => {
    const base = '候选内容已采用并保存为草稿'

    it('returns base message when metrics is missing or null (backward compatible)', () => {
      expect(formatApprovalSuccessToast(base)).toBe(base)
      expect(formatApprovalSuccessToast(base, null)).toBe(base)
      expect(formatApprovalSuccessToast(base, undefined)).toBe(base)
    })

    it('returns unedited message when metrics.edited is false', () => {
      const metrics = {
        edited: false,
        original_chars: 1000,
        final_chars: 1000,
        changed_chars: 0,
        edit_ratio: 0,
      }
      expect(formatApprovalSuccessToast(base, metrics)).toBe(`${base}（未改动）`)
    })

    it('returns formatted metrics when metrics.edited is true', () => {
      const metrics = {
        edited: true,
        original_chars: 3120,
        final_chars: 3300,
        changed_chars: 420,
        edit_ratio: 0.1346,
      }
      expect(formatApprovalSuccessToast(base, metrics)).toBe(
        `${base}。本次人工改动 13%（改动 420 字 / 原稿 3120 字）`,
      )
    })
  })
})
