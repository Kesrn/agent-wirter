import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark'

const THEME_STORAGE_KEY = 'ai_write_theme'
const theme = ref<ThemeMode>('dark')

export function getPreferredTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    // ignore localStorage errors (e.g. private browsing mode)
  }
  return 'dark'
}

function applyTheme(nextTheme: ThemeMode) {
  theme.value = nextTheme
  if (typeof document !== 'undefined' && document.documentElement) {
    document.documentElement.dataset.theme = nextTheme
    const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    if (meta) {
      meta.content = nextTheme === 'dark' ? '#0e0f12' : '#f3f3f0'
    }
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, nextTheme)
  } catch {
    // ignore
  }
}

export function initTheme() {
  applyTheme(getPreferredTheme())
}

export function useTheme() {
  const isDark = computed(() => theme.value === 'dark')

  function setTheme(nextTheme: ThemeMode) {
    applyTheme(nextTheme)
  }

  function toggleTheme() {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  return {
    theme,
    isDark,
    setTheme,
    toggleTheme,
  }
}
