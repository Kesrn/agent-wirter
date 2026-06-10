const TOKEN_KEY = 'ai_write_token'
const LOGGED_IN_KEY = 'ai_write_logged_in'
const USERNAME_KEY = 'ai_write_username'
const LAST_USERNAME_KEY = 'ai_write_last_username'
const REMEMBER_LOGIN_KEY = 'ai_write_remember_login'

type StorageKind = 'local' | 'session'

export interface SaveAuthSessionOptions {
  token: string
  username: string
  remember: boolean
}

function getStorage(kind: StorageKind): Storage | null {
  try {
    return kind === 'local' ? globalThis.localStorage : globalThis.sessionStorage
  } catch {
    return null
  }
}

function read(kind: StorageKind, key: string): string | null {
  try {
    return getStorage(kind)?.getItem(key) ?? null
  } catch {
    return null
  }
}

function write(kind: StorageKind, key: string, value: string): void {
  try {
    getStorage(kind)?.setItem(key, value)
  } catch {
    // Storage can be unavailable in privacy modes; callers still proceed with in-memory UI state.
  }
}

function remove(kind: StorageKind, key: string): void {
  try {
    getStorage(kind)?.removeItem(key)
  } catch {
    // Ignore storage cleanup failures.
  }
}

function clearStorageSession(kind: StorageKind): void {
  remove(kind, TOKEN_KEY)
  remove(kind, LOGGED_IN_KEY)
  remove(kind, USERNAME_KEY)
}

export function getRememberLoginPreference(): boolean {
  return read('local', REMEMBER_LOGIN_KEY) !== 'false'
}

export function getRememberedUsername(): string {
  return read('local', LAST_USERNAME_KEY) ?? read('local', USERNAME_KEY) ?? ''
}

export function getAuthToken(): string | null {
  return read('session', TOKEN_KEY) ?? read('local', TOKEN_KEY)
}

export function getAuthUsername(): string {
  return read('session', USERNAME_KEY) ?? read('local', USERNAME_KEY) ?? getRememberedUsername()
}

export function hasAuthSession(): boolean {
  const sessionLoggedIn = read('session', LOGGED_IN_KEY) === 'true'
  const localLoggedIn = read('local', LOGGED_IN_KEY) === 'true'
  return Boolean(getAuthToken() && (sessionLoggedIn || localLoggedIn))
}

export function saveAuthSession({ token, username, remember }: SaveAuthSessionOptions): void {
  clearStorageSession('local')
  clearStorageSession('session')

  const target: StorageKind = remember ? 'local' : 'session'
  write(target, LOGGED_IN_KEY, 'true')
  write(target, TOKEN_KEY, token)
  write(target, USERNAME_KEY, username)

  write('local', REMEMBER_LOGIN_KEY, remember ? 'true' : 'false')
  if (remember) {
    write('local', LAST_USERNAME_KEY, username)
  } else {
    remove('local', LAST_USERNAME_KEY)
  }
}

export function clearAuthSession(options: { keepRememberedUser?: boolean } = {}): void {
  const keepRememberedUser = options.keepRememberedUser ?? true
  const rememberedUsername = keepRememberedUser ? getRememberedUsername() || getAuthUsername() : ''
  const rememberPreference = getRememberLoginPreference()

  clearStorageSession('local')
  clearStorageSession('session')

  if (keepRememberedUser && rememberPreference && rememberedUsername) {
    write('local', LAST_USERNAME_KEY, rememberedUsername)
  } else if (!keepRememberedUser) {
    remove('local', LAST_USERNAME_KEY)
    remove('local', REMEMBER_LOGIN_KEY)
  }
}
