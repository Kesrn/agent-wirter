import { describe, it, expect } from 'vitest'
// @ts-expect-error CommonJS module import without d.ts
import mediaAuth = require('./mediaAuthHeader.cjs')

const {
  isValidToken,
  isValidSenderUrl,
  shouldInjectAuthHeader,
  buildHeadersWithAuth,
} = mediaAuth

describe('mediaAuthHeader helpers', () => {
  const backendOrigin = 'http://127.0.0.1:8765'

  describe('isValidToken', () => {
    it('accepts valid token strings', () => {
      expect(isValidToken('token-123')).toBe(true)
      expect(isValidToken('  jwt-token-value  ')).toBe(true)
      expect(isValidToken('x'.repeat(4096))).toBe(true)
    })

    it('rejects non-string types without throwing', () => {
      expect(isValidToken(null)).toBe(false)
      expect(isValidToken(undefined)).toBe(false)
      expect(isValidToken(12345)).toBe(false)
      expect(isValidToken({})).toBe(false)
      expect(isValidToken([])).toBe(false)
      expect(isValidToken(() => {})).toBe(false)
    })

    it('rejects empty or whitespace-only strings', () => {
      expect(isValidToken('')).toBe(false)
      expect(isValidToken('   ')).toBe(false)
      expect(isValidToken('\t\r\n')).toBe(false)
    })

    it('rejects strings exceeding 4096 characters', () => {
      expect(isValidToken('x'.repeat(4097))).toBe(false)
    })
  })

  describe('isValidSenderUrl', () => {
    it('validates sender URL in dev mode', () => {
      expect(isValidSenderUrl('http://127.0.0.1:5173/index.html', { isDev: true })).toBe(true)
      expect(isValidSenderUrl('http://127.0.0.1:5173/#/projects', { isDev: true })).toBe(true)
      expect(isValidSenderUrl('http://evil.com/phish', { isDev: true })).toBe(false)
      expect(isValidSenderUrl('file:///path/index.html', { isDev: true })).toBe(false)
    })

    it('validates sender URL with custom devRendererUrl', () => {
      expect(
        isValidSenderUrl('http://localhost:3000/app', {
          isDev: true,
          devRendererUrl: 'http://localhost:3000',
        }),
      ).toBe(true)
      expect(
        isValidSenderUrl('http://127.0.0.1:5173/app', {
          isDev: true,
          devRendererUrl: 'http://localhost:3000',
        }),
      ).toBe(false)
    })

    it('validates sender URL in production packaged mode (file:// only)', () => {
      expect(isValidSenderUrl('file:///Applications/App.app/dist-desktop/index.html', { isDev: false })).toBe(true)
      expect(isValidSenderUrl('http://127.0.0.1:5173/index.html', { isDev: false })).toBe(false)
      expect(isValidSenderUrl('https://evil.com', { isDev: false })).toBe(false)
    })
  })

  describe('shouldInjectAuthHeader', () => {
    const validToken = 'valid-test-token-jwt'

    it('injects header for media URLs under backend origin', () => {
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media/projects/p1/images/img1.png', backendOrigin, validToken),
      ).toBe(true)
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media/anything', backendOrigin, validToken),
      ).toBe(true)
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media', backendOrigin, validToken),
      ).toBe(true)
    })

    it('does NOT inject header for /api/* routes', () => {
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/api/projects', backendOrigin, validToken),
      ).toBe(false)
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/api/auth/login', backendOrigin, validToken),
      ).toBe(false)
    })

    it('does NOT inject header for other domains or ports', () => {
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:9999/media/pic.png', backendOrigin, validToken),
      ).toBe(false)
      expect(
        shouldInjectAuthHeader('http://example.com/media/pic.png', backendOrigin, validToken),
      ).toBe(false)
      expect(
        shouldInjectAuthHeader('https://127.0.0.1:8765/media/pic.png', backendOrigin, validToken),
      ).toBe(false)
    })

    it('does NOT inject header when token is missing, empty, or invalid', () => {
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media/pic.png', backendOrigin, ''),
      ).toBe(false)
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media/pic.png', backendOrigin, null),
      ).toBe(false)
      expect(
        shouldInjectAuthHeader('http://127.0.0.1:8765/media/pic.png', backendOrigin, undefined),
      ).toBe(false)
    })

    it('does NOT inject header for malformed URLs', () => {
      expect(shouldInjectAuthHeader('not-a-valid-url', backendOrigin, validToken)).toBe(false)
      expect(shouldInjectAuthHeader('', backendOrigin, validToken)).toBe(false)
    })
  })

  describe('buildHeadersWithAuth', () => {
    it('adds Authorization header without mutating original object', () => {
      const original = { 'Content-Type': 'application/json' }
      const result = buildHeadersWithAuth(original, 'my-secret-token')

      expect(result).toEqual({
        'Content-Type': 'application/json',
        Authorization: 'Bearer my-secret-token',
      })
      expect(original).toEqual({ 'Content-Type': 'application/json' })
    })

    it('does not add Authorization header if token is invalid', () => {
      const original = { Accept: 'image/*' }
      expect(buildHeadersWithAuth(original, '')).toEqual({ Accept: 'image/*' })
      expect(buildHeadersWithAuth(original, null)).toEqual({ Accept: 'image/*' })
    })
  })
})
