const CHINESE_DIGITS: Record<string, number> = {
  零: 0,
  〇: 0,
  一: 1,
  二: 2,
  两: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
}

const CHINESE_UNITS: Record<string, number> = {
  十: 10,
  百: 100,
  千: 1000,
  万: 10000,
}

function normalizeDigits(text: string): string {
  return text.replace(/[０-９]/g, ch => String(ch.charCodeAt(0) - 0xff10))
}

function parseChineseNumber(text: string): number | null {
  let total = 0
  let section = 0
  let number = 0
  let hasValue = false

  for (const ch of text) {
    if (ch in CHINESE_DIGITS) {
      number = CHINESE_DIGITS[ch]
      hasValue = true
      continue
    }
    const unit = CHINESE_UNITS[ch]
    if (!unit) return null
    hasValue = true
    if (unit === 10000) {
      total += (section + number || 1) * unit
      section = 0
      number = 0
    } else {
      section += (number || 1) * unit
      number = 0
    }
  }

  if (!hasValue) return null
  return total + section + number
}

function parseChapterNumber(value: string): number | null {
  const normalized = normalizeDigits(value.trim())
  if (/^\d+$/.test(normalized)) {
    const parsed = Number.parseInt(normalized, 10)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }
  const parsed = parseChineseNumber(normalized)
  return parsed && parsed > 0 ? parsed : null
}

export function extractChapterNumber(title: string): number | null {
  const normalized = normalizeDigits(title.trim())
  const numbered = normalized.match(/^第\s*([0-9一二三四五六七八九十百千万两〇零]+)\s*[章节卷回部集篇]/)
  if (numbered) return parseChapterNumber(numbered[1])

  const english = normalized.match(/^Chapter\s+(\d+)/i)
  if (english) return parseChapterNumber(english[1])

  const simple = normalized.match(/^(\d{1,4})\s*[.、．]/)
  if (simple) return parseChapterNumber(simple[1])

  return null
}

export function stripChapterNumber(title: string): string {
  return title
    .replace(/^第\s*[0-9０-９一二三四五六七八九十百千万两〇零]+\s*[章节卷回部集篇]\s*[：:、.．\-—]?\s*/, '')
    .replace(/^Chapter\s+\d+\s*[：:、.．\-—]?\s*/i, '')
    .replace(/^\d{1,4}\s*[.、．]\s*/, '')
    .trim()
}

export function displayChapterNumber(sequenceNumber: number, title: string): number {
  return extractChapterNumber(title) ?? sequenceNumber
}

export function formatNovelChapterTitle(sequenceNumber: number, title: string): string {
  const displayNumber = displayChapterNumber(sequenceNumber, title)
  const displayTitle = stripChapterNumber(title)
  return displayTitle ? `第${displayNumber}章 · ${displayTitle}` : `第${displayNumber}章`
}
