import { beforeEach, describe, expect, it, vi } from 'vitest'
import { chatApi } from './chat'
import { resetUnauthorizedFlag } from '../utils/authSession'

describe('chatApi and chatRequest error handling', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    resetUnauthorizedFlag()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      removeItem: vi.fn(),
      setItem: vi.fn(),
    })
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn(() => null),
      removeItem: vi.fn(),
      setItem: vi.fn(),
    })
  })

  it('triggers unauthorized redirect on chatRequest 401', async () => {
    const redirectMock = vi.fn()
    vi.stubGlobal('window', {
      location: {
        set href(val: string) { redirectMock(val) },
      },
    })

    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ detail: '登录已过期' }), { status: 401 }),
    ))

    await expect(chatApi.getSession('session-1')).rejects.toThrow()
    expect(redirectMock).toHaveBeenCalledWith('/login')

    vi.unstubAllGlobals()
    resetUnauthorizedFlag()
  })

  it('parses non-JSON 413 and 429 into friendly messages', async () => {
    // 413
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response('<html>Entity Too Large</html>', { status: 413 }),
    ))
    await expect(chatApi.getSession('session-1')).rejects.toMatchObject({
      status: 413,
      message: '上传内容过大',
    })

    // 429
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response('<html>Too Many Requests</html>', { status: 429 }),
    ))
    await expect(chatApi.getSession('session-1')).rejects.toMatchObject({
      status: 429,
      message: '操作过于频繁，请稍后再试',
    })

    // 500
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response('Internal Error', { status: 500 }),
    ))
    await expect(chatApi.getSession('session-1')).rejects.toMatchObject({
      status: 500,
      message: '服务器异常，请稍后重试',
    })

    vi.unstubAllGlobals()
  })
})
