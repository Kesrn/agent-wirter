/**
 * 对话式创作（Chat Creative Assistant）API 封装。
 *
 * 接口路径与 JSON 结构对齐《对话式创作方案 v2》§12 后端契约草案：
 *   POST /api/chat/sessions
 *   GET  /api/chat/sessions/{id}
 *   POST /api/chat/sessions/{id}/summary/refresh
 *   POST /api/chat/sessions/{id}/messages
 *   POST /api/chat/sessions/{id}/messages/{message_id}/confirm
 *   POST /api/chat/messages/{message_id}/clarify
 *   POST /api/chat/executions/{id}/candidate/accept|discard|revise
 *   POST /api/chat/change-sets/{id}/revert
 *   POST /api/chat/change-sets/revert-session
 *   POST /api/chat/executions/{id}/finalize
 *
 * 后端尚未就绪时，每个方法在请求失败（网络错误 / 404 / 405 / 501）时自动回退到
 * 本地 mock 实现，保证前端 UI 全链路（消息→意图→任务卡→确认→候选稿→采用）可跑通。
 * mock 实现带幂等缓存：同一 `idempotency_key` 重复请求返回首次结果。
 */
import { API_BASE_URL } from './types'
import { ApiError } from './client'
import { getAuthToken } from '../utils/authSession'

// ─── §12 契约类型 ───

export type ConversationIntent = 'knowledge_query' | 'discussion' | 'rewrite' | 'data_operation' | 'flow_control'

export interface ConversationPlanStep {
  intent: ConversationIntent
  target?: string | Record<string, unknown> | null
  query?: string | null
  based_on?: string | null
}

export interface ConversationPlan {
  plan_id: string
  steps: ConversationPlanStep[]
  dependencies: Array<{ step: number; depends_on: number[] }>
}

export interface ConversationTaskCardConstraints {
  must_preserve?: string[]
  must_not_change?: string[]
  target_word_count?: number
  /** 用户原始要求（与 resolved_instruction 并存便于追溯） */
  user_instruction?: string
}

export interface ConversationTaskCardContextSnapshot {
  chapter_no?: number | null
  version_id?: string | null
  selected_text?: string | null
  current_paragraph?: string | null
}

export interface ConversationTaskCardTarget {
  chapter_no?: number | null
  version_id?: string | null
}

/** §12.3 任务卡（rewrite 示例） */
export interface ConversationTaskCard {
  schema_version?: number
  operation_type: string
  target?: ConversationTaskCardTarget
  base_version_id?: string | null
  constraints?: ConversationTaskCardConstraints
  risk_level?: 'L0' | 'L1' | 'L2' | 'L3' | 'L4' | string
  confirmation_required?: boolean
  idempotency_key?: string | null
  /** v2.4：LLM 解析后的具体指令（方案 A 模板组装；无 operation/requirements 时为 null） */
  resolved_instruction?: string | null
  /** v2.4：细粒度操作白名单值（如 polish_chapter） */
  operation?: string | null
  mentioned_entities?: string[]
  requirements?: string[]
  /** v2.4：分类来源（llm 主路径 / rule 规则降级），存根溯源 */
  router_source?: 'llm' | 'rule' | string
  prompt_version?: string
  /** P1-2：AI 理解时的工作对象上下文快照（章节/版本/选中文本/当前段落） */
  context_snapshot?: ConversationTaskCardContextSnapshot | null
}

export interface ConversationClarificationOption {
  value: string
  label: string
}

export interface ConversationClarificationQuestion {
  id: string
  question: string
  type?: 'single_choice' | 'multi_choice' | 'free_text' | 'number'
  options?: ConversationClarificationOption[]
  required?: boolean
  reason?: string
}

export interface ConversationClarification {
  needs_clarification: boolean
  questions: ConversationClarificationQuestion[]
  reason?: string
}

