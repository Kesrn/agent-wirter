<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import ToastMessage from './components/ToastMessage.vue'
import ThemeToggle from './components/ThemeToggle.vue'

const route = useRoute()
const showThemeToggle = computed(() => !['login', 'register'].includes(String(route.name)))
const isDesktopRuntime = ref(false)
const desktopInfo = ref<DesktopInfo | null>(null)
const backendStatus = ref<BackendStatus | null>(null)
const backendChecking = ref(false)
let refreshDisposer: (() => void) | undefined
let backendCheckTimer: number | undefined

const backendStatusLabel = computed(() => {
  if (backendChecking.value && !backendStatus.value) return '检查中'
  if (!backendStatus.value) return '等待连接'
  return backendStatus.value.ok ? '后端在线' : '后端未连接'
})

const backendStatusDetail = computed(() => {
  if (!backendStatus.value) return desktopInfo.value?.backendBaseUrl ?? 'http://localhost:8000'
  if (backendStatus.value.ok) {
    const providers = [backendStatus.value.llmProvider, backendStatus.value.embeddingProvider].filter(Boolean).join(' / ')
    return providers ? `${backendStatus.value.url} · ${providers}` : backendStatus.value.url
  }
  return `${backendStatus.value.url} · ${backendStatus.value.message}`
})

async function refreshBackendStatus() {
  if (!window.desktopApp) return
  backendChecking.value = true
  try {
    backendStatus.value = await window.desktopApp.checkBackend()
  } finally {
    backendChecking.value = false
  }
}

onMounted(async () => {
  if (!window.desktopApp || import.meta.env.VITE_DESKTOP !== 'true') return
  isDesktopRuntime.value = true
  document.documentElement.classList.add('desktop-runtime')
  desktopInfo.value = await window.desktopApp.getInfo()
  await refreshBackendStatus()
  refreshDisposer = window.desktopApp.onRefreshBackendStatus(refreshBackendStatus)
  backendCheckTimer = window.setInterval(refreshBackendStatus, 30_000)
})

onUnmounted(() => {
  document.documentElement.classList.remove('desktop-runtime')
  refreshDisposer?.()
  if (backendCheckTimer) window.clearInterval(backendCheckTimer)
})
</script>

<template>
  <div v-if="isDesktopRuntime" class="desktop-status-bar">
    <div class="desktop-status-main">
      <span class="desktop-app-mark" aria-hidden="true"></span>
      <span class="desktop-app-title">AI 创作平台桌面端</span>
      <span class="desktop-version">v{{ desktopInfo?.appVersion ?? '0.0.0' }}</span>
    </div>
    <div class="desktop-backend" :class="{ online: backendStatus?.ok, offline: backendStatus && !backendStatus.ok }">
      <span class="desktop-backend-dot" aria-hidden="true"></span>
      <span class="desktop-backend-label">{{ backendStatusLabel }}</span>
      <span class="desktop-backend-detail">{{ backendStatusDetail }}</span>
      <button class="desktop-status-action" type="button" :disabled="backendChecking" @click="refreshBackendStatus">
        {{ backendChecking ? '检查中' : '刷新' }}
      </button>
    </div>
  </div>
  <router-view />
  <ToastMessage />
  <ThemeToggle v-if="showThemeToggle" />
</template>
