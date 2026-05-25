import {
  API_BASE_URL,
  type ApiProject, type ApiChapter, type ApiDocument, type ApiDocumentVersion, type ApiExpert, type ApiWorldEntry, type ApiCharacter,
  type ApiCharacterRelation, type ApiOutline, type ApiHiddenThread,
  type ProjectCreatePayload, type ChapterCreatePayload, type DocumentCreatePayload, type DocumentUpdatePayload, type ExpertCreatePayload,
  type WorldEntryCreatePayload, type WorldEntryUpdatePayload,
  type CharacterCreatePayload, type CharacterUpdatePayload,
  type CharacterRelationCreatePayload, type CharacterRelationUpdatePayload,
  type OutlineCreatePayload, type OutlineUpdatePayload,
  type HiddenThreadCreatePayload, type HiddenThreadUpdatePayload,
  type GenerateRequest, type SSEEnvelope,
  type ExpertUpdatePayload, type ProjectMode,
  type LLMConfigCreatePayload, type ModelListRequest,
  type LLMConfigResponse, type LLMStatusResponse, type ModelInfo,
  type LoginRequest, type LoginResponse, type MeResponse,
  type RegisterRequest, type RegisterResponse,
  type ProjectImageUploadResponse,
  type ApiChapterVersion, type ChapterVersionDiffRequest, type ChapterVersionDiffResponse,
  type DocumentVersionDiffRequest, type DocumentVersionDiffResponse,
  type ApiGenerationRecordListItem, type ApiGenerationRecord,
  type GenerationRecordUpdatePayload, type GenerationRecordDiffRequest, type GenerationRecordDiffResponse,
} from './types'

function getToken(): string | null {
  return localStorage.getItem('ai_write_token')
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

function binaryAuthHeaders(contentType: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': contentType }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

/** Parse backend error body into a user-friendly Chinese message */
function parseApiError(status: number, body: string): string {
  const statusMap: Record<number, string> = {
    400: '请求参数有误',
    401: '登录已失效，请重新登录',
    403: '没有操作权限',
    404: '资源不存在',
    422: '输入内容不符合要求',
    500: '服务器异常，请稍后重试',
  }

  // Try to parse JSON body
  let parsed: unknown
  try { parsed = JSON.parse(body) } catch { /* not JSON */ }

  if (parsed && typeof parsed === 'object') {
    const detail = (parsed as Record<string, unknown>).detail
    // detail is a plain string — likely a Chinese business error message
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
    // detail is a Pydantic validation error array
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as Record<string, unknown> | undefined
      if (first) {
        const msg = first.msg as string | undefined
        if (msg) {
          // Translate common Pydantic messages
          if (msg.includes('required') || msg.includes('missing')) return '请填写必填项'
          if (msg.includes('valid') || msg.includes('allowed')) return '输入内容不符合要求'
          return msg
        }
      }
      return '输入内容不符合要求'
    }
  }

  // Fallback to status-based message
  return statusMap[status] ?? `请求失败 (${status})`
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: authHeaders(),
    ...init,
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    if (res.status === 401) {
      localStorage.removeItem('ai_write_token')
      localStorage.removeItem('ai_write_logged_in')
      window.location.href = '/login'
    }
    throw new ApiError(res.status, parseApiError(res.status, body))
  }
  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T
  }
  return res.json()
}

/** Parse a single SSE block into event + data */
function parseSSEBlock(block: string): SSEEnvelope | null {
  let event = ''
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7)
    else if (line.startsWith('data: ')) data = line.slice(6)
  }
  if (!event && !data) return null
  let parsed: Record<string, unknown> | string
  try { parsed = JSON.parse(data) } catch { parsed = data }
  return { event: event as SSEEnvelope['event'], data: parsed }
}

/** Parse standard SSE stream (event + data blocks separated by \n\n) */
async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (envelope: SSEEnvelope) => void,
): Promise<void> {
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (value) buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const envelope = parseSSEBlock(block)
      if (envelope) onEvent(envelope)
    }
    if (done) {
      // Flush remaining buffer — last block may not end with \n\n
      if (buffer.trim()) {
        const envelope = parseSSEBlock(buffer)
        if (envelope) onEvent(envelope)
      }
      return
    }
  }
}

function sseHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

