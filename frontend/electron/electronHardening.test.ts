import { describe, expect, it } from 'vitest'
import mainSource from './main.cjs?raw'
import preloadSource from './preload.cjs?raw'

describe('Electron configuration hardening', () => {
  it('enforces sandbox: true and keeps nodeIntegration: false and contextIsolation: true', () => {
    const webPreferencesMatch = mainSource.match(/webPreferences:\s*\{([\s\S]*?)\}/)
    expect(webPreferencesMatch).not.toBeNull()
    const webPreferences = webPreferencesMatch![1]

    expect(webPreferences).toContain('sandbox: true')
    expect(webPreferences).toContain('nodeIntegration: false')
    expect(webPreferences).toContain('contextIsolation: true')
    expect(webPreferences).toContain("preload: path.join(__dirname, 'preload.cjs')")
  })

  it('ensures preload.cjs only requires electron and has no Node built-in dependencies', () => {
    // Collect all require calls in preload.cjs
    const requireMatches = [...preloadSource.matchAll(/require\s*\(\s*['"]([^'"]+)['"]\s*\)/g)]
    const requiredModules = requireMatches.map(m => m[1])

    expect(requiredModules.length).toBe(1)
    expect(requiredModules[0]).toBe('electron')

    // Must not require node:fs, node:path, child_process, etc.
    expect(preloadSource).not.toContain('node:')
    expect(preloadSource).not.toContain('fs')
    expect(preloadSource).not.toContain('path')
    expect(preloadSource).not.toContain('child_process')
  })

  it('wires navigation security handlers in main.cjs', () => {
    expect(mainSource).toContain('handleWindowOpen')
    expect(mainSource).toContain('handleWillNavigate')
    expect(mainSource).toContain('win.webContents.setWindowOpenHandler')
    expect(mainSource).toContain("win.webContents.on('will-navigate'")
  })
})
