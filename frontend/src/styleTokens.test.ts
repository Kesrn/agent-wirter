import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const styleSource = readFileSync(new URL('./style.css', import.meta.url), 'utf-8')

function extractCssBlock(source: string, selectorPattern: RegExp): string {
  const match = source.match(selectorPattern)
  return match?.[1] ?? ''
}

describe('design tokens completeness contract', () => {
  const lightBlock = extractCssBlock(styleSource, /:root\s*\{([\s\S]*?)\n\}\n\nhtml\s*\{/)
  const darkBlock = extractCssBlock(styleSource, /:root\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/)

  const REQUIRED_TOKENS = [
    '--bg',
    '--bg-panel',
    '--text',
    '--accent',
    '--border',
    '--diff-ins-bg',
    '--diff-del-bg',
    '--editor-font',
  ]

  it('declares all required tokens in light theme root block', () => {
    expect(lightBlock).toBeTruthy()
    for (const token of REQUIRED_TOKENS) {
      expect(lightBlock).toMatch(new RegExp(`${token}\\s*:`))
    }
  })

  it('declares all required tokens in dark theme root block', () => {
    expect(darkBlock).toBeTruthy()
    for (const token of REQUIRED_TOKENS) {
      expect(darkBlock).toMatch(new RegExp(`${token}\\s*:`))
    }
  })

  it('ensures dark theme activates dark color scheme', () => {
    expect(styleSource).toContain('html[data-theme="dark"] {\n  color-scheme: dark;\n}')
  })
})
