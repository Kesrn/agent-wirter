import { describe, expect, it } from 'vitest'
import { escapeHtml, renderInlineMarkdown, renderMarkdown, sanitizeMarkdownUrl } from './markdown'

describe('markdown renderer', () => {
  it('escapes HTML special characters', () => {
    expect(escapeHtml(`&<>"'`)).toBe('&amp;&lt;&gt;&quot;&#39;')
  })

  it('allows only safe markdown URL schemes and relative URLs', () => {
    expect(sanitizeMarkdownUrl('https://example.com?a=1&b=2')).toBe('https://example.com?a=1&amp;b=2')
    expect(sanitizeMarkdownUrl('mailto:hello@example.com')).toBe('mailto:hello@example.com')
    expect(sanitizeMarkdownUrl('/docs/page')).toBe('/docs/page')
    expect(sanitizeMarkdownUrl('./asset.png')).toBe('./asset.png')
    expect(sanitizeMarkdownUrl('../asset.png')).toBe('../asset.png')
    expect(sanitizeMarkdownUrl('#section')).toBe('#section')
  })

  it('rejects unsafe markdown URL schemes and encoded protocol variants', () => {
    expect(sanitizeMarkdownUrl('javascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('vbscript:msgbox(1)')).toBe('')
    expect(sanitizeMarkdownUrl('data:text/html,<svg onload=alert(1)>')).toBe('')
    expect(sanitizeMarkdownUrl('%6Aavascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('java%0Ascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('https://example.com/a b')).toBe('')
  })

  it('escapes raw HTML when rendering markdown', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n<img src=x onerror=alert(1)>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img src=x')
  })

  it('renders safe links and strips unsafe links', () => {
    expect(renderInlineMarkdown('[OpenAI](https://openai.com)')).toBe(
      '<a href="https://openai.com" target="_blank" rel="noreferrer noopener">OpenAI</a>',
    )
    expect(renderInlineMarkdown('[bad](javascript:alert(1))')).toBe('bad')
  })

  it('renders images with referrer protection', () => {
    expect(renderInlineMarkdown('![Logo](https://example.com/logo.png)')).toBe(
      '<img src="https://example.com/logo.png" alt="Logo" referrerpolicy="no-referrer">',
    )
  })

  it('does not replace user-authored legacy token text', () => {
    const html = renderInlineMarkdown('@@MDTOKEN0@@ and [safe](https://example.com)')
    expect(html).toContain('@@MDTOKEN0@@')
    expect(html).toContain('<a href="https://example.com" target="_blank" rel="noreferrer noopener">safe</a>')
  })

  it('renders common markdown blocks', () => {
    const html = renderMarkdown([
      '# Title',
      '',
      '> Quote',
      '',
      '- one',
      '- [x] done',
      '',
      '```',
      '<tag>',
      '```',
      '',
      '| A | B |',
      '| --- | ---: |',
      '| **x** | `y` |',
      '',
      '---',
    ].join('\n'))

    expect(html).toContain('<h1>Title</h1>')
    expect(html).toContain('<blockquote><p>Quote</p></blockquote>')
    expect(html).toContain('<ul class="task-list">')
    expect(html).toContain('<input type="checkbox" disabled checked>')
    expect(html).toContain('<pre><code>&lt;tag&gt;</code></pre>')
    expect(html).toContain('<table>')
    expect(html).toContain('<strong>x</strong>')
    expect(html).toContain('<code>y</code>')
    expect(html).toContain('<hr>')
  })
})
