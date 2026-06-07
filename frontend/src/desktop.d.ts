interface DesktopInfo {
  isDesktop: boolean
  platform: string
  backendBaseUrl: string
  appVersion: string
}

interface BackendStatus {
  ok: boolean
  url: string
  service?: string
  llmProvider?: string
  embeddingProvider?: string
  message: string
}

interface Window {
  desktopApp?: {
    getInfo: () => Promise<DesktopInfo>
    checkBackend: () => Promise<BackendStatus>
    onRefreshBackendStatus: (callback: () => void) => () => void
  }
}