/** §12.2 消息响应：先回意图，不直接执行 */
export interface ConversationSendMessageResponse {
  message_id: string
  intent: ConversationIntent
  plan?: ConversationPlan | null
  requires_confirmation: boolean
  task_card?: ConversationTaskCard | null
  clarification?: ConversationClarification | null
  content?: string | null
  /** 后端消息接口返回的助手自然语言回复 */
  reply?: string | null
  /** v2.4：识别到意图但第一版不支持执行（如复合任务/定稿）。现有字段不变，新增两个可选字段。 */
  intent_unsupported?: boolean
  unsupported_reason?: string | null
}

export interface ConversationCandidate {
  id: string
  execution_id: string
  content: string
  base_version_id: string | null
  status: 'candidate' | 'applied' | 'discarded'
  created_at: string
}

export interface ConversationChangeSetObject {
  object_kind: string
  object_id: string
  before_ref: string | null
  after_ref: string | null
}

export interface ConversationChangeSet {
  id: string
  session_id: string
  execution_id: string | null
  operation_type: string
  intent: ConversationIntent
  status: 'pending' | 'applied' | 'reverted' | 'failed'
  objects: ConversationChangeSetObject[]
  base_version_id: string | null
  created_at: string
  applied_at?: string | null
  reverted_at?: string | null
}

export interface ConversationConfirmResponse {
  execution_id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  candidate?: ConversationCandidate | null
  change_set?: ConversationChangeSet | null
  /** 生成失败时的原因（status=failed 且 candidate 为空时必填） */
  error_message?: string | null
}

export interface ConversationAcceptResponse {
  change_set_id: string
  new_version_id: string
}

export interface ConversationSession {
  id: string
  project_id: string
  title: string
  summary: string | null
  created_at: string
  updated_at: string
}

type ChatSessionPayload = {
  session_id?: string
  id?: string
  project_id: string
  title?: string | null
  summary?: string | null
  created_at?: string | null
  updated_at?: string | null
}

type ChatCandidatePayload = {
  id?: string
  record_id?: string
  execution_id?: string | null
  content: string
  word_count?: number
  base_version_id?: string | null
  status: 'candidate' | 'applied' | 'discarded'
  created_at?: string | null
}

function normalizeSession(payload: ChatSessionPayload): ConversationSession {
  const id = payload.id ?? payload.session_id
  if (!id) throw new ChatApiError(502, '聊天会话响应缺少会话 ID', null, payload)
  return {
    id,
    project_id: payload.project_id,
    title: payload.title ?? '',
    summary: payload.summary ?? null,
    created_at: payload.created_at ?? new Date().toISOString(),
    updated_at: payload.updated_at ?? new Date().toISOString(),
  }
}

function normalizeCandidate(payload: ChatCandidatePayload, executionId: string): ConversationCandidate {
  const id = payload.id ?? payload.record_id
  if (!id) throw new ChatApiError(502, '候选稿响应缺少候选稿 ID', null, payload)
  return {
    id,
    execution_id: payload.execution_id ?? executionId,
    content: payload.content,
    base_version_id: payload.base_version_id ?? null,
    status: payload.status,
    created_at: payload.created_at ?? new Date().toISOString(),
  }
}

function normalizeConfirmResponse(payload: {
  execution_id: string
  status: ConversationConfirmResponse['status']
  candidate?: ChatCandidatePayload | null
  change_set?: ConversationChangeSet | null
  error_message?: string | null
}): ConversationConfirmResponse {
  return {
    execution_id: payload.execution_id,
    status: payload.status,
    candidate: payload.candidate ? normalizeCandidate(payload.candidate, payload.execution_id) : null,
    change_set: payload.change_set
      ? { ...payload.change_set, execution_id: payload.change_set.execution_id ?? payload.execution_id }
      : null,
    // 透传后端失败原因，store 才能展示具体原因而非默认文案
    error_message: payload.error_message ?? null,
  }
}

export interface ConversationMessageRecord {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  intent?: ConversationIntent | null
  plan?: ConversationPlan | null
  task_card?: ConversationTaskCard | null
  execution_id?: string | null
  idempotency_key?: string | null
  created_at: string
}

/** 前端当前工作对象上下文（用于任务卡定位与 mock 生成候选）。 */
export interface ConversationContextInput {
  chapterNo?: number | null
  chapterTitle?: string | null
  draft?: string
  currentVersionId?: string | null
  /** 补锁 3：用户选中的文本与光标所在段落（供 LLM 理解"改这段/改选中部分"） */
  selection?: string | null
  currentParagraph?: string | null
}

