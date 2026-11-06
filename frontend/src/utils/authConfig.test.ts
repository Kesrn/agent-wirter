// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  loadAuthConfig,
  getCachedAuthConfig,
  resetAuthConfigCache,
  DEFAULT_AUTH_CONFIG,
} from './authConfig'
import { api, ApiError } from '../api/client'

describe('authConfig utility (loadAuthConfig & getCachedAuthConfig)', () => {
  beforeEach(() => {
    resetAuthConfigCache()
    vi.restoreAllMocks()
    const store = new Map<string, string>()
    vi.stubGlobal('sessionStorage', {
      getItem(key: string) {
        return store.get(key) ?? null
      },
      setItem(key: string, value: string) {
        store.set(key, String(value))
      },
      removeItem(key: string) {
        store.delete(key)
      },
      clear() {
        store.clear()
      },
    })
  })

  afterEach(() => {
    resetAuthConfigCache()
    vi.unstubAllGlobals()
  })

  it('returns registration_enabled=false when backend reports disabled', async () => {
    vi.spyOn(api, 'getAuthConfig').mockResolvedValueOnce({
      registration_enabled: false,
    })

    const config = await loadAuthConfig()
    expect(config).toEqual({ registration_enabled: false })
    expect(api.getAuthConfig).toHaveBeenCalledTimes(1)
  })

  it('returns registration_enabled=true when backend reports enabled', async () => {
    vi.spyOn(api, 'getAuthConfig').mockResolvedValueOnce({
      registration_enabled: true,
    })

    const config = await loadAuthConfig()
    expect(config).toEqual({ registration_enabled: true })
    expect(api.getAuthConfig).toHaveBeenCalledTimes(1)
  })

  it('coalesces multiple calls in the same session: only one request is sent', async () => {
    const getAuthConfigSpy = vi.spyOn(api, 'getAuthConfig').mockResolvedValue({
      registration_enabled: false,
    })

    const res1 = await loadAuthConfig()
    const res2 = await loadAuthConfig()
    const res3 = await loadAuthConfig()

    expect(res1).toEqual({ registration_enabled: false })
    expect(res2).toEqual({ registration_enabled: false })
    expect(res3).toEqual({ registration_enabled: false })
    expect(getAuthConfigSpy).toHaveBeenCalledTimes(1)
  })

  it('coalesces concurrent inflight calls into a single request', async () => {
    let resolveCall!: (value: { registration_enabled: boolean }) => void
    const pendingPromise = new Promise<{ registration_enabled: boolean }>((resolve) => {
      resolveCall = resolve
    })

    const getAuthConfigSpy = vi.spyOn(api, 'getAuthConfig').mockReturnValueOnce(pendingPromise)

    const p1 = loadAuthConfig()
    const p2 = loadAuthConfig()
    const p3 = loadAuthConfig()

    resolveCall({ registration_enabled: false })

    const [r1, r2, r3] = await Promise.all([p1, p2, p3])
    expect(r1).toEqual({ registration_enabled: false })
    expect(r2).toEqual({ registration_enabled: false })
    expect(r3).toEqual({ registration_enabled: false })
    expect(getAuthConfigSpy).toHaveBeenCalledTimes(1)
  })

  it('defaults to registration_enabled=true on network failure without uncaught errors', async () => {
    vi.spyOn(api, 'getAuthConfig').mockRejectedValueOnce(new Error('Network error / connection refused'))

    const config = await loadAuthConfig()
    expect(config).toEqual(DEFAULT_AUTH_CONFIG)
    expect(config.registration_enabled).toBe(true)

    // And subsequent calls return the cached fallback without re-fetching
    const config2 = await loadAuthConfig()
    expect(config2.registration_enabled).toBe(true)
    expect(api.getAuthConfig).toHaveBeenCalledTimes(1)
  })

  it('defaults to registration_enabled=true on 401/403/500 ApiError', async () => {
    vi.spyOn(api, 'getAuthConfig').mockRejectedValueOnce(new ApiError(500, 'Internal Server Error'))

    const config = await loadAuthConfig()
    expect(config).toEqual({ registration_enabled: true })
  })

  it('defaults to registration_enabled=true on timeout (AbortError)', async () => {
    const abortErr = new DOMException('The operation was aborted', 'AbortError')
    vi.spyOn(api, 'getAuthConfig').mockRejectedValueOnce(abortErr)

    const config = await loadAuthConfig()
    expect(config).toEqual({ registration_enabled: true })
  })

  it('defaults to registration_enabled=true on malformed response or missing fields', async () => {
    // Missing registration_enabled field
    vi.spyOn(api, 'getAuthConfig').mockResolvedValueOnce({} as any)
    const c1 = await loadAuthConfig()
    expect(c1).toEqual({ registration_enabled: true })

    resetAuthConfigCache()

    // Non-boolean registration_enabled field
    vi.spyOn(api, 'getAuthConfig').mockResolvedValueOnce({ registration_enabled: 'yes' } as any)
    const c2 = await loadAuthConfig()
    expect(c2).toEqual({ registration_enabled: true })
  })

  it('getCachedAuthConfig synchronously retrieves cached result', async () => {
    expect(getCachedAuthConfig()).toBeNull()

    vi.spyOn(api, 'getAuthConfig').mockResolvedValueOnce({
      registration_enabled: false,
    })

    await loadAuthConfig()

    const syncCached = getCachedAuthConfig()
    expect(syncCached).toEqual({ registration_enabled: false })
  })

  it('resetAuthConfigCache clears cached state and allows fresh request', async () => {
    const getAuthConfigSpy = vi.spyOn(api, 'getAuthConfig')
      .mockResolvedValueOnce({ registration_enabled: false })
      .mockResolvedValueOnce({ registration_enabled: true })

    const res1 = await loadAuthConfig()
    expect(res1.registration_enabled).toBe(false)
    expect(getAuthConfigSpy).toHaveBeenCalledTimes(1)

    resetAuthConfigCache()
    expect(getCachedAuthConfig()).toBeNull()

    const res2 = await loadAuthConfig()
    expect(res2.registration_enabled).toBe(true)
    expect(getAuthConfigSpy).toHaveBeenCalledTimes(2)
  })
})
