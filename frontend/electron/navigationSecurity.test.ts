import { describe, it, expect, vi } from 'vitest'
// @ts-expect-error CommonJS module import without d.ts
import navSecurity = require('./navigationSecurity.cjs')

const {
  isHttpOrHttpsUrl,
  isInternalAppUrl,
  handleWindowOpen,
  handleWillNavigate,
} = navSecurity

describe('Electron navigation security helpers', () => {
  const prodAppFilePrefix = 'file:///Applications/AIWriter.app/dist-desktop/'

  describe('isHttpOrHttpsUrl', () => {
    it('accepts valid http and https URLs', () => {
      expect(isHttpOrHttpsUrl('https://example.com')).toBe(true)
      expect(isHttpOrHttpsUrl('http://127.0.0.1:8765/media/pic.png')).toBe(true)
      expect(isHttpOrHttpsUrl('https://sub.domain.org/path?q=1#anchor')).toBe(true)
    })

    it('rejects dangerous and non-http schemes', () => {
      expect(isHttpOrHttpsUrl('javascript:alert(1)')).toBe(false)
      expect(isHttpOrHttpsUrl('data:text/html;base64,PHNjcmlwdD4=')).toBe(false)
      expect(isHttpOrHttpsUrl('file:///etc/passwd')).toBe(false)
      expect(isHttpOrHttpsUrl('blob:http://example.com/uuid')).toBe(false)
      expect(isHttpOrHttpsUrl('about:blank')).toBe(false)
    })

    it('rejects invalid, malformed, or empty inputs', () => {
      expect(isHttpOrHttpsUrl('')).toBe(false)
      expect(isHttpOrHttpsUrl('not-a-url')).toBe(false)
      expect(isHttpOrHttpsUrl(null as any)).toBe(false)
      expect(isHttpOrHttpsUrl(undefined as any)).toBe(false)
    })
  })

  describe('isInternalAppUrl', () => {
    it('allows dev renderer URLs in dev mode', () => {
      const opts = { isDev: true, devRendererUrl: 'http://127.0.0.1:5173' }
      expect(isInternalAppUrl('http://127.0.0.1:5173/', opts)).toBe(true)
      expect(isInternalAppUrl('http://127.0.0.1:5173/#/projects', opts)).toBe(true)
      expect(isInternalAppUrl('http://127.0.0.1:5173/assets/index.js', opts)).toBe(true)

      // External or file URLs are not internal in dev mode
      expect(isInternalAppUrl('https://example.com', opts)).toBe(false)
      expect(isInternalAppUrl('file:///etc/passwd', opts)).toBe(false)
    })

    it('allows only application package file URLs in production mode', () => {
      const opts = { isDev: false, appFilePrefix: prodAppFilePrefix }
      expect(isInternalAppUrl('file:///Applications/AIWriter.app/dist-desktop/index.html', opts)).toBe(true)
      expect(isInternalAppUrl('file:///Applications/AIWriter.app/dist-desktop/index.html#/settings', opts)).toBe(true)

      // Arbitrary local files (e.g. /etc/passwd) must be rejected
      expect(isInternalAppUrl('file:///etc/passwd', opts)).toBe(false)
      expect(isInternalAppUrl('file:///C:/Windows/System32/calc.exe', opts)).toBe(false)
      expect(isInternalAppUrl('https://example.com', opts)).toBe(false)
    })
  })

  describe('handleWindowOpen', () => {
    it('delegates http/https URLs to shell.openExternal and returns deny action', () => {
      const openExternalMock = vi.fn()
      const result = handleWindowOpen('https://example.com', { openExternal: openExternalMock })

      expect(openExternalMock).toHaveBeenCalledWith('https://example.com')
      expect(result).toEqual({ action: 'deny' })
    })

    it('denies pseudo-protocols and local files without calling shell.openExternal', () => {
      const openExternalMock = vi.fn()

      expect(handleWindowOpen('javascript:alert(1)', { openExternal: openExternalMock })).toEqual({ action: 'deny' })
      expect(handleWindowOpen('data:text/html,<script>alert(1)</script>', { openExternal: openExternalMock })).toEqual({ action: 'deny' })
      expect(handleWindowOpen('file:///etc/passwd', { openExternal: openExternalMock })).toEqual({ action: 'deny' })

      expect(openExternalMock).not.toHaveBeenCalled()
    })
  })

  describe('handleWillNavigate', () => {
    it('allows internal navigation without calling preventDefault or openExternal', () => {
      const openExternalMock = vi.fn()
      const preventDefaultMock = vi.fn()
      const opts = { isDev: true, devRendererUrl: 'http://127.0.0.1:5173' }

      const outcome = handleWillNavigate('http://127.0.0.1:5173/#/workspace', opts, {
        openExternal: openExternalMock,
        preventDefault: preventDefaultMock,
      })

      expect(outcome).toBe('allow')
      expect(preventDefaultMock).not.toHaveBeenCalled()
      expect(openExternalMock).not.toHaveBeenCalled()
    })

    it('intercepts external http/https navigation, prevents default, and opens in system browser', () => {
      const openExternalMock = vi.fn()
      const preventDefaultMock = vi.fn()
      const opts = { isDev: true, devRendererUrl: 'http://127.0.0.1:5173' }

      const outcome = handleWillNavigate('https://github.com', opts, {
        openExternal: openExternalMock,
        preventDefault: preventDefaultMock,
      })

      expect(outcome).toBe('external')
      expect(preventDefaultMock).toHaveBeenCalledTimes(1)
      expect(openExternalMock).toHaveBeenCalledWith('https://github.com')
    })

    it('blocks dangerous schemes (javascript:, data:, file:///etc/passwd) and does not call openExternal', () => {
      const openExternalMock = vi.fn()
      const preventDefaultMock = vi.fn()
      const opts = { isDev: false, appFilePrefix: prodAppFilePrefix }

      const outcome1 = handleWillNavigate('javascript:alert(1)', opts, {
        openExternal: openExternalMock,
        preventDefault: preventDefaultMock,
      })
      expect(outcome1).toBe('deny')

      const outcome2 = handleWillNavigate('data:text/html,attack', opts, {
        openExternal: openExternalMock,
        preventDefault: preventDefaultMock,
      })
      expect(outcome2).toBe('deny')

      const outcome3 = handleWillNavigate('file:///etc/passwd', opts, {
        openExternal: openExternalMock,
        preventDefault: preventDefaultMock,
      })
      expect(outcome3).toBe('deny')

      expect(preventDefaultMock).toHaveBeenCalledTimes(3)
      expect(openExternalMock).not.toHaveBeenCalled()
    })
  })
})
