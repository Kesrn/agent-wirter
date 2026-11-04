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
    onSaveRequested: (callback: () => void) => () => void
    setMediaAuthToken?: (token: string) => Promise<boolean> | Promise<void> | void
    clearMediaAuthToken?: () => Promise<boolean> | Promise<void> | void
  }
}
