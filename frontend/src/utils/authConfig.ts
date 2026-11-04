import { api } from '../api/client'
import type { AuthConfigResponse } from '../api/types'

export interface AuthConfig {
  registration_enabled: boolean
}

export const DEFAULT_AUTH_CONFIG: Readonly<AuthConfig> = Object.freeze({
  registration_enabled: true,
})

const AUTH_CONFIG_STORAGE_KEY = 'ai_write_auth_config'

let cachedConfig: AuthConfig | null = null
let inflightPromise: Promise<AuthConfig> | null = null

function readSessionStorage(): AuthConfig | null {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return null
    const raw = window.sessionStorage.getItem(AUTH_CONFIG_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.registration_enabled === 'boolean') {
      return { registration_enabled: parsed.registration_enabled }
    }
    return null
  } catch {
    return null
  }
}

function writeSessionStorage(config: AuthConfig): void {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return
    window.sessionStorage.setItem(AUTH_CONFIG_STORAGE_KEY, JSON.stringify(config))
  } catch {
    // Storage can be unavailable in privacy modes or restricted contexts
  }
}

/**
 * Synchronously retrieve auth config if already cached in memory or sessionStorage.
 */
export function getCachedAuthConfig(): AuthConfig | null {
  if (cachedConfig) return cachedConfig
  const stored = readSessionStorage()
  if (stored) {
    cachedConfig = stored
    return stored
  }
  return null
}

/**
 * Load auth configuration (whether registration is enabled).
 *
 * Guarantees:
 * - Only requests once per session and caches the result.
 * - Coalesces concurrent calls into a single inflight request.
 * - On failure, timeout, non-JSON or missing/invalid fields, gracefully defaults
 *   to { registration_enabled: true } without throwing uncaught exceptions.
 */
export async function loadAuthConfig(options?: {
  timeoutMs?: number
  forceRefresh?: boolean
}): Promise<AuthConfig> {
  if (!options?.forceRefresh) {
    const cached = getCachedAuthConfig()
    if (cached) {
      return cached
    }
  }

  if (inflightPromise) {
    return inflightPromise
  }

  inflightPromise = (async () => {
    try {
      const res: AuthConfigResponse | undefined = await api.getAuthConfig?.({
        timeoutMs: options?.timeoutMs,
      })
      if (res && typeof res.registration_enabled === 'boolean') {
        const result: AuthConfig = { registration_enabled: res.registration_enabled }
        cachedConfig = result
        writeSessionStorage(result)
        return result
      }
      // Malformed / missing field -> fallback to default (open)
      const fallback = { ...DEFAULT_AUTH_CONFIG }
      cachedConfig = fallback
      writeSessionStorage(fallback)
      return fallback
    } catch {
      // Network error, timeout, 5xx, 4xx -> fallback to default (open)
      const fallback = { ...DEFAULT_AUTH_CONFIG }
      cachedConfig = fallback
      writeSessionStorage(fallback)
      return fallback
    } finally {
      inflightPromise = null
    }
  })()

  return inflightPromise
}

/**
 * Reset memory and sessionStorage cache (primarily for tests).
 */
export function resetAuthConfigCache(): void {
  cachedConfig = null
  inflightPromise = null
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) {
      window.sessionStorage.removeItem(AUTH_CONFIG_STORAGE_KEY)
    }
  } catch {
    // Ignore
  }
}