// ─── 错误语义（§12.6）───

export class ChatApiError extends Error {
  status: number
  code: string | null
  details: Record<string, unknown> | null

  constructor(status: number, message: string, code: string | null, details: Record<string, unknown> | null) {
    super(message)
    this.name = 'ChatApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

function parseChatError(status: number, body: string): ChatApiError {
  let parsed: unknown
  try {
    parsed = JSON.parse(body)
  } catch {
    /* 非 JSON body */
  }
  if (parsed && typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>
    const code = typeof obj.code === 'string' ? obj.code : null
    if (typeof obj.detail === 'string' && obj.detail.trim()) {
      return new ChatApiError(status, obj.detail, code, obj)
    }
    if (code === 'version_conflict') {
      const latest = typeof obj.latest_version_id === 'string' ? obj.latest_version_id : ''
      return new ChatApiError(status, '当前章节已在其他操作中更新，请基于最新版本重新生成', code, { latest_version_id: latest })
    }
    if (code === 'idempotency_duplicate') {
      return new ChatApiError(status, '检测到重复提交，已返回首次执行结果', code, obj)
    }
    if (code === 'clarification_required') {
      return new ChatApiError(status, '需要进一步澄清后才能执行', code, obj)
    }
    if (code === 'intent_unsupported') {
      return new ChatApiError(status, '当前版本暂不支持该操作类型', code, obj)
    }
  }
  return new ChatApiError(status, `请求失败 (${status})`, null, null)
}

// ─── 请求辅助 ───

async function chatRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getAuthToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(`${API_BASE_URL}${path}`, { headers, ...init })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw parseChatError(res.status, text)
  }
  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T
  }
  return res.json() as Promise<T>
}

/** 后端尚未实现对应路由（404/405/501）或网络不可达时，回退到本地 mock。 */
function isBackendNotReady(e: unknown): boolean {
  // 修复 2（彻底隔离）：VITE_CHAT_MOCK != 1（生产/默认）时**任何错误都上抛**，
  // 绝不返回 mock——否则采用接口真实 404 会落入 mock 分支返回假
  // change_set_id/new_version_id，界面显示"采用成功"但数据库没有变化。
  // VITE_CHAT_MOCK == 1 时：仅"后端未部署"信号（404/405/501）才回退 mock；
  // TypeError（网络中断/代码 bug）仍上抛，避免 mock 掩盖真实错误。
  const mockEnabled = import.meta.env?.VITE_CHAT_MOCK === '1'
  if (!mockEnabled) return false
  if (e instanceof ApiError) return e.status === 404 || e.status === 405 || e.status === 501
  if (e instanceof ChatApiError) return e.status === 404 || e.status === 405 || e.status === 501
  return false
}

async function withMockFallback<T>(real: () => Promise<T>, mock: () => T | Promise<T>): Promise<T> {
  try {
    return await real()
  } catch (e: unknown) {
    if (isBackendNotReady(e)) return mock()
    throw e
  }
}

// ─── Mock 后端（后端未就绪时打通 UI 流）───

interface MockBackendState {
  sessions: ConversationSession[]
  messages: Record<string, ConversationMessageRecord[]>
}

const mockState: MockBackendState = { sessions: [], messages: {} }
let mockSessionSeq = 0
let mockMsgSeq = 0
let mockExecSeq = 0
const mockIdempotencyCache = new Map<string, unknown>()

function mockEnsureSession(projectId: string): ConversationSession {
  const existing = mockState.sessions.find(s => s.project_id === projectId)
  if (existing) return existing
  mockSessionSeq += 1
  const session: ConversationSession = {
    id: `mock-session-${mockSessionSeq}`,
    project_id: projectId,
    title: '对话创作',
    summary: '本会话将记录你的创作讨论与已确认任务约束；正式内容始终走任务卡确认与人工审核。',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }
  mockState.sessions.push(session)
  mockState.messages[session.id] = []
  return session
}

