import { describe, expect, it } from 'vitest'
import { displayChapterNumber, formatNovelChapterTitle, stripChapterNumber } from './chapterTitle'

describe('chapter title helpers', () => {
  it('strips duplicated chapter numbers from imported titles', () => {
    expect(stripChapterNumber('第1章 世界大变')).toBe('世界大变')
    expect(stripChapterNumber('第二章 转折')).toBe('转折')
    expect(stripChapterNumber('Chapter 12: The Gate')).toBe('The Gate')
  })

  it('uses the title chapter number for old shifted imports', () => {
    expect(displayChapterNumber(2, '第1章 世界大变')).toBe(1)
    expect(displayChapterNumber(3, '第二章 转折')).toBe(2)
    expect(displayChapterNumber(8, '根本停不下来')).toBe(8)
  })

  it('formats the editor title without duplicate numbering', () => {
    expect(formatNovelChapterTitle(2, '第1章 世界大变')).toBe('第1章 · 世界大变')
    expect(formatNovelChapterTitle(2, '世界大变')).toBe('第2章 · 世界大变')
  })
})
