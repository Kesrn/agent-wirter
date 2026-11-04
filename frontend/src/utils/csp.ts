export interface BuildCspOptions {
  apiOrigin?: string
  desktop?: boolean
}

/**
 * Builds Content-Security-Policy directive string.
 *
 * Directives summary:
 * - default-src 'self': default restriction to current origin
 * - script-src 'self': strictly disallow 'unsafe-inline' and 'unsafe-eval'
 * - style-src 'self' 'unsafe-inline': Vue dynamic style bindings require inline styles; this is an accepted trade-off
 * - img-src 'self' data: blob: https: [backendOrigin if desktop]: allow local assets, data/blob, and external HTTPS images; desktop includes backend origin for /media
 * - font-src 'self' data:: allow fonts from self and data URI
 * - connect-src 'self' [backendOrigin]: allow XHR/fetch/SSE to self and explicit backend origin (if absolute URL)
 * - object-src 'none': block plugins (Flash, Java, etc.)
 * - base-uri 'self': prevent base tag hijacking
 * - form-action 'self': restrict form submissions
 *
 * Note: 'frame-ancestors' is ignored when set via <meta http-equiv="Content-Security-Policy">
 * according to the W3C CSP specification. For web deployments, anti-clickjacking protection
 * (frame-ancestors 'self') should be configured by the reverse proxy (e.g. Nginx) in HTTP response headers.
 */
export function buildCsp(options: BuildCspOptions = {}): string {
  const { apiOrigin = '', desktop = false } = options
  const cleanOrigin = apiOrigin.trim()

  const imgSrcParts = ["'self'", 'data:', 'blob:', 'https:']
  if (desktop && cleanOrigin) {
    imgSrcParts.push(cleanOrigin)
  }

  const connectSrcParts = ["'self'"]
  if (cleanOrigin) {
    connectSrcParts.push(cleanOrigin)
  }

  const directives = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    `img-src ${imgSrcParts.join(' ')}`,
    "font-src 'self' data:",
    `connect-src ${connectSrcParts.join(' ')}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ]

  return directives.join('; ')
}
