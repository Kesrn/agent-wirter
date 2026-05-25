import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'

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
})