function mockTaskCard(ctx: ConversationContextInput, content: string, hasConflict: boolean): ConversationTaskCard {
  const constraints: ConversationTaskCardConstraints = { must_preserve: [], must_not_change: ['人物设定', '大纲结构'] }
  if (/保留/.test(content)) constraints.must_preserve = ['结尾伏笔', '人物关系']
  else constraints.must_preserve = ['现有剧情主线']
  const wordMatch = content.match(/(\d+)\s*字/)
  if (wordMatch) constraints.target_word_count = Number(wordMatch[1])
  return {
    schema_version: 1,
    operation_type: 'chapter_rewrite',
    target: { chapter_no: ctx.chapterNo ?? null, version_id: ctx.currentVersionId ?? null },
    base_version_id: ctx.currentVersionId ?? null,
    constraints,
    risk_level: hasConflict ? 'L2' : 'L1',
    confirmation_required: true,
    idempotency_key: null,
  }
}

function buildMockSendResponse(_sessionId: string, content: string, ctx: ConversationContextInput): ConversationSendMessageResponse {
  const messageId = `mock-msg-${++mockMsgSeq}`
  const text = content.trim()
  const lower = text.toLowerCase()
  const base: ConversationSendMessageResponse = {
    message_id: messageId,
    intent: 'discussion',
    requires_confirmation: false,
    clarification: null,
    content: '这是创作讨论（mock）。你可以说：压缩本章、改写某段、查询角色设定、或撤销本轮修改。',
  }

  if (/查询|检索|搜|查找|在哪里|最后出现|是谁|什么关系|资料/.test(lower)) {
    return {
      ...base,
      intent: 'knowledge_query',
      requires_confirmation: false,
      content: '【知识查询】已按知识库优先链路完成检索（mock）。接入真实后端后这里会返回检索证据与引用来源。',
      plan: {
        plan_id: `plan-${messageId}`,
        steps: [{ intent: 'knowledge_query', target: '知识库', query: text }],
        dependencies: [],
      },
    }
  }
  if (/撤销|回退|恢复|不要这版|取消本轮/.test(lower)) {
    return {
      ...base,
      intent: 'flow_control',
      requires_confirmation: false,
      content: '【流程控制】识别到“撤销”意图（mock）。点击下方“撤销本轮”按钮可回滚本会话已应用的全部修改。',
    }
  }
  if (/改大纲|改人物|改名|改设定|删除角色|合并角色/.test(lower)) {
    return {
      ...base,
      intent: 'data_operation',
      requires_confirmation: false,
      content: '第一版仅支持单章局部重写。跨对象数据操作（大纲 / 人物 / 设定）暂不支持，请到工作流模式完成，或等待后续版本。',
    }
  }
  if (/压缩|精简|删减|缩短|重写|改写|修改|润色|调整|扩写|续写|打磨/.test(lower)) {
    const needsClarification = /压缩|精简|删减|缩短/.test(lower) && /伏笔|保留/.test(lower)
    return {
      ...base,
      intent: 'rewrite',
      requires_confirmation: true,
      task_card: mockTaskCard(ctx, text, needsClarification),
      clarification: needsClarification
        ? {
            needs_clarification: true,
            reason: '同时提到“压缩”与“保留伏笔”，需要确认保留范围。',
            questions: [
              {
                id: 'preserve_scope',
                question: '“保留伏笔”具体指保留哪些内容？',
                type: 'multi_choice',
                required: false,
                options: [
                  { value: '结尾伏笔', label: '结尾伏笔' },
                  { value: '人物关系线', label: '人物关系线' },
                  { value: '具体情节', label: '具体情节' },
                ],
              },
            ],
          }
        : null,
      content: '我理解你希望对本章做局部重写。请核对下面的任务卡并确认。',
    }
  }
  return base
}

function mockSendMessage(sessionId: string, content: string, idempotencyKey: string | undefined, ctx: ConversationContextInput): ConversationSendMessageResponse {
  const cacheKey = `${sessionId}:${idempotencyKey ?? 'none'}`
  if (idempotencyKey) {
    const cached = mockIdempotencyCache.get(cacheKey)
    if (cached) return cached as ConversationSendMessageResponse
  }
  const response = buildMockSendResponse(sessionId, content, ctx)
  if (idempotencyKey) mockIdempotencyCache.set(cacheKey, response)
  return response
}

