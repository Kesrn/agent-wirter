// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getPreferredTheme, initTheme, useTheme } from './useTheme'

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
    document.documentElement.dataset.theme = ''
  })

  it('defaults to dark theme when no saved preference exists', () => {
    expect(localStorage.getItem('ai_write_theme')).toBeNull()
    expect(getPreferredTheme()).toBe('dark')
    initTheme()
    const { theme, isDark } = useTheme()
    expect(theme.value).toBe('dark')
    expect(isDark.value).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('honors saved light theme preference', () => {
    localStorage.setItem('ai_write_theme', 'light')
    expect(getPreferredTheme()).toBe('light')
    initTheme()
    const { theme, isDark } = useTheme()
    expect(theme.value).toBe('light')
    expect(isDark.value).toBe(false)
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('honors saved dark theme preference', () => {
    localStorage.setItem('ai_write_theme', 'dark')
    expect(getPreferredTheme()).toBe('dark')
    initTheme()
    const { theme, isDark } = useTheme()
    expect(theme.value).toBe('dark')
    expect(isDark.value).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('falls back to dark theme without throwing if localStorage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: localStorage is disabled in private mode')
    })
    expect(() => getPreferredTheme()).not.toThrow()
    expect(getPreferredTheme()).toBe('dark')
  })

  it('toggles theme between dark and light', () => {
    initTheme()
    const { theme, isDark, toggleTheme, setTheme } = useTheme()
    setTheme('dark')
    expect(theme.value).toBe('dark')
    expect(isDark.value).toBe(true)

    toggleTheme()
    expect(theme.value).toBe('light')
    expect(isDark.value).toBe(false)
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem('ai_write_theme')).toBe('light')

    toggleTheme()
    expect(theme.value).toBe('dark')
    expect(isDark.value).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('ai_write_theme')).toBe('dark')
  })

  it('safely handles localStorage.setItem errors during theme switch', () => {
    const { setTheme } = useTheme()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => setTheme('light')).not.toThrow()
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
