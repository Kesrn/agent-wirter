import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark'

const THEME_STORAGE_KEY = 'ai_write_theme'
const theme = ref<ThemeMode>('light')

function getPreferredTheme(): ThemeMode {
  const saved = localStorage.getItem(THEME_STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  if (window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'light'
}

function applyTheme(nextTheme: ThemeMode) {
  theme.value = nextTheme
  document.documentElement.dataset.theme = nextTheme
  localStorage.setItem(THEME_STORAGE_KEY, nextTheme)
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (meta) {
    meta.content = nextTheme === 'dark' ? '#0f141b' : '#f4f6fb'
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