export const api = {
  // ─── Auth ───
  login: (data: LoginRequest) =>
    request<LoginResponse>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  register: (data: RegisterRequest) =>
    request<RegisterResponse>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),
  me: () => request<MeResponse>('/auth/me'),

  // ─── Projects ───
  listProjects: () => request<ApiProject[]>('/projects'),
  getProject: (id: string) => request<ApiProject>(`/projects/${id}`),
  createProject: (data: ProjectCreatePayload) =>
    request<ApiProject>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  uploadProjectImage: async (projectId: string, file: File) => {
    const res = await fetch(`${API_BASE_URL}/projects/${projectId}/assets/images`, {
      method: 'POST',
      headers: binaryAuthHeaders(file.type || 'application/octet-stream'),
      body: file,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      if (res.status === 401) {
        localStorage.removeItem('ai_write_token')
        localStorage.removeItem('ai_write_logged_in')
        window.location.href = '/login'
      }
      throw new ApiError(res.status, parseApiError(res.status, body))
    }
    return res.json() as Promise<ProjectImageUploadResponse>
  },

  // ─── Chapters ───
  listChapters: (projectId: string) => request<ApiChapter[]>(`/projects/${projectId}/chapters`),
  createChapter: (projectId: string, data: ChapterCreatePayload) =>
    request<ApiChapter>(`/projects/${projectId}/chapters`, { method: 'POST', body: JSON.stringify(data) }),
  updateChapter: (projectId: string, sequenceNumber: number, data: { title?: string; content?: string; outline?: string; status?: string }) =>
    request<ApiChapter>(`/projects/${projectId}/chapters/${sequenceNumber}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteChapter: (projectId: string, sequenceNumber: number) =>
    request<{ ok: boolean }>(`/projects/${projectId}/chapters/${sequenceNumber}`, { method: 'DELETE' }),

  // ─── Documents ───
  listDocuments: (projectId: string) => request<ApiDocument[]>(`/projects/${projectId}/documents`),
  createDocument: (projectId: string, data: DocumentCreatePayload) =>
    request<ApiDocument>(`/projects/${projectId}/documents`, { method: 'POST', body: JSON.stringify(data) }),
  updateDocument: (projectId: string, documentId: string, data: DocumentUpdatePayload) =>
    request<ApiDocument>(`/projects/${projectId}/documents/${documentId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteDocument: (projectId: string, documentId: string) =>
    request<{ ok: boolean }>(`/projects/${projectId}/documents/${documentId}`, { method: 'DELETE' }),

  // ─── Experts ───
  listExperts: (projectId: string) => request<ApiExpert[]>(`/projects/${projectId}/experts`),
  createCustomExpert: (projectId: string, data: ExpertCreatePayload) =>
    request<ApiExpert>(`/projects/${projectId}/experts`, { method: 'POST', body: JSON.stringify(data) }),
  updateExpert: (projectId: string, expertId: string, data: ExpertUpdatePayload) =>
    request<ApiExpert>(`/projects/${projectId}/experts/${expertId}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // ─── World Entries ───
  listWorldEntries: (projectId: string) => request<ApiWorldEntry[]>(`/projects/${projectId}/world-entries`),
  createWorldEntry: (projectId: string, data: WorldEntryCreatePayload) =>
    request<ApiWorldEntry>(`/projects/${projectId}/world-entries`, { method: 'POST', body: JSON.stringify(data) }),
  updateWorldEntry: (projectId: string, entryId: string, data: WorldEntryUpdatePayload) =>
    request<ApiWorldEntry>(`/projects/${projectId}/world-entries/${entryId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteWorldEntry: (projectId: string, entryId: string) =>
    request<void>(`/projects/${projectId}/world-entries/${entryId}`, { method: 'DELETE' }),

  // ─── Characters ───
  listCharacters: (projectId: string) => request<ApiCharacter[]>(`/projects/${projectId}/characters`),
  createCharacter: (projectId: string, data: CharacterCreatePayload) =>
    request<ApiCharacter>(`/projects/${projectId}/characters`, { method: 'POST', body: JSON.stringify(data) }),
  updateCharacter: (projectId: string, characterId: string, data: CharacterUpdatePayload) =>
    request<ApiCharacter>(`/projects/${projectId}/characters/${characterId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteCharacter: (projectId: string, characterId: string) =>
    request<void>(`/projects/${projectId}/characters/${characterId}`, { method: 'DELETE' }),

  // ─── Character Relations ───
  listCharacterRelations: (projectId: string) => request<ApiCharacterRelation[]>(`/projects/${projectId}/character-relations`),
  createCharacterRelation: (projectId: string, data: CharacterRelationCreatePayload) =>
    request<ApiCharacterRelation>(`/projects/${projectId}/character-relations`, { method: 'POST', body: JSON.stringify(data) }),
  updateCharacterRelation: (projectId: string, relationId: string, data: CharacterRelationUpdatePayload) =>
    request<ApiCharacterRelation>(`/projects/${projectId}/character-relations/${relationId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteCharacterRelation: (projectId: string, relationId: string) =>
    request<void>(`/projects/${projectId}/character-relations/${relationId}`, { method: 'DELETE' }),

  // ─── Outlines ───
  listOutlines: (projectId: string) => request<ApiOutline[]>(`/projects/${projectId}/outlines`),
  createOutline: (projectId: string, data: OutlineCreatePayload) =>
    request<ApiOutline>(`/projects/${projectId}/outlines`, { method: 'POST', body: JSON.stringify(data) }),
  updateOutline: (projectId: string, outlineId: string, data: OutlineUpdatePayload) =>
    request<ApiOutline>(`/projects/${projectId}/outlines/${outlineId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteOutline: (projectId: string, outlineId: string) =>
    request<void>(`/projects/${projectId}/outlines/${outlineId}`, { method: 'DELETE' }),

  // ─── Hidden Threads ───
  listHiddenThreads: (projectId: string) => request<ApiHiddenThread[]>(`/projects/${projectId}/hidden-threads`),
  createHiddenThread: (projectId: string, data: HiddenThreadCreatePayload) =>
    request<ApiHiddenThread>(`/projects/${projectId}/hidden-threads`, { method: 'POST', body: JSON.stringify(data) }),
  updateHiddenThread: (projectId: string, threadId: string, data: HiddenThreadUpdatePayload) =>
    request<ApiHiddenThread>(`/projects/${projectId}/hidden-threads/${threadId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteHiddenThread: (projectId: string, threadId: string) =>
    request<void>(`/projects/${projectId}/hidden-threads/${threadId}`, { method: 'DELETE' }),

  // ─── SSE ───
  resumeGeneration: (
    projectId: string,
    threadId: string,
    action: 'approve' | 'reject' | 'review' | 'revise',
    onEvent: (envelope: SSEEnvelope) => void,
    feedback?: string,
    signal?: AbortSignal,
    mode: ProjectMode = 'novel',
  ) => {
    const params = new URLSearchParams({ thread_id: threadId, action })
    if (feedback) params.set('feedback', feedback)
    const unitPath = mode === 'article' ? 'documents' : 'chapters'
    const url = `${API_BASE_URL}/projects/${projectId}/${unitPath}/resume?${params.toString()}`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  testExpertStream: (
    projectId: string,
    expertId: string,
    body: { test_text: string },
    onEvent: (envelope: SSEEnvelope) => void,
    signal?: AbortSignal,
  ) => {
    const url = `${API_BASE_URL}/projects/${projectId}/experts/${expertId}/test`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      body: JSON.stringify(body),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  generateStream: (
    projectId: string,
    params: GenerateRequest,
    onEvent: (envelope: SSEEnvelope) => void,
    signal?: AbortSignal,
    mode: ProjectMode = 'novel',
  ) => {
    const unitPath = mode === 'article' ? 'documents' : 'chapters'
    const url = `${API_BASE_URL}/projects/${projectId}/${unitPath}/generate`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      body: JSON.stringify(params),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  // ─── Chapter Versions ───
  listChapterVersions: (projectId: string, sequenceNumber: number) =>
    request<ApiChapterVersion[]>(`/projects/${projectId}/chapters/${sequenceNumber}/versions`),
  getChapterVersion: (projectId: string, sequenceNumber: number, versionId: string) =>
    request<ApiChapterVersion>(`/projects/${projectId}/chapters/${sequenceNumber}/versions/${versionId}`),
  diffChapterVersions: (projectId: string, sequenceNumber: number, data: ChapterVersionDiffRequest) =>
    request<ChapterVersionDiffResponse>(`/projects/${projectId}/chapters/${sequenceNumber}/versions/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── Document Versions ───
  listDocumentVersions: (projectId: string, documentId: string) =>
    request<ApiDocumentVersion[]>(`/projects/${projectId}/documents/${documentId}/versions`),
  getDocumentVersion: (projectId: string, documentId: string, versionId: string) =>
    request<ApiDocumentVersion>(`/projects/${projectId}/documents/${documentId}/versions/${versionId}`),
  restoreDocumentVersion: (projectId: string, documentId: string, versionId: string) =>
    request<ApiDocument>(`/projects/${projectId}/documents/${documentId}/versions/${versionId}/restore`, { method: 'POST' }),
  diffDocumentVersions: (projectId: string, documentId: string, data: DocumentVersionDiffRequest) =>
    request<DocumentVersionDiffResponse>(`/projects/${projectId}/documents/${documentId}/versions/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── AI Generation History ───
  listChapterGenerations: (projectId: string, sequenceNumber: number) =>
    request<ApiGenerationRecordListItem[]>(`/projects/${projectId}/chapters/${sequenceNumber}/generations`),
  listDocumentGenerations: (projectId: string, documentId: string) =>
    request<ApiGenerationRecordListItem[]>(`/projects/${projectId}/documents/${documentId}/generations`),
  getGenerationRecord: (projectId: string, generationId: string) =>
    request<ApiGenerationRecord>(`/projects/${projectId}/generations/${generationId}`),
  updateGenerationRecord: (projectId: string, generationId: string, data: GenerationRecordUpdatePayload) =>
    request<ApiGenerationRecord>(`/projects/${projectId}/generations/${generationId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  diffGenerationRecord: (projectId: string, generationId: string, data: GenerationRecordDiffRequest) =>
    request<GenerationRecordDiffResponse>(`/projects/${projectId}/generations/${generationId}/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── LLM Settings ───
  getLLMConfig: () => request<LLMConfigResponse>('/llm-settings'),
  updateLLMConfig: (data: LLMConfigCreatePayload) =>
    request<LLMConfigResponse>('/llm-settings', { method: 'PUT', body: JSON.stringify(data) }),
  fetchModels: (data: ModelListRequest) =>
    request<ModelInfo[]>('/llm-settings/models', { method: 'POST', body: JSON.stringify(data) }),
  getLLMStatus: () => request<LLMStatusResponse>('/llm-settings/status'),
}
