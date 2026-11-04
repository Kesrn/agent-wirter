/**
 * Pure functions for Electron navigation security and window open handling.
 * Free of Electron dependencies for clean unit testing.
 */

/**
 * Checks whether a URL is an allowed external HTTP or HTTPS URL.
 * Strictly disallows pseudo-protocols (javascript:, data:), local files (file:), etc.
 */
function isHttpOrHttpsUrl(url) {
  if (typeof url !== 'string' || !url) return false
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

/**
 * Checks whether a navigation URL is an internal application page.
 */
function isInternalAppUrl(url, options = {}) {
  if (typeof url !== 'string' || !url) return false
  const isDev = options.isDev ?? false
  const devRendererUrl = options.devRendererUrl || 'http://127.0.0.1:5173'
  const appFilePrefix = options.appFilePrefix || ''

  if (isDev) {
    return url.startsWith(devRendererUrl)
  }

  if (appFilePrefix) {
    return url.startsWith(appFilePrefix)
  }

  // Fallback: file:// protocol with dist-desktop in path
  return url.startsWith('file://') && url.includes('dist-desktop')
}

/**
 * Handles window open requests.
 * External HTTP(S) links are forwarded to system browser (shell.openExternal).
 * All new app window creation is denied.
 */
function handleWindowOpen(url, callbacks = {}) {
  const openExternal = callbacks.openExternal || (() => {})
  if (isHttpOrHttpsUrl(url)) {
    openExternal(url)
  }
  return { action: 'deny' }
}

/**
 * Handles webContents will-navigate events.
 * Only internal application URLs are allowed to navigate within the webContents.
 * External HTTP(S) URLs are prevented and forwarded to system browser.
 * Dangerous schemes (javascript:, data:, file:///etc/passwd) are blocked entirely.
 */
function handleWillNavigate(url, options = {}, callbacks = {}) {
  const openExternal = callbacks.openExternal || (() => {})
  const preventDefault = callbacks.preventDefault || (() => {})

  if (isInternalAppUrl(url, options)) {
    return 'allow'
  }

  preventDefault()

  if (isHttpOrHttpsUrl(url)) {
    openExternal(url)
    return 'external'
  }

  return 'deny'
}

module.exports = {
  isHttpOrHttpsUrl,
  isInternalAppUrl,
  handleWindowOpen,
  handleWillNavigate,
}
