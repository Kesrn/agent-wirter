import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  clearAuthSession,
  getAuthToken,
  getRememberLoginPreference,
  getRememberedUsername,
  handleUnauthorized,
  hasAuthSession,
  initAuthSession,
  performLogout,
  resetUnauthorizedFlag,
  saveAuthSession,
  syncDesktopMediaToken,
} from './authSession'
import { useProjectStore } from '../stores'

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

  it('handleUnauthorized clears session and redirects to login once even when called concurrently', () => {
    resetUnauthorizedFlag()
    saveAuthSession({ token: 'local-token', username: 'admin', remember: true })

    const assignMock = vi.fn()
    let targetHref = 'http://localhost/'
    const mockWindow = {
      location: {
        get href() { return targetHref },
        set href(val: string) {
          assignMock(val)
          targetHref = val
        },
      },
    }
    vi.stubGlobal('window', mockWindow)

    try {
      handleUnauthorized()
      handleUnauthorized()

      expect(getAuthToken()).toBeNull()
      expect(hasAuthSession()).toBe(false)
      expect(assignMock).toHaveBeenCalledTimes(1)
      expect(assignMock).toHaveBeenCalledWith('/login')
    } finally {
      vi.unstubAllGlobals()
      resetUnauthorizedFlag()
    }
  })

  describe('desktop media token synchronization and initAuthSession', () => {
    it('safely handles desktop token sync when window.desktopApp is undefined', () => {
      vi.stubGlobal('window', {})
      expect(() => syncDesktopMediaToken('test-token')).not.toThrow()
      expect(() => syncDesktopMediaToken(null)).not.toThrow()
      vi.unstubAllGlobals()
    })

    it('invokes window.desktopApp methods when present', () => {
      const setTokenMock = vi.fn()
      const clearTokenMock = vi.fn()
      vi.stubGlobal('window', {
        desktopApp: {
          setMediaAuthToken: setTokenMock,
          clearMediaAuthToken: clearTokenMock,
        },
      })

      syncDesktopMediaToken('jwt-abc')
      expect(setTokenMock).toHaveBeenCalledWith('jwt-abc')

      syncDesktopMediaToken(null)
      expect(clearTokenMock).toHaveBeenCalled()

      vi.unstubAllGlobals()
    })

    it('initAuthSession synchronizes token to desktop app when existing session is present', () => {
      saveAuthSession({ token: 'initial-boot-token', username: 'admin', remember: true })

      const setTokenMock = vi.fn()
      vi.stubGlobal('window', {
        desktopApp: {
          setMediaAuthToken: setTokenMock,
        },
      })

      initAuthSession()
      expect(setTokenMock).toHaveBeenCalledWith('initial-boot-token')

      vi.unstubAllGlobals()
    })

    it('initAuthSession does nothing when no session exists', () => {
      clearAuthSession()
      const setTokenMock = vi.fn()
      vi.stubGlobal('window', {
        desktopApp: {
          setMediaAuthToken: setTokenMock,
        },
      })

      initAuthSession()
      expect(setTokenMock).not.toHaveBeenCalled()

      vi.unstubAllGlobals()
    })
  })

  describe('performLogout', () => {
    beforeEach(() => {
      setActivePinia(createPinia())
    })

    it('calls POST /auth/logout once, clears storage, notifies desktop, resets stores and redirects', async () => {
      saveAuthSession({ token: 'logout-token-123', username: 'alice', remember: true })
      const projectStore = useProjectStore()
      projectStore.setCurrent('some-project-id')

      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 204,
      })
      vi.stubGlobal('fetch', fetchMock)

      const clearTokenMock = vi.fn()
      const assignMock = vi.fn()
      let targetHref = 'http://localhost/projects'
      const mockWindow = {
        location: {
          get href() { return targetHref },
          set href(val: string) {
            assignMock(val)
            targetHref = val
          },
        },
        desktopApp: {
          clearMediaAuthToken: clearTokenMock,
        },
      }
      vi.stubGlobal('window', mockWindow)

      try {
        await performLogout()

        // 1. POST /auth/logout called once with Authorization header
        expect(fetchMock).toHaveBeenCalledTimes(1)
        const [url, options] = fetchMock.mock.calls[0]
        expect(String(url)).toContain('/auth/logout')
        expect(options.method).toBe('POST')
        expect(options.headers['Authorization']).toBe('Bearer logout-token-123')

        // 2. Storage cleared
        expect(getAuthToken()).toBeNull()
        expect(hasAuthSession()).toBe(false)

        // 3. Desktop main process cleared
        expect(clearTokenMock).toHaveBeenCalledTimes(1)

        // 4. Stores reset
        expect(projectStore.currentProjectId).toBeNull()

        // 5. Redirected to /login
        expect(assignMock).toHaveBeenCalledWith('/login')
      } finally {
        vi.unstubAllGlobals()
      }
    })

    it('completes local logout even if POST /auth/logout fails or times out', async () => {
      saveAuthSession({ token: 'logout-token-broken', username: 'bob', remember: false })
      const projectStore = useProjectStore()
      projectStore.setCurrent('bob-project')

      // Network rejection / 500 error
      const fetchMock = vi.fn().mockRejectedValue(new Error('Network timeout'))
      vi.stubGlobal('fetch', fetchMock)

      const clearTokenMock = vi.fn()
      const assignMock = vi.fn()
      let targetHref = 'http://localhost/projects'
      const mockWindow = {
        location: {
          get href() { return targetHref },
          set href(val: string) {
            assignMock(val)
            targetHref = val
          },
        },
        desktopApp: {
          clearMediaAuthToken: clearTokenMock,
        },
      }
      vi.stubGlobal('window', mockWindow)

      try {
        await performLogout()

        // Storage still cleared
        expect(getAuthToken()).toBeNull()
        expect(hasAuthSession()).toBe(false)

        // Desktop notified
        expect(clearTokenMock).toHaveBeenCalledTimes(1)

        // Stores reset
        expect(projectStore.currentProjectId).toBeNull()

        // Redirected
        expect(assignMock).toHaveBeenCalledWith('/login')
      } finally {
        vi.unstubAllGlobals()
      }
    })
  })
})
