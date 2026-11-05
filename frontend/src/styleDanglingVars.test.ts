import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

function collectFiles(dir: string, extensions: string[]): string[] {
  const entries = readdirSync(dir)
  const files: string[] = []
  for (const entry of entries) {
    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)
    if (stat.isDirectory()) {
      files.push(...collectFiles(fullPath, extensions))
    } else if (extensions.some(ext => entry.endsWith(ext))) {
      files.push(fullPath)
    }
  }
  return files
}

describe('CSS dangling variables detection', () => {
  const currentDir = fileURLToPath(new URL('.', import.meta.url))
  const styleCssPath = join(currentDir, 'style.css')
  const styleCssContent = readFileSync(styleCssPath, 'utf-8')

  // Extract all globally defined CSS variables in style.css
  const globalVars = new Set<string>()
  for (const match of styleCssContent.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)) {
    globalVars.add(match[1])
  }

  const vueFiles = collectFiles(currentDir, ['.vue'])
  const targetFiles = [...vueFiles, styleCssPath]

  it('ensures all var(--name) without fallback are defined in style.css or locally', () => {
    const noFallbackRegex = /var\s*\(\s*(--[a-zA-Z0-9_-]+)\s*\)/g
    const dangling: Array<{ varName: string; file: string }> = []

    for (const filePath of targetFiles) {
      const content = readFileSync(filePath, 'utf-8')
      const localVars = new Set<string>()
      for (const match of content.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)) {
        localVars.add(match[1])
      }

      const relPath = relative(join(currentDir, '..'), filePath)
      for (const match of content.matchAll(noFallbackRegex)) {
        const varName = match[1]
        if (!globalVars.has(varName) && !localVars.has(varName)) {
          dangling.push({ varName, file: relPath })
        }
      }
    }

    if (dangling.length > 0) {
      const formatted = dangling
        .map(item => `  - ${item.varName} in ${item.file}`)
        .join('\n')
      expect.fail(
        `Found ${dangling.length} dangling CSS variable usage(s) without fallback or definition:\n${formatted}`
      )
    }

    expect(dangling).toHaveLength(0)
  })
})