function mockRewrite(draft: string): string {
  const base = (draft ?? '').trim()
  if (!base) {
    return '〔mock 候选稿〕当前章节正文为空。这是对话助手在接入真实后端前生成的占位候选，用于验证“原文 / 候选稿对照”与“采用为草稿”的完整流转。'
  }
  const cleaned = base
    .split('\n')
    .map(line => line.replace(/，{2,}/g, '，').replace(/(。|！|？)\1+/g, '$1').trimEnd())
    .reduce<string[]>((acc, line) => {
      if (!line && acc.length && acc[acc.length - 1] === '') return acc
      acc.push(line)
      return acc
    }, [])
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
  return `${cleaned}\n\n〔mock：局部重写候选稿，保留主线情节并精简了重复铺陈，正式内容待后端生成〕`
}

function mockConfirmMessage(sessionId: string, body: { idempotency_key?: string }, ctx: ConversationContextInput): ConversationConfirmResponse {
  const cacheKey = `${sessionId}:${body.idempotency_key ?? 'none'}`
  if (body.idempotency_key) {
    const cached = mockIdempotencyCache.get(cacheKey)
    if (cached) return cached as ConversationConfirmResponse
  }
  const executionId = `mock-exec-${++mockExecSeq}`
  const response: ConversationConfirmResponse = {
    execution_id: executionId,
    status: 'completed',
    candidate: {
      id: `mock-candidate-${executionId}`,
      execution_id: executionId,
      content: mockRewrite(ctx.draft ?? ''),
      base_version_id: ctx.currentVersionId ?? null,
      status: 'candidate',
      created_at: new Date().toISOString(),
    },
    change_set: {
      id: `mock-change-set-${executionId}`,
      session_id: sessionId,
      execution_id: executionId,
      operation_type: 'chapter_rewrite',
      intent: 'rewrite',
      status: 'pending',
      objects: [],
      base_version_id: ctx.currentVersionId ?? null,
      created_at: new Date().toISOString(),
    },
  }
  if (body.idempotency_key) mockIdempotencyCache.set(cacheKey, response)
  return response
}

function mockClarifyMessage(_messageId: string, body: { content?: string }, ctx: ConversationContextInput): ConversationSendMessageResponse {
  const content = body.content?.trim() || '保留结尾伏笔与人物关系，压缩前半段。'
  return {
    message_id: `mock-msg-${++mockMsgSeq}`,
    intent: 'rewrite',
    requires_confirmation: true,
    task_card: mockTaskCard(ctx, content, false),
    clarification: null,
    content: '已按你的澄清重新整理任务卡（mock）。确认后开始生成候选稿。',
  }
}

// ─── 对外 API ───

