/**
 * Edit metrics estimation utility.
 * Calculates approximate character-level changes and edit ratio between original and edited text.
 */

export interface EditMetricsResult {
  changedChars: number
  originalChars: number
  ratio: number
}

function countNonWhitespace(text: string): number {
  let count = 0
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)
    // Fast check for whitespace: space(32), \t(9), \n(10), \v(11), \f(12), \r(13), and full-width space(12288)
    if (code > 32 && code !== 12288 && code !== 160) {
      count++
    } else if (!/\s/.test(text[i])) {
      count++
    }
  }
  return count
}

function stripWhitespace(text: string): string {
  return text.replace(/\s+/g, '')
}

/**
 * 2-row space-efficient LCS length computation on two strings.
 */
function lcsLength(a: string, b: string): number {
  if (!a || !b) return 0
  const m = a.length
  const n = b.length
  if (m < n) return lcsLength(b, a)

  // Cap max comparison matrix to avoid CPU spikes on huge paragraphs
  if (m * n > 250000) {
    // If matrix is too large, use prefix/suffix heuristic
    let matched = 0
    let j = 0
    for (let i = 0; i < m && j < n; i++) {
      if (a[i] === b[j]) {
        matched++
        j++
      }
    }
    return matched
  }

  let prev = new Uint16Array(n + 1)
  let curr = new Uint16Array(n + 1)
  for (let i = 0; i < m; i++) {
    const chA = a.charCodeAt(i)
    for (let j = 0; j < n; j++) {
      if (chA === b.charCodeAt(j)) {
        curr[j + 1] = prev[j] + 1
      } else {
        const top = prev[j + 1]
        const left = curr[j]
        curr[j + 1] = top > left ? top : left
      }
    }
    const tmp = prev
    prev = curr
    curr = tmp
    curr.fill(0)
  }
  return prev[n]
}

/**
 * Align paragraphs using LCS of paragraph hashes/signatures.
 */
function alignParagraphs(origParas: string[], editedParas: string[]): Array<[number, number]> {
  const m = origParas.length
  const n = editedParas.length
  if (m === 0 || n === 0) return []

  const origClean = origParas.map(p => stripWhitespace(p))
  const editedClean = editedParas.map(p => stripWhitespace(p))

  // DP table for paragraph LCS
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1) as unknown as number[])
  for (let i = 0; i < m; i++) {
    for (let j = 0; j < n; j++) {
      if (origClean[i] && origClean[i] === editedClean[j]) {
        dp[i + 1][j + 1] = dp[i][j] + 1
      } else {
        dp[i + 1][j + 1] = Math.max(dp[i][j + 1], dp[i + 1][j])
      }
    }
  }

  // Backtrack to find aligned pairs
  const matches: Array<[number, number]> = []
  let i = m
  let j = n
  while (i > 0 && j > 0) {
    if (origClean[i - 1] && origClean[i - 1] === editedClean[j - 1]) {
      matches.push([i - 1, j - 1])
      i--
      j--
    } else if (dp[i - 1][j] >= dp[i][j - 1]) {
      i--
    } else {
      j--
    }
  }
  matches.reverse()
  return matches
}

/**
 * Compute character changes between two non-whitespace character sequences.
 */
function diffCharacterSequences(a: string, b: string): number {
  if (a === b) return 0
  if (!a) return b.length
  if (!b) return a.length

  // Strip common prefix
  let prefix = 0
  const maxPrefix = Math.min(a.length, b.length)
  while (prefix < maxPrefix && a.charCodeAt(prefix) === b.charCodeAt(prefix)) {
    prefix++
  }

  // Strip common suffix
  let suffix = 0
  const maxSuffix = Math.min(a.length - prefix, b.length - prefix)
  while (
    suffix < maxSuffix &&
    a.charCodeAt(a.length - 1 - suffix) === b.charCodeAt(b.length - 1 - suffix)
  ) {
    suffix++
  }

  const aMid = a.slice(prefix, a.length - suffix)
  const bMid = b.slice(prefix, b.length - suffix)

  if (!aMid) return bMid.length
  if (!bMid) return aMid.length

  const lcs = lcsLength(aMid, bMid)
  return Math.max(aMid.length - lcs, bMid.length - lcs)
}

/**
 * Estimate character-level edit metrics between original and edited text.
 *
 * Rules:
 * - Word counts consider non-whitespace characters only.
 * - Blank / whitespace-only differences do not count as changes.
 * - ratio = min(1, changedChars / max(originalChars, 1)).
 * - Paragraph-aligned for fast execution on long texts (< 100ms for 30k words).
 */
export function estimateEditMetrics(original: string, edited: string): EditMetricsResult {
  const originalChars = countNonWhitespace(original)
  const editedChars = countNonWhitespace(edited)

  // Edge cases
  if (originalChars === 0 && editedChars === 0) {
    return { changedChars: 0, originalChars: 0, ratio: 0 }
  }
  if (originalChars === 0) {
    return { changedChars: editedChars, originalChars: 0, ratio: 1 }
  }
  if (editedChars === 0) {
    return { changedChars: originalChars, originalChars, ratio: 1 }
  }
  if (original === edited) {
    return { changedChars: 0, originalChars, ratio: 0 }
  }

  // Check if non-whitespace characters are identical
  const origClean = stripWhitespace(original)
  const editedClean = stripWhitespace(edited)
  if (origClean === editedClean) {
    return { changedChars: 0, originalChars, ratio: 0 }
  }

  // Paragraph-based alignment
  const origParas = original.split('\n')
  const editedParas = edited.split('\n')
  const matches = alignParagraphs(origParas, editedParas)

  let changedChars = 0
  let lastOrig = 0
  let lastEdited = 0

  for (const [oIdx, eIdx] of matches) {
    // Unmatched block before this match
    if (oIdx > lastOrig || eIdx > lastEdited) {
      const oBlock = origParas.slice(lastOrig, oIdx).map(stripWhitespace).join('')
      const eBlock = editedParas.slice(lastEdited, eIdx).map(stripWhitespace).join('')
      changedChars += diffCharacterSequences(oBlock, eBlock)
    }
    lastOrig = oIdx + 1
    lastEdited = eIdx + 1
  }

  // Remaining unmatched block after last match
  if (lastOrig < origParas.length || lastEdited < editedParas.length) {
    const oBlock = origParas.slice(lastOrig).map(stripWhitespace).join('')
    const eBlock = editedParas.slice(lastEdited).map(stripWhitespace).join('')
    changedChars += diffCharacterSequences(oBlock, eBlock)
  }

  const rawRatio = changedChars / Math.max(originalChars, 1)
  const ratio = Math.min(1, Number(rawRatio.toFixed(4)))

  return {
    changedChars,
    originalChars,
    ratio,
  }
}

/**
 * Format success toast message with server-reported human_edit_metrics.
 */
export function formatApprovalSuccessToast(
  baseMessage: string,
  metrics?: import('../api/types').HumanEditMetrics | null,
): string {
  if (!metrics) return baseMessage
  if (metrics.edited === false) {
    return `${baseMessage}（未改动）`
  }
  const pct = Math.round(metrics.edit_ratio * 100)
  return `${baseMessage}。本次人工改动 ${pct}%（改动 ${metrics.changed_chars} 字 / 原稿 ${metrics.original_chars} 字）`
}
