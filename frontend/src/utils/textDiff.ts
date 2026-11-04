export type TextDiffSegmentType = 'equal' | 'removed' | 'added'

export interface TextDiffSegment {
  type: TextDiffSegmentType
  text: string
}

export interface TextComparisonDiff {
  original: TextDiffSegment[]
  candidate: TextDiffSegment[]
}

interface SequenceDiffOperation {
  type: TextDiffSegmentType
  text: string
}

function appendSegment(segments: TextDiffSegment[], type: TextDiffSegmentType, text: string) {
  if (!text) return
  const previous = segments[segments.length - 1]
  if (previous?.type === type) previous.text += text
  else segments.push({ type, text })
}

function splitLines(text: string): string[] {
  return text.match(/[^\n]*\n|[^\n]+/g) ?? []
}

/** LCS 适合做段落级定位，避免对整篇章节建立字符级矩阵。 */
function sequenceDiff(left: string[], right: string[]): SequenceDiffOperation[] {
  if (!left.length) return right.length ? [{ type: 'added', text: right.join('') }] : []
  if (!right.length) return [{ type: 'removed', text: left.join('') }]

  // 极端长文本不建立过大的矩阵，退化为整段标记，保证审核弹窗不会卡死。
  if (left.length * right.length > 250_000) {
    return [
      { type: 'removed', text: left.join('') },
      { type: 'added', text: right.join('') },
    ]
  }

  const matrix = Array.from(
    { length: left.length + 1 },
    () => new Uint32Array(right.length + 1),
  )
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      matrix[i][j] = left[i] === right[j]
        ? matrix[i + 1][j + 1] + 1
        : Math.max(matrix[i + 1][j], matrix[i][j + 1])
    }
  }

  const operations: SequenceDiffOperation[] = []
  let i = 0
  let j = 0
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      appendSegment(operations, 'equal', left[i])
      i += 1
      j += 1
    } else if (matrix[i + 1][j] >= matrix[i][j + 1]) {
      appendSegment(operations, 'removed', left[i])
      i += 1
    } else {
      appendSegment(operations, 'added', right[j])
      j += 1
    }
  }
  while (i < left.length) appendSegment(operations, 'removed', left[i++])
  while (j < right.length) appendSegment(operations, 'added', right[j++])
  return operations
}

function boundedCharDiff(left: string, right: string): SequenceDiffOperation[] {
  if (!left) return right ? [{ type: 'added', text: right }] : []
  if (!right) return [{ type: 'removed', text: left }]

  const leftChars = Array.from(left)
  const rightChars = Array.from(right)
  const prefixLimit = Math.min(leftChars.length, rightChars.length)
  let prefix = 0
  while (prefix < prefixLimit && leftChars[prefix] === rightChars[prefix]) prefix += 1

  let suffix = 0
  while (
    suffix < prefixLimit - prefix
    && leftChars[leftChars.length - suffix - 1] === rightChars[rightChars.length - suffix - 1]
  ) suffix += 1

  const leftMiddle = leftChars.slice(prefix, leftChars.length - suffix)
  const rightMiddle = rightChars.slice(prefix, rightChars.length - suffix)
  const operations: SequenceDiffOperation[] = []
  appendSegment(operations, 'equal', leftChars.slice(0, prefix).join(''))

  // 单段修改过大时只标记变化区，不让超长章节阻塞主线程。
  if (leftMiddle.length * rightMiddle.length > 250_000) {
    appendSegment(operations, 'removed', leftMiddle.join(''))
    appendSegment(operations, 'added', rightMiddle.join(''))
  } else {
    const matrix = Array.from(
      { length: leftMiddle.length + 1 },
      () => new Uint32Array(rightMiddle.length + 1),
    )
    for (let i = leftMiddle.length - 1; i >= 0; i -= 1) {
      for (let j = rightMiddle.length - 1; j >= 0; j -= 1) {
        matrix[i][j] = leftMiddle[i] === rightMiddle[j]
          ? matrix[i + 1][j + 1] + 1
          : Math.max(matrix[i + 1][j], matrix[i][j + 1])
      }
    }
    let i = 0
    let j = 0
    while (i < leftMiddle.length && j < rightMiddle.length) {
      if (leftMiddle[i] === rightMiddle[j]) {
        appendSegment(operations, 'equal', leftMiddle[i])
        i += 1
        j += 1
      } else if (matrix[i + 1][j] >= matrix[i][j + 1]) {
        appendSegment(operations, 'removed', leftMiddle[i++])
      } else {
        appendSegment(operations, 'added', rightMiddle[j++])
      }
    }
    while (i < leftMiddle.length) appendSegment(operations, 'removed', leftMiddle[i++])
    while (j < rightMiddle.length) appendSegment(operations, 'added', rightMiddle[j++])
  }

  appendSegment(operations, 'equal', leftChars.slice(leftChars.length - suffix).join(''))
  return operations
}

export function buildTextComparisonDiff(original: string, candidate: string): TextComparisonDiff {
  if (original === candidate) {
    return {
      original: original ? [{ type: 'equal', text: original }] : [],
      candidate: candidate ? [{ type: 'equal', text: candidate }] : [],
    }
  }

  const originalSegments: TextDiffSegment[] = []
  const candidateSegments: TextDiffSegment[] = []
  const lineOperations = sequenceDiff(splitLines(original), splitLines(candidate))

  let index = 0
  while (index < lineOperations.length) {
    const operation = lineOperations[index]
    if (operation.type === 'equal') {
      appendSegment(originalSegments, 'equal', operation.text)
      appendSegment(candidateSegments, 'equal', operation.text)
      index += 1
      continue
    }

    let removedText = ''
    let addedText = ''
    while (index < lineOperations.length && lineOperations[index].type !== 'equal') {
      const changed = lineOperations[index]
      if (changed.type === 'removed') removedText += changed.text
      if (changed.type === 'added') addedText += changed.text
      index += 1
    }

    for (const changed of boundedCharDiff(removedText, addedText)) {
      if (changed.type === 'equal') {
        appendSegment(originalSegments, 'equal', changed.text)
        appendSegment(candidateSegments, 'equal', changed.text)
      } else if (changed.type === 'removed') {
        appendSegment(originalSegments, 'removed', changed.text)
      } else {
        appendSegment(candidateSegments, 'added', changed.text)
      }
    }
  }

  return { original: originalSegments, candidate: candidateSegments }
}