export const chatApi = {
  createSession: (projectId: string) => withMockFallback(
    () => chatRequest<ChatSessionPayload>('/chat/sessions', { method: 'POST', body: JSON.stringify({ project_id: projectId }) }).then(normalizeSession),
    () => mockEnsureSession(projectId),
  ),

  getSession: (sessionId: string) => withMockFallback(
    () => chatRequest<ChatSessionPayload>(`/chat/sessions/${sessionId}`).then(normalizeSession),
    () => {
      const session = mockState.sessions.find(s => s.id === sessionId)
      if (!session) throw new ChatApiError(404, '会话不存在', null, null)
      return session
    },
  ),

  refreshSummary: (sessionId: string) => withMockFallback(
    () => chatRequest<ConversationSession>(`/chat/sessions/${sessionId}/summary/refresh`, { method: 'POST' }),
    () => {
      const session = mockState.sessions.find(s => s.id === sessionId)
      if (!session) throw new ChatApiError(404, '会话不存在', null, null)
      return { ...session, summary: '（mock 摘要）本会话已确认若干创作约束，正文未发生直接修改。' }
    },
  ),

  sendMessage: (sessionId: string, body: { content: string; idempotency_key?: string }, ctx: ConversationContextInput) => withMockFallback(
    () => chatRequest<ConversationSendMessageResponse>(`/chat/sessions/${sessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({
        content: body.content,
        session_id: sessionId,
        idempotency_key: body.idempotency_key,
        context: {
          chapter_no: ctx.chapterNo ?? null,
          chapter_title: ctx.chapterTitle ?? null,
          current_version_id: ctx.currentVersionId ?? null,
          draft: ctx.draft ?? null,
        },
      }),
    }),
    () => mockSendMessage(sessionId, body.content, body.idempotency_key, ctx),
  ),

  confirmMessage: (
    sessionId: string,
    messageId: string,
    body: { confirmed: boolean; overrides: Record<string, unknown>; base_version_id?: string | null; idempotency_key?: string },
    ctx: ConversationContextInput,
  ) => withMockFallback(
    () => chatRequest<{
      execution_id: string
      status: ConversationConfirmResponse['status']
      candidate?: ChatCandidatePayload | null
      change_set?: ConversationChangeSet | null
    }>(`/chat/sessions/${sessionId}/messages/${messageId}/confirm`, {
      method: 'POST',
      body: JSON.stringify({
        confirmed: body.confirmed,
        overrides: body.overrides,
        base_version_id: body.base_version_id ?? null,
        idempotency_key: body.idempotency_key,
      }),
    }).then(normalizeConfirmResponse),
    () => mockConfirmMessage(sessionId, { idempotency_key: body.idempotency_key }, ctx),
  ),

  clarifyMessage: (messageId: string, body: { content?: string; answers?: Record<string, string> }, ctx: ConversationContextInput) => withMockFallback(
    () => chatRequest<ConversationSendMessageResponse>(`/chat/messages/${messageId}/clarify`, {
      method: 'POST',
      body: JSON.stringify({ answers: body.answers ?? {}, content: body.content ?? '' }),
    }),
    () => mockClarifyMessage(messageId, { content: body.content }, ctx),
  ),

  acceptCandidate: (executionId: string) => withMockFallback(
    () => chatRequest<ConversationAcceptResponse>(`/chat/executions/${executionId}/candidate/accept`, { method: 'POST' }),
    () => Promise.resolve({ change_set_id: `mock-change-set-accept-${Date.now()}`, new_version_id: `mock-version-${Date.now()}` }),
  ),

  discardCandidate: (executionId: string) => withMockFallback(
    () => chatRequest<{ ok: boolean }>(`/chat/executions/${executionId}/candidate/discard`, { method: 'POST' }),
    () => Promise.resolve({ ok: true }),
  ),

  reviseCandidate: (executionId: string, reviseInstruction: string, prevContent: string, baseVersionId: string | null) => withMockFallback(
    () => chatRequest<ConversationCandidate>(`/chat/executions/${executionId}/candidate/revise`, {
      method: 'POST',
      body: JSON.stringify({ revise_instruction: reviseInstruction }),
    }),
    () => Promise.resolve({
      id: `mock-candidate-${Date.now()}`,
      execution_id: executionId,
      content: `${prevContent || '〔mock 候选稿〕'}\n\n〔mock：按“${reviseInstruction}”继续调整的候选稿〕`,
      base_version_id: baseVersionId,
      status: 'candidate',
      created_at: new Date().toISOString(),
    }),
  ),

  revertChangeSet: (changeSetId: string) => withMockFallback(
    () => chatRequest<{ ok: boolean }>(`/chat/change-sets/${changeSetId}/revert`, { method: 'POST' }),
    () => Promise.resolve({ ok: true }),
  ),

  revertSession: (sessionId: string) => withMockFallback(
    () => chatRequest<{ ok: boolean }>(`/chat/change-sets/revert-session?session_id=${encodeURIComponent(sessionId)}`, { method: 'POST' }),
    () => Promise.resolve({ ok: true }),
  ),

  finalizeExecution: (executionId: string) => withMockFallback(
    () => chatRequest<{ ok: boolean }>(`/chat/executions/${executionId}/finalize`, { method: 'POST' }),
    () => Promise.resolve({ ok: true }),
  ),
}
