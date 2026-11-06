import { describe, it, expect } from 'vitest'
import { buildCsp } from './csp'

describe('Content-Security-Policy builder (buildCsp)', () => {
  it('generates compliant CSP for web builds without backend origin', () => {
    const csp = buildCsp({ desktop: false, apiOrigin: '' })

    // No unsafe-eval anywhere
    expect(csp).not.toContain('unsafe-eval')

    // script-src must not contain unsafe-inline or wildcards
    const scriptSrcMatch = csp.match(/script-src\s+([^;]+)/)
    expect(scriptSrcMatch).not.toBeNull()
    const scriptSrc = scriptSrcMatch![1]
    expect(scriptSrc).toBe("'self'")
    expect(scriptSrc).not.toContain('unsafe-inline')
    expect(scriptSrc).not.toContain('*')

    // Web build must not hardcode 127.0.0.1
    expect(csp).not.toContain('127.0.0.1')

    // object-src 'none'
    expect(csp).toContain("object-src 'none'")

    // base-uri and form-action
    expect(csp).toContain("base-uri 'self'")
    expect(csp).toContain("form-action 'self'")

    // style-src includes unsafe-inline for Vue dynamic style bindings
    expect(csp).toContain("style-src 'self' 'unsafe-inline'")

    // frame-ancestors must not be in meta CSP (it is ineffective per spec)
    expect(csp).not.toContain('frame-ancestors')

    // img-src includes self, data, blob, https
    expect(csp).toContain("img-src 'self' data: blob: https:")
  })

  it('generates compliant CSP for desktop builds including backend origin', () => {
    const backendOrigin = 'http://127.0.0.1:8765'
    const csp = buildCsp({ desktop: true, apiOrigin: backendOrigin })

    // No unsafe-eval
    expect(csp).not.toContain('unsafe-eval')

    // script-src strictly 'self'
    const scriptSrcMatch = csp.match(/script-src\s+([^;]+)/)
    expect(scriptSrcMatch![1]).toBe("'self'")

    // Desktop includes backend origin in img-src (for /media images)
    expect(csp).toContain(`img-src 'self' data: blob: https: ${backendOrigin}`)

    // Desktop includes backend origin in connect-src (for API / SSE calls)
    expect(csp).toContain(`connect-src 'self' ${backendOrigin}`)

    // object-src 'none'
    expect(csp).toContain("object-src 'none'")
  })

  it('handles custom ports or domain origins dynamically', () => {
    const csp = buildCsp({ desktop: true, apiOrigin: 'http://localhost:9000' })
    expect(csp).toContain('http://localhost:9000')
    expect(csp).not.toContain('8765')
  })
})
