<script setup lang="ts">
import { computed } from 'vue'
import { useTheme } from '../composables/useTheme'

const { isDark, toggleTheme } = useTheme()
const label = computed(() => isDark.value ? '切换到浅色主题' : '切换到暗色主题')
</script>

<template>
  <button
    class="theme-toggle"
    type="button"
    :class="{ dark: isDark }"
    :aria-label="label"
    :title="label"
    @click="toggleTheme"
  >
    <span class="theme-icon" aria-hidden="true"></span>
  </button>
</template>

<style scoped>
.theme-toggle {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 4000;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  color: var(--text);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(10px);
  transition:
    transform var(--transition),
    background var(--transition),
    border-color var(--transition),
    box-shadow var(--transition);
}
.theme-toggle:hover {
  transform: translateY(-1px);
  border-color: var(--border-focus);
  background: var(--bg-panel);
}
.theme-toggle:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent), var(--shadow-lg);
}
.theme-icon {
  position: relative;
  width: 18px;
  height: 18px;
  border-radius: 999px;
  background: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
  transition: background var(--transition), box-shadow var(--transition);
}
.theme-icon::before {
  content: '';
  position: absolute;
  inset: -5px;
  border-radius: inherit;
  background:
    linear-gradient(var(--accent), var(--accent)) 50% 0 / 2px 4px no-repeat,
    linear-gradient(var(--accent), var(--accent)) 50% 100% / 2px 4px no-repeat,
    linear-gradient(var(--accent), var(--accent)) 0 50% / 4px 2px no-repeat,
    linear-gradient(var(--accent), var(--accent)) 100% 50% / 4px 2px no-repeat;
  opacity: 1;
  transition: opacity var(--transition);
}
.theme-icon::after {
  content: '';
  position: absolute;
  top: -1px;
  right: -1px;
  width: 16px;
  height: 16px;
  border-radius: inherit;
  background: var(--bg-panel);
  opacity: 0;
  transform: translate(5px, -4px);
  transition:
    opacity var(--transition),
    transform var(--transition),
    background var(--transition);
}
.theme-toggle.dark .theme-icon {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}
.theme-toggle.dark .theme-icon::before {
  opacity: 0;
}
.theme-toggle.dark .theme-icon::after {
  opacity: 1;
  transform: translate(2px, -2px);
}

@media (max-width: 760px) {
  .theme-toggle {
    right: 14px;
    bottom: 14px;
    width: 38px;
    height: 38px;
  }
}
</style>
