import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { createServer } from 'vite'

describe('CSP build artifact and dev server verification', () => {
  const distHtmlPath = path.resolve(__dirname, '../../dist/index.html')
  const desktopHtmlPath = path.resolve(__dirname, '../../dist-desktop/index.html')

  it('verifies dist/index.html contains production CSP meta and no inline scripts', () => {
    expect(fs.existsSync(distHtmlPath)).toBe(true)
    const content = fs.readFileSync(distHtmlPath, 'utf-8')

    // Contains CSP meta tag
    expect(content).toContain('http-equiv="Content-Security-Policy"')
    expect(content).toContain("default-src &#39;self&#39;")
    expect(content).toContain("script-src &#39;self&#39;")
    expect(content).toContain("object-src &#39;none&#39;")
    // Web build does not contain 127.0.0.1
    expect(content).not.toContain('127.0.0.1')

    // No inline scripts: <script>...</script> where content is not empty
    const inlineScriptMatch = content.match(/<script(?![^>]*src=)[^>]*>([\s\S]+?)<\/script>/i)
    expect(inlineScriptMatch).toBeNull()
  })

  it('verifies dist-desktop/index.html contains desktop CSP meta with backend origin and no inline scripts', () => {
    expect(fs.existsSync(desktopHtmlPath)).toBe(true)
    const content = fs.readFileSync(desktopHtmlPath, 'utf-8')

    expect(content).toContain('http-equiv="Content-Security-Policy"')
    expect(content).toContain("default-src &#39;self&#39;")
    expect(content).toContain("script-src &#39;self&#39;")
    // Desktop build contains backend origin in img-src and connect-src
    expect(content).toContain('http://127.0.0.1:8765')

    const inlineScriptMatch = content.match(/<script(?![^>]*src=)[^>]*>([\s\S]+?)<\/script>/i)
    expect(inlineScriptMatch).toBeNull()
  })

  it('verifies development server HTML does not contain Content-Security-Policy meta', async () => {
    const server = await createServer({
      configFile: path.resolve(__dirname, '../../vite.config.ts'),
      server: { port: 0 },
    })

    try {
      const html = fs.readFileSync(path.resolve(__dirname, '../../index.html'), 'utf-8')
      const transformed = await server.transformIndexHtml('/', html)
      // Dev mode must not contain CSP meta tag (so HMR and WebSockets function without restriction)
      expect(transformed).not.toContain('http-equiv="Content-Security-Policy"')
      expect(transformed).not.toContain('Content-Security-Policy')
    } finally {
      await server.close()
    }
  })
})
