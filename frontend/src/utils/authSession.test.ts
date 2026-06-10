import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearAuthSession,
  getAuthToken,
  getRememberLoginPreference,
  getRememberedUsername,
  hasAuthSession,
  saveAuthSession,
} from './authSession'

function createMemoryStorage(): Storage {
  let data: Record<string, string> = {}
  return {
    get length() {
      return Object.keys(data).length
    },
    clear: vi.fn(() => {
      data = {}
    }),
    getItem: vi.fn((key: string) => data[key] ?? null),
    key: vi.fn((index: number) => Object.keys(data)[index] ?? null),
    removeItem: vi.fn((key: string) => {
      delete data[key]
    }),
    setItem: vi.fn((key: string, value: string) => {
      data[key] = String(value)
    }),
  }
}

describe('authSession', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', createMemoryStorage())
    vi.stubGlobal('sessionStorage', createMemoryStorage())
    localStorage.clear()
    sessionStorage.clear()
  })

  it('persists remembered login sessions in localStorage', () => {
    saveAuthSession({ token: 'local-token', username: 'admin', remember: true })

    expect(getAuthToken()).toBe('local-token')
    expect(hasAuthSession()).toBe(true)
    expect(getRememberLoginPreference()).toBe(true)
    expect(getRememberedUsername()).toBe('admin')
    expect(localStorage.getItem('ai_write_token')).toBe('local-token')
    expect(sessionStorage.getItem('ai_write_token')).toBeNull()
  })

  it('stores non-remembered sessions only for the current browser session', () => {
    saveAuthSession({ token: 'session-token', username: 'guest', remember: false })

    expect(getAuthToken()).toBe('session-token')
    expect(hasAuthSession()).toBe(true)
    expect(getRememberLoginPreference()).toBe(false)
    expect(getRememberedUsername()).toBe('')
    expect(localStorage.getItem('ai_write_token')).toBeNull()
    expect(sessionStorage.getItem('ai_write_token')).toBe('session-token')
  })

  it('clears login state but can keep the remembered username', () => {
    saveAuthSession({ token: 'local-token', username: 'admin', remember: true })
    clearAuthSession()

    expect(getAuthToken()).toBeNull()
    expect(hasAuthSession()).toBe(false)
    expect(getRememberedUsername()).toBe('admin')
  })
})
