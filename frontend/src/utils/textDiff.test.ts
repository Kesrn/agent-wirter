import { describe, expect, it } from 'vitest'
import { buildTextComparisonDiff } from './textDiff'

describe('文本对照差异', () => {
  it('同时标记原文被替换部分和候选稿新增部分', () => {
    const diff = buildTextComparisonDiff('你好，世界。\n这是原文。', '你好，新的世界。\n这是候选文。')

    expect(diff.original.some(segment => segment.type === 'removed' && segment.text.includes('原'))).toBe(true)
    expect(diff.candidate.some(segment => segment.type === 'added' && segment.text.includes('候选'))).toBe(true)
    expect(diff.original.some(segment => segment.type === 'equal' && segment.text.includes('你好'))).toBe(true)
    expect(diff.candidate.some(segment => segment.type === 'equal' && segment.text.includes('世界'))).toBe(true)
  })

  it('相同文本不产生删除或新增标记', () => {
    const diff = buildTextComparisonDiff('同一段正文。', '同一段正文。')

    expect(diff.original).toEqual([{ type: 'equal', text: '同一段正文。' }])
    expect(diff.candidate).toEqual([{ type: 'equal', text: '同一段正文。' }])
  })
})
