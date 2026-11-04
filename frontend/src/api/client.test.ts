import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, parseApiError, parseRetryAfter } from './client'
import { resetUnauthorizedFlag } from '../utils/authSession'

function sseResponse(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: done\ndata: {"message":"ok"}\n\n'))
      controller.close()
    },
  })
  return new Response(body, {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  })
}

describe('api generation routes', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      removeItem: vi.fn(),
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse()))
  })

  it('routes article generation through document endpoints', async () => {
    await api.generateStream(
      'project-1',
      { mode: 'continue', document_id: 'doc-1', chapter_num: 1 },
      vi.fn(),
      undefined,
      'article',
    )

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/documents/generate')
  })

  it('routes novel generation through chapter endpoints', async () => {
    await api.generateStream(
      'project-1',
      { mode: 'continue', chapter_id: 'chapter-1', chapter_num: 1 },
      vi.fn(),
      undefined,
      'novel',
    )

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/chapters/generate')
  })

  it('finalizes a chapter through the dedicated final-review endpoint', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response('{}', { status: 200 }))

    await api.finalizeChapter('project-1', 2, { content: '确认后的章节正文' })

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/chapters/2/finalize')
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ content: '确认后的章节正文' })
  })

  it('routes article resume through document endpoints', async () => {
    await api.resumeGeneration('project-1', 'thread-1', 'reject', vi.fn(), undefined, undefined, 'article')

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/documents/resume')
  })

  it('routes novel resume through chapter endpoints', async () => {
    await api.resumeGeneration('project-1', 'thread-1', 'reject', vi.fn(), undefined, undefined, 'novel')

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/chapters/resume')
  })

  it('sends chapter finalization as one finalize resume action', async () => {
    await api.resumeGeneration('project-1', 'thread-1', 'finalize', vi.fn(), undefined, undefined, 'novel')

    const fetchMock = vi.mocked(fetch)
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/projects/project-1/chapters/resume')
    expect(url).toContain('action=finalize')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('sends task card resume payload in request body instead of query string', async () => {
    const taskCard = JSON.stringify({ core_task: 'x'.repeat(10_000) })

    await api.resumeGeneration(
      'project-1',
      'thread-1',
      'approve_task_card',
      vi.fn(),
      undefined,
      undefined,
      'novel',
      taskCard,
    )

    const fetchMock = vi.mocked(fetch)
    const url = String(fetchMock.mock.calls[0][0])
    const init = fetchMock.mock.calls[0][1]
    expect(url).toContain('thread_id=thread-1')
    expect(url).toContain('action=approve_task_card')
    expect(url).not.toContain('task_card=')
    expect(JSON.parse(String(init?.body)).task_card).toBe(taskCard)
  })

  it('sends resume feedback in request body instead of query string', async () => {
    const feedback = '请重点修改：'.repeat(1000)

    await api.resumeGeneration('project-1', 'thread-1', 'revise', vi.fn(), feedback, undefined, 'novel')

    const fetchMock = vi.mocked(fetch)
    const url = String(fetchMock.mock.calls[0][0])
    const init = fetchMock.mock.calls[0][1]
    expect(url).toContain('action=revise')
    expect(url).not.toContain('feedback=')
    expect(JSON.parse(String(init?.body)).feedback).toBe(feedback)
  })

  it('routes AI generation history through chapter and document endpoints', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response('[]', { status: 200 }))

    await api.listChapterGenerations('project-1', 2)
    await api.listDocumentGenerations('project-1', 'doc-1')

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/chapters/2/generations')
    expect(String(fetchMock.mock.calls[1][0])).toContain('/projects/project-1/documents/doc-1/generations')
  })

  it('updates and diffs AI generation records through project-scoped endpoints', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response('{}', { status: 200 }))

    await api.updateGenerationRecord('project-1', 'gen-1', { status: 'applied' })
    await api.diffGenerationRecord('project-1', 'gen-1', { current_content: '当前正文' })

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/generations/gen-1')
    expect(fetchMock.mock.calls[0][1]?.method).toBe('PATCH')
    expect(String(fetchMock.mock.calls[1][0])).toContain('/projects/project-1/generations/gen-1/diff')
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST')
  })

  it('uploads project images as binary body', async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response(JSON.stringify({
      url: '/media/projects/project-1/images/a.png',
      filename: 'a.png',
      content_type: 'image/png',
      size: 4,
    }), { status: 200 }))

    const file = new File([new Uint8Array([1, 2, 3, 4])], 'a.png', { type: 'image/png' })
    const result = await api.uploadProjectImage('project-1', file)

    const fetchMock = vi.mocked(fetch)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/projects/project-1/assets/images')
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST')
    expect((fetchMock.mock.calls[0][1]?.headers as Record<string, string>)['Content-Type']).toBe('image/png')
    expect(fetchMock.mock.calls[0][1]?.body).toBe(file)
    expect(result.url).toBe('/media/projects/project-1/images/a.png')
  })

  it('parses Retry-After header correctly', () => {
    const resValid = new Response('', { headers: { 'Retry-After': '30' } })
    expect(parseRetryAfter(resValid)).toBe(30)

    const resInvalid = new Response('', { headers: { 'Retry-After': 'invalid' } })
    expect(parseRetryAfter(resInvalid)).toBeUndefined()

    const resNegative = new Response('', { headers: { 'Retry-After': '-5' } })
    expect(parseRetryAfter(resNegative)).toBeUndefined()

    const resZero = new Response('', { headers: { 'Retry-After': '0' } })
    expect(parseRetryAfter(resZero)).toBeUndefined()

    const resMissing = new Response('')
    expect(parseRetryAfter(resMissing)).toBeUndefined()
  })

  it('parses error messages with status fallback, json detail, and 429 countdown', () => {
    // 429 with retryAfter
    expect(parseApiError(429, '', 30)).toBe('操作过于频繁，请 30 秒后重试')
    // 429 without retryAfter, non-json
    expect(parseApiError(429, '<html>Too Many Requests</html>')).toBe('操作过于频繁，请稍后再试')
    // 429 with custom detail
    expect(parseApiError(429, JSON.stringify({ detail: 'IP 限流' }))).toBe('IP 限流')

    // 413 non-json
    expect(parseApiError(413, '<html>Request Entity Too Large</html>')).toBe('上传内容过大')

    // 500 non-json
    expect(parseApiError(500, 'Internal Server Error')).toBe('服务器异常，请稍后重试')

    // 400 with detail
    expect(parseApiError(400, JSON.stringify({ detail: '模型服务地址不被允许' }))).toBe('模型服务地址不被允许')

    // 403 with detail
    expect(parseApiError(403, JSON.stringify({ detail: '注册已关闭' }))).toBe('注册已关闭')

    // 401 with detail
    expect(parseApiError(401, JSON.stringify({ detail: '用户名或密码错误' }))).toBe('用户名或密码错误')
  })

  it('does not trigger unauthorized redirect on login failure', async () => {
    resetUnauthorizedFlag()
    const redirectMock = vi.fn()
    vi.stubGlobal('window', {
      location: {
        set href(val: string) { redirectMock(val) },
      },
    })

    vi.mocked(fetch).mockImplementation(async () =>
      new Response(JSON.stringify({ detail: '用户名或密码错误' }), { status: 401 }),
    )

    await expect(api.login({ username: 'user', password: 'bad' })).rejects.toMatchObject({
      status: 401,
      message: '用户名或密码错误',
    })

    expect(redirectMock).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
    resetUnauthorizedFlag()
  })

  it('triggers unauthorized redirect on regular request 401', async () => {
    resetUnauthorizedFlag()
    const redirectMock = vi.fn()
    vi.stubGlobal('window', {
      location: {
        set href(val: string) { redirectMock(val) },
      },
    })

    vi.mocked(fetch).mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'token expired' }), { status: 401 }),
    )

    await expect(api.listProjects()).rejects.toMatchObject({
      status: 401,
      message: 'token expired',
    })

    expect(redirectMock).toHaveBeenCalledWith('/login')
    vi.unstubAllGlobals()
    resetUnauthorizedFlag()
  })

  it('handles image upload 413 and 400 errors correctly', async () => {
    const file = new File([new Uint8Array([1])], 'test.png', { type: 'image/png' })

    // 413 non-JSON
    vi.mocked(fetch).mockImplementation(async () =>
      new Response('<html>Entity Too Large</html>', { status: 413 }),
    )
    await expect(api.uploadProjectImage('project-1', file)).rejects.toMatchObject({
      status: 413,
      message: '图片过大',
    })

    // 400 with detail
    vi.mocked(fetch).mockImplementation(async () =>
      new Response(JSON.stringify({ detail: '不支持的文件类型' }), { status: 400 }),
    )
    await expect(api.uploadProjectImage('project-1', file)).rejects.toMatchObject({
      status: 400,
      message: '不支持的文件类型',
    })
  })
})
