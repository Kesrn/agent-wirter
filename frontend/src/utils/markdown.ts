export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function decodeForProtocolCheck(value: string): string {
  let decoded = value
  for (let i = 0; i < 3; i += 1) {
    try {
      const next = decodeURIComponent(decoded)
      if (next === decoded) break
      decoded = next
    } catch {
      break
    }
  }
  return decoded
}

export function sanitizeMarkdownUrl(rawUrl: string): string {
  const value = rawUrl.trim().replace(/^<|>$/g, '')
  if (!value || /[\u0000-\u001F\u007F\s]/.test(value)) return ''

  const decoded = decodeForProtocolCheck(value).trim()
  if (/[\u0000-\u001F\u007F\s]/.test(decoded)) return ''
  if (/^(?:javascript|vbscript|data):/i.test(decoded)) return ''

  const allowed =
    /^(?:https?:|mailto:|tel:)/i.test(value) ||
    value.startsWith('/') ||
    value.startsWith('#') ||
    value.startsWith('./') ||
    value.startsWith('../')

  return allowed ? escapeHtml(value) : ''
}

export function renderInlineMarkdown(raw: string): string {
  const tokens: string[] = []
  const urlPattern = '((?:[^\\s()]+|\\([^()\\s]*\\))+)(?:\\s+"[^"]*")?'
  const stash = (html: string) => {
    const token = `\u0000MDTOKEN${tokens.length}\u0000`
    tokens.push(html)
    return token
  }

  let text = raw
    .replace(/`([^`\n]+)`/g, (_match, code: string) => stash(`<code>${escapeHtml(code)}</code>`))
    .replace(new RegExp(`!\\[([^\\]]*)\\]\\(${urlPattern}\\)`, 'g'), (_match, alt: string, url: string) => {
      const safeUrl = sanitizeMarkdownUrl(url)
      return safeUrl ? stash(`<img src="${safeUrl}" alt="${escapeHtml(alt)}" referrerpolicy="no-referrer">`) : escapeHtml(alt)
    })
    .replace(new RegExp(`\\[([^\\]]+)\\]\\(${urlPattern}\\)`, 'g'), (_match, label: string, url: string) => {
      const safeUrl = sanitizeMarkdownUrl(url)
      return safeUrl
        ? stash(`<a href="${safeUrl}" target="_blank" rel="noreferrer noopener">${escapeHtml(label)}</a>`)
        : escapeHtml(label)
    })

  text = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>')

  tokens.forEach((html, index) => {
    text = text.split(escapeHtml(`\u0000MDTOKEN${index}\u0000`)).join(html)
  })
  return text
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim()
  if (!trimmed.includes('|')) return []
  return trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line)
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell))
}

function isMarkdownBlockStart(lines: string[], index: number): boolean {
  const line = lines[index] ?? ''
  const next = lines[index + 1] ?? ''
  return (
    /^ {0,3}```/.test(line) ||
    /^ {0,3}#{1,6}\s+/.test(line) ||
    /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
    /^ {0,3}>\s?/.test(line) ||
    /^ {0,3}[-*+]\s+/.test(line) ||
    /^ {0,3}\d+\.\s+/.test(line) ||
    (line.includes('|') && isTableSeparator(next))
  )
}

function renderMarkdownTable(lines: string[], start: number): { html: string; nextIndex: number } {
  const header = splitTableRow(lines[start])
  const separator = splitTableRow(lines[start + 1])
  const alignAttr = (cell: string) => {
    const align = cell.startsWith(':') && cell.endsWith(':')
      ? 'center'
      : cell.endsWith(':')
        ? 'right'
        : cell.startsWith(':')
          ? 'left'
          : ''
    return align ? ` style="text-align:${align}"` : ''
  }
  const head = header
    .map((cell, index) => `<th${alignAttr(separator[index] ?? '')}>${renderInlineMarkdown(cell)}</th>`)
    .join('')
  const rows: string[] = []
  let i = start + 2
  while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
    const cells = splitTableRow(lines[i])
    rows.push(`<tr>${header.map((_cell, index) => `<td${alignAttr(separator[index] ?? '')}>${renderInlineMarkdown(cells[index] ?? '')}</td>`).join('')}</tr>`)
    i += 1
  }
  return {
    html: `<table><thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table>`,
    nextIndex: i,
  }
}

function renderMarkdownList(lines: string[], start: number, ordered: boolean): { html: string; nextIndex: number } {
  const items: string[] = []
  const regex = ordered ? /^ {0,3}\d+\.\s+(.+)$/ : /^ {0,3}[-*+]\s+(.+)$/
  let hasTask = false
  let i = start
  while (i < lines.length) {
    const match = lines[i].match(regex)
    if (!match) break
    let content = match[1]
    let checkbox = ''
    const task = content.match(/^\[([ xX])\]\s+(.+)$/)
    if (task) {
      hasTask = true
      content = task[2]
      checkbox = `<input type="checkbox" disabled${task[1].toLowerCase() === 'x' ? ' checked' : ''}> `
    }
    items.push(`<li${checkbox ? ' class="task-list-item"' : ''}>${checkbox}${renderInlineMarkdown(content)}</li>`)
    i += 1
  }
  const tag = ordered ? 'ol' : 'ul'
  const classAttr = hasTask ? ' class="task-list"' : ''
  return { html: `<${tag}${classAttr}>${items.join('')}</${tag}>`, nextIndex: i }
}

export function renderMarkdown(markdown: string): string {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n')
  const blocks: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i += 1
      continue
    }

    const fence = line.match(/^ {0,3}```\s*([\w-]+)?\s*$/)
    if (fence) {
      const codeLines: string[] = []
      i += 1
      while (i < lines.length && !/^ {0,3}```\s*$/.test(lines[i])) {
        codeLines.push(lines[i])
        i += 1
      }
      if (i < lines.length) i += 1
      blocks.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
      continue
    }

    if (line.includes('|') && isTableSeparator(lines[i + 1] ?? '')) {
      const table = renderMarkdownTable(lines, i)
      blocks.push(table.html)
      i = table.nextIndex
      continue
    }

    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/)
    if (heading) {
      const level = heading[1].length
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`)
      i += 1
      continue
    }

    if (/^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push('<hr>')
      i += 1
      continue
    }

    if (/^ {0,3}>\s?/.test(line)) {
      const quoteLines: string[] = []
      while (i < lines.length && /^ {0,3}>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^ {0,3}>\s?/, ''))
        i += 1
      }
      blocks.push(`<blockquote>${renderMarkdown(quoteLines.join('\n'))}</blockquote>`)
      continue
    }

    if (/^ {0,3}[-*+]\s+/.test(line)) {
      const list = renderMarkdownList(lines, i, false)
      blocks.push(list.html)
      i = list.nextIndex
      continue
    }

    if (/^ {0,3}\d+\.\s+/.test(line)) {
      const list = renderMarkdownList(lines, i, true)
      blocks.push(list.html)
      i = list.nextIndex
      continue
    }

    const paragraph: string[] = []
    while (i < lines.length && lines[i].trim() && !isMarkdownBlockStart(lines, i)) {
      paragraph.push(lines[i].trim())
      i += 1
    }
    if (paragraph.length) {
      blocks.push(`<p>${paragraph.map(renderInlineMarkdown).join('<br>')}</p>`)
    } else {
      i += 1
    }
  }

  return blocks.join('\n')
}
