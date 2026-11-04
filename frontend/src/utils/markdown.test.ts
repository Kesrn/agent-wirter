// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import {
  escapeHtml,
  renderInlineMarkdown,
  renderMarkdown,
  sanitizeMarkdownUrl,
  getBackendOrigin,
  resolveMediaUrl,
  _setPurifierDisabledForTesting,
} from './markdown'

function ensureHappyDomNodeName() {
  if (typeof window === 'undefined' || !window.Node?.prototype) return
  const desc = Object.getOwnPropertyDescriptor(window.Node.prototype, 'nodeName')
  if (desc && desc.get) {
    const dummy = window.document?.createElement('div')
    if (dummy && desc.get.call(dummy) === '') {
      Object.defineProperty(window.Node.prototype, 'nodeName', {
        get() {
          switch (this.nodeType) {
            case 1:
              return this.tagName || ''
            case 3:
              return '#text'
            case 8:
              return '#comment'
            case 9:
              return '#document'
            case 11:
              return '#document-fragment'
            default:
              return ''
          }
        },
        configurable: true,
      })
    }
  }
}

ensureHappyDomNodeName()

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

  it('rejects unsafe markdown URL schemes, protocol-relative URLs, and encoded variants', () => {
    expect(sanitizeMarkdownUrl('javascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('vbscript:msgbox(1)')).toBe('')
    expect(sanitizeMarkdownUrl('data:text/html,<svg onload=alert(1)>')).toBe('')
    expect(sanitizeMarkdownUrl('%6Aavascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('java%0Ascript:alert(1)')).toBe('')
    expect(sanitizeMarkdownUrl('https://example.com/a b')).toBe('')
    expect(sanitizeMarkdownUrl('//evil.com')).toBe('')
    expect(sanitizeMarkdownUrl('//evil.com/a.png')).toBe('')
    expect(sanitizeMarkdownUrl('/\\evil.com')).toBe('')
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

  it('renders common markdown blocks with DOMPurify sanitization', () => {
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
    expect(html).toContain('<li class="task-list-item">☑ done</li>')
    expect(html).not.toContain('<input')
    expect(html).toContain('<pre><code>&lt;tag&gt;</code></pre>')
    expect(html).toContain('<table>')
    expect(html).toContain('<td align="right">')
    expect(html).toContain('<strong>x</strong>')
    expect(html).toContain('<code>y</code>')
    expect(html).toContain('<hr>')
  })

  describe('security and XSS sanitization (Task F2)', () => {
    it('disallows raw dangerous tags injected in markdown', () => {
      const inputs = [
        '<img src=x onerror=alert(1)>',
        '<script>alert(1)</script>',
        '<svg onload=alert(1)>',
        '<iframe src="https://evil.com"></iframe>',
      ]
      for (const input of inputs) {
        const output = renderMarkdown(input)
        const doc = new DOMParser().parseFromString(output, 'text/html')
        // No executable elements created in DOM
        expect(doc.querySelector('script')).toBeNull()
        expect(doc.querySelector('svg')).toBeNull()
        expect(doc.querySelector('iframe')).toBeNull()
        const img = doc.querySelector('img')
        if (img) {
          expect(img.getAttribute('onerror')).toBeNull()
        }
      }
    })

    it('neutralizes unsafe pseudo-protocols in links and images', () => {
      const jsLink = renderMarkdown('[点我](javascript:alert(1))')
      const linkDoc = new DOMParser().parseFromString(jsLink, 'text/html')
      const a = linkDoc.querySelector('a')
      if (a) {
        expect(a.getAttribute('href')).not.toMatch(/^javascript:/i)
      }

      const jsImg = renderMarkdown('![x](javascript:alert(1))')
      const imgDoc = new DOMParser().parseFromString(jsImg, 'text/html')
      const img = imgDoc.querySelector('img')
      if (img) {
        expect(img.getAttribute('src')).not.toMatch(/^javascript:/i)
      }

      const dataLink = renderMarkdown('[点我](data:text/html;base64,PHNjcmlwdD4=)')
      const dataDoc = new DOMParser().parseFromString(dataLink, 'text/html')
      const dataA = dataDoc.querySelector('a')
      if (dataA) {
        expect(dataA.getAttribute('href')).not.toMatch(/^data:/i)
      }
    })

    it('blocks obfuscated and encoded pseudo-protocols', () => {
      const hexEncoded = renderMarkdown('[x](jav&#x61;script:alert(1))')
      const hexDoc = new DOMParser().parseFromString(hexEncoded, 'text/html')
      const hexA = hexDoc.querySelector('a')
      if (hexA) {
        expect(hexA.getAttribute('href')).not.toMatch(/javascript/i)
      }

      const decEncoded = renderMarkdown('[x](&#106;avascript:alert(1))')
      const decDoc = new DOMParser().parseFromString(decEncoded, 'text/html')
      const decA = decDoc.querySelector('a')
      if (decA) {
        expect(decA.getAttribute('href')).not.toMatch(/javascript/i)
      }

      const newlineVariant = renderMarkdown('[x](java\nscript:alert(1))')
      const nlDoc = new DOMParser().parseFromString(newlineVariant, 'text/html')
      // Newlines break link parsing entirely, rendering as text without an <a> tag
      expect(nlDoc.querySelector('a')).toBeNull()
    })

    it('blocks attribute injection attempts', () => {
      const imgAttrInject = renderMarkdown('![a" onerror="alert(1)](/media/x.png)')
      const imgDoc = new DOMParser().parseFromString(imgAttrInject, 'text/html')
      const img = imgDoc.querySelector('img')!
      expect(img).not.toBeNull()
      expect(img.getAttribute('onerror')).toBeNull()
      expect(img.getAttribute('src')).toBe('/media/x.png')
      expect(img.getAttribute('referrerpolicy')).toBe('no-referrer')

      const linkAttrInject = renderMarkdown('[a](http://x.com" onmouseover="alert(1))')
      const linkDoc = new DOMParser().parseFromString(linkAttrInject, 'text/html')
      const a = linkDoc.querySelector('a')
      if (a) {
        expect(a.getAttribute('onmouseover')).toBeNull()
      }
    })

    it('preserves /media/... relative image format and referrerpolicy', () => {
      const html = renderMarkdown('![插图](/media/illustrations/scene1.png "场景一")')
      expect(html).toContain('<img src="/media/illustrations/scene1.png" alt="插图" referrerpolicy="no-referrer">')
    })

    it('enforces rel="noopener noreferrer" on external links with target', () => {
      const html = renderMarkdown('[外部参考](https://example.com/reference)')
      expect(html).toContain('target="_blank"')
      expect(html).toContain('rel="noopener noreferrer"')
    })

    it('strictly forbids protocol-relative URLs (//...) in links and images while keeping valid paths', () => {
      // Protocol-relative URLs must not generate <a> or <img> tags
      const protoRelLink = renderMarkdown('[x](//evil.com)')
      expect(protoRelLink).not.toContain('<a')
      expect(protoRelLink).not.toContain('evil.com</a>')

      const protoRelImg = renderMarkdown('![x](//evil.com/a.png)')
      expect(protoRelImg).not.toContain('<img')
      expect(protoRelImg).toContain('x')

      // Safe paths must continue to work normally
      expect(renderMarkdown('[link](/path)')).toContain('<a href="/path"')
      expect(renderMarkdown('[link](#anchor)')).toContain('<a href="#anchor"')
      expect(renderMarkdown('[link](./a)')).toContain('<a href="./a"')
      expect(renderMarkdown('[link](../a)')).toContain('<a href="../a"')
      expect(renderMarkdown('![pic](/media/x.png)')).toContain('<img src="/media/x.png"')
    })

    describe('resolveMediaUrl and desktop image rewriting', () => {
      it('returns original url unchanged in web build environment', () => {
        vi.stubEnv('VITE_DESKTOP', 'false')
        vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8765/api')
        try {
          expect(resolveMediaUrl('/media/projects/a/images/b.png')).toBe('/media/projects/a/images/b.png')
          expect(resolveMediaUrl('//media/x.png')).toBe('//media/x.png')
          expect(resolveMediaUrl('/mediafoo/x.png')).toBe('/mediafoo/x.png')
          expect(resolveMediaUrl('https://x.com/media/x.png')).toBe('https://x.com/media/x.png')
          expect(resolveMediaUrl('./media/x.png')).toBe('./media/x.png')

          const html = renderMarkdown('![场景](/media/projects/a/images/b.png)')
          expect(html).toContain('<img src="/media/projects/a/images/b.png" alt="场景" referrerpolicy="no-referrer">')
        } finally {
          vi.unstubAllEnvs()
        }
      })

      it('rewrites /media/... in desktop mode to backend origin with port from env', () => {
        vi.stubEnv('VITE_DESKTOP', 'true')
        vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8765/api')
        try {
          expect(getBackendOrigin()).toBe('http://127.0.0.1:8765')
          expect(resolveMediaUrl('/media/projects/a/images/b.png')).toBe(
            'http://127.0.0.1:8765/media/projects/a/images/b.png',
          )
          // Other paths remain unchanged
          expect(resolveMediaUrl('//media/x.png')).toBe('//media/x.png')
          expect(resolveMediaUrl('/mediafoo/x.png')).toBe('/mediafoo/x.png')
          expect(resolveMediaUrl('https://x.com/media/x.png')).toBe('https://x.com/media/x.png')
          expect(resolveMediaUrl('./media/x.png')).toBe('./media/x.png')

          // Renders complete URL in <img> and preserves referrerpolicy
          const html = renderMarkdown('![场景](/media/projects/a/images/b.png)')
          expect(html).toContain(
            '<img src="http://127.0.0.1:8765/media/projects/a/images/b.png" alt="场景" referrerpolicy="no-referrer">',
          )
        } finally {
          vi.unstubAllEnvs()
        }
      })

      it('respects dynamic port in VITE_API_BASE_URL', () => {
        vi.stubEnv('VITE_DESKTOP', 'true')
        vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:9090/api')
        try {
          expect(getBackendOrigin()).toBe('http://127.0.0.1:9090')
          expect(resolveMediaUrl('/media/projects/1/images/2.png')).toBe(
            'http://127.0.0.1:9090/media/projects/1/images/2.png',
          )
        } finally {
          vi.unstubAllEnvs()
        }
      })
    })

    it('renders task list items with unicode checkbox text (☑ / ☐)', () => {
      const markdown = [
        '- [ ] 待办任务一',
        '- [x] 已完成任务二',
        '- [X] 大写勾选任务三',
        '- 普通列表项',
      ].join('\n')
      const html = renderMarkdown(markdown)
      expect(html).toContain('<ul class="task-list">')
      expect(html).toContain('<li class="task-list-item">☐ 待办任务一</li>')
      expect(html).toContain('<li class="task-list-item">☑ 已完成任务二</li>')
      expect(html).toContain('<li class="task-list-item">☑ 大写勾选任务三</li>')
      expect(html).toContain('<li>普通列表项</li>')
      expect(html).not.toContain('<input')
    })

    it('fails closed when DOMPurify is unavailable and logs a warning', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      _setPurifierDisabledForTesting(true)
      try {
        const dirty = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>'
        const output = renderMarkdown(dirty)
        expect(warnSpy).toHaveBeenCalledWith(
          expect.stringContaining('[markdown] DOMPurify sanitizer is unavailable')
        )
        // Wraps in <p> with escaped text, no executable elements or on* attributes
        expect(output).toBe('<p>&lt;script&gt;alert(1)&lt;/script&gt;\n&lt;img src=x onerror=alert(1)&gt;</p>')

        // Verify that in DOM, no executable elements or on* attributes exist
        const doc = new DOMParser().parseFromString(output, 'text/html')
        expect(doc.querySelector('script')).toBeNull()
        expect(doc.querySelector('img')).toBeNull()
        const allElements = doc.querySelectorAll('*')
        for (const el of allElements) {
          for (const attr of el.getAttributeNames()) {
            expect(attr.toLowerCase().startsWith('on')).toBe(false)
          }
        }

        // Inner text contains no raw unescaped < or >
        const inner = output.replace(/^<p>/, '').replace(/<\/p>$/, '')
        expect(inner).not.toContain('<')
        expect(inner).not.toContain('>')
      } finally {
        _setPurifierDisabledForTesting(false)
        warnSpy.mockRestore()
      }
    })

    it('renders comprehensive standard markdown features consistently', () => {
      const input = [
        '# 一级标题',
        '## 二级标题',
        '段落第一行文本，包含 **加粗**、*斜体* 与 ~~删除线~~。',
        '',
        '> 经典引用语句',
        '',
        '- 无序列表一',
        '- 无序列表二',
        '',
        '1. 有序第一步',
        '2. 有序第二步',
        '',
        '行内代码 `const token = localStorage.getItem("token")` 展示。',
        '',
        '```typescript',
        'function test() { return true; }',
        '```',
        '',
        '| 列A | 列B |',
        '| :--- | ---: |',
        '| 数据1 | 数据2 |',
        '',
        '![本地图片](/media/test.png)',
        '![外链图片](https://images.example.com/pic.jpg)',
        '[外部链接](https://example.com)',
      ].join('\n')

      const html = renderMarkdown(input)
      expect(html).toContain('<h1>一级标题</h1>')
      expect(html).toContain('<h2>二级标题</h2>')
      expect(html).toContain('<strong>加粗</strong>')
      expect(html).toContain('<em>斜体</em>')
      expect(html).toContain('<del>删除线</del>')
      expect(html).toContain('<blockquote><p>经典引用语句</p></blockquote>')
      expect(html).toContain('<ul><li>无序列表一</li><li>无序列表二</li></ul>')
      expect(html).toContain('<ol><li>有序第一步</li><li>有序第二步</li></ol>')
      expect(html).toContain('<code>const token = localStorage.getItem("token")</code>')
      expect(html).toContain('<pre><code>function test() { return true; }</code></pre>')
      expect(html).toContain('<table>')
      expect(html).toContain('<th align="left">列A</th>')
      expect(html).toContain('<th align="right">列B</th>')
      expect(html).toContain('<img src="/media/test.png" alt="本地图片" referrerpolicy="no-referrer">')
      expect(html).toContain('<img src="https://images.example.com/pic.jpg" alt="外链图片" referrerpolicy="no-referrer">')
      expect(html).toContain('<a href="https://example.com" target="_blank" rel="noopener noreferrer">外部链接</a>')
    })
  })
})
