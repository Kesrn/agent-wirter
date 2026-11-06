/**
 * Pure functions for Electron media request Authorization header injection.
 * Free of Electron dependencies for clean unit testing.
 */

/**
 * Validates a media authentication token.
 * Must be a non-empty string, length 1..4096 after trim.
 */
function isValidToken(token) {
  if (typeof token !== 'string') return false
  const trimmed = token.trim()
  return trimmed.length >= 1 && trimmed.length <= 4096
}

/**
 * Validates the IPC sender frame URL.
 * Dev: ELECTRON_RENDERER_URL or http://127.0.0.1:5173
 * Prod: file://
 */
function isValidSenderUrl(url, options = {}) {
  if (typeof url !== 'string' || !url) return false
  const isDev = options.isDev ?? false
  const devUrl = options.devRendererUrl || 'http://127.0.0.1:5173'
  if (isDev) {
    return url.startsWith(devUrl)
  }
  return url.startsWith('file://')
}

/**
 * Determines whether Authorization header should be injected for a request.
 * - Must have a valid non-empty token
 * - URL must match backend origin (protocol, hostname, port)
 * - URL pathname must start with /media/ or equal /media
 */
function shouldInjectAuthHeader(requestUrl, backendOrigin, token) {
  if (!isValidToken(token)) return false
  if (!requestUrl || !backendOrigin) return false

  try {
    const req = new URL(requestUrl)
    const target = new URL(backendOrigin)

    if (req.protocol !== target.protocol) return false
    if (req.hostname !== target.hostname) return false

    const reqPort = req.port || (req.protocol === 'https:' ? '443' : '80')
    const targetPort = target.port || (target.protocol === 'https:' ? '443' : '80')
    if (reqPort !== targetPort) return false

    if (!req.pathname.startsWith('/media/') && req.pathname !== '/media') {
      return false
    }

    return true
  } catch {
    return false
  }
}

/**
 * Adds Authorization: Bearer <token> to request headers.
 * Returns a new headers object without mutating original.
 */
function buildHeadersWithAuth(requestHeaders, token) {
  const headers = { ...(requestHeaders || {}) }
  if (isValidToken(token)) {
    headers['Authorization'] = `Bearer ${token.trim()}`
  }
  return headers
}

module.exports = {
  isValidToken,
  isValidSenderUrl,
  shouldInjectAuthHeader,
  buildHeadersWithAuth,
}
