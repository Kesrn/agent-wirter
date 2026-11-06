import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  chatApi,
  ChatApiError,
  type ConversationCandidate,
  type ConversationChangeSet,
  type ConversationClarification,
  type ConversationContextInput,
  type ConversationIntent,
  type ConversationMessageRecord,
  type ConversationPlan,
  type ConversationSendMessageResponse,
  type ConversationSession,
  type ConversationTaskCard,
} from '../api/chat'

/**
 * 对话式创作会话状态。独立于 expertStore / chapterStore：
 * 这里只保存会话、消息、意图、任务卡、候选稿与执行账本，
 * 不写入正文，不复制章节生成状态机。
 */
export type ConversationAwaiting = 'idle' | 'clarification' | 'task_card_confirm' | 'candidate_review' | 'running'

function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `key-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function errMessage(e: unknown, fallback: string): string {
  if (e instanceof Error && e.message) return e.message
  return fallback
}

function assistantReply(res: ConversationSendMessageResponse): string {
  // v2.4：识别但拒绝执行（复合任务/定稿）→ 展示具体原因，不得用默认文案
  if (res.intent_unsupported) return res.unsupported_reason ?? '该指令暂不支持自动执行。'
  if (res.intent === 'knowledge_query') return '已完成知识查询。'
  if (res.intent === 'rewrite') return '已为你整理任务卡，请确认。'
  if (res.intent === 'flow_control') return '已识别流程控制意图。'
  if (res.intent === 'data_operation') return '该操作类型暂不支持。'
  return '收到，请继续。'
}

export const useConversationStore = defineStore('conversation', () => {
  const sessions = ref<ConversationSession[]>([])
  const sessionId = ref<string | null>(null)
  const summary = ref<string | null>(null)
  const messages = ref<ConversationMessageRecord[]>([])
  const intent = ref<ConversationIntent | null>(null)
  const plan = ref<ConversationPlan | null>(null)
  const taskCard = ref<ConversationTaskCard | null>(null)
  const clarification = ref<ConversationClarification | null>(null)
  const requiresConfirmation = ref(false)
  const awaiting = ref<ConversationAwaiting>('idle')
  const candidate = ref<ConversationCandidate | null>(null)
  const executionId = ref<string | null>(null)
  const changeSet = ref<ConversationChangeSet | null>(null)
  const baseVersionId = ref<string | null>(null)
  const baseDraft = ref<string | null>(null)
  const idempotencyKey = ref<string | null>(null)
  const versionConflict = ref<{ latestVersionId: string } | null>(null)
  const sending = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastMessageId = ref<string | null>(null)

  function clearRoundState(): void {
    stopPolling()
    intent.value = null
    plan.value = null
    taskCard.value = null
    clarification.value = null
    requiresConfirmation.value = false
    awaiting.value = 'idle'
    candidate.value = null
    executionId.value = null
    changeSet.value = null
    versionConflict.value = null
    error.value = null
    lastMessageId.value = null
  }

  /** 创建/复用项目会话，返回 session_id */
  async function ensureSession(projectId: string): Promise<string> {
    if (sessionId.value) return sessionId.value
    loading.value = true
    error.value = ''
    try {
      const session = await chatApi.createSession(projectId)
      sessions.value = [session, ...sessions.value.filter(s => s.id !== session.id)]
      sessionId.value = session.id
      summary.value = session.summary
      return session.id
    } catch (e: unknown) {
      error.value = errMessage(e, '创建会话失败')
      throw e
    } finally {
      loading.value = false
    }
  }

  /**
   * 发送用户消息 → 后端返回意图 / 计划 / 任务卡 / 澄清问题。
   * 只展示，不直接触发生成。
   */
  async function sendMessage(projectId: string, content: string, context: ConversationContextInput): Promise<void> {
    stopPolling()  // 新任务开始：旧轮询失效，防旧结果覆盖新状态
    const text = content.trim()
    if (!text || sending.value) return
    sending.value = true
    error.value = ''
    // 幂等号只属于本次消息；不能复用上一条消息的 key，否则后端会把
    // 新指令当成旧响应重放（或命中旧消息的候选稿）。确认任务卡时仍使用
    // 当前这一轮保存的 key。
    const key = createIdempotencyKey()
    try {
      const sid = await ensureSession(projectId)
      idempotencyKey.value = key
      messages.value.push({
        id: `local-user-${messages.value.length}-${Date.now()}`,
        session_id: sid,
        role: 'user',
        content: text,
        idempotency_key: key,
        created_at: new Date().toISOString(),
      })
      const res = await chatApi.sendMessage(sid, { content: text, idempotency_key: key }, context)
      lastMessageId.value = res.message_id
      intent.value = res.intent
      plan.value = res.plan ?? null
      taskCard.value = res.task_card ?? null
      clarification.value = res.clarification ?? null
      requiresConfirmation.value = res.requires_confirmation
      if (res.task_card?.base_version_id) baseVersionId.value = res.task_card.base_version_id
      awaiting.value = res.intent_unsupported
        ? 'idle' // v2.4：识别但拒绝执行 → 不得静默回空闲，以 unsupported_reason 提示
        : res.clarification?.needs_clarification
          ? 'clarification'
          : res.task_card && (res.requires_confirmation || res.task_card.confirmation_required)
            ? 'task_card_confirm'
            : 'idle'
      if (res.intent_unsupported) {
        error.value = res.unsupported_reason ?? '该指令暂不支持自动执行。'
      }
      messages.value.push({
        id: res.message_id,
        session_id: sid,
        role: 'assistant',
        content: res.content ?? res.reply ?? assistantReply(res),
        intent: res.intent,
        plan: res.plan ?? null,
        task_card: res.task_card ?? null,
        created_at: new Date().toISOString(),
      })
    } catch (e: unknown) {
      error.value = errMessage(e, '发送消息失败')
      throw e
    } finally {
      sending.value = false
    }
  }

  /** 澄清后重新识别（§12.2 POST /chat/messages/{message_id}/clarify） */
  async function submitClarification(projectId: string, content: string, answers: Record<string, string>, context: ConversationContextInput): Promise<void> {
    if (!lastMessageId.value) {
      error.value = '缺少待澄清的消息上下文'
      throw new Error(error.value)
    }
    sending.value = true
    error.value = ''
    try {
      const sid = await ensureSession(projectId)
      const note = content.trim() || Object.values(answers).filter(Boolean).join('；')
      messages.value.push({
        id: `local-user-${Date.now()}`,
        session_id: sid,
        role: 'user',
        content: note,
        created_at: new Date().toISOString(),
      })
      // 澄清补充 → 作为新消息重发（后端意图识别逐条无状态，无独立 clarify 端点）
      const res = await chatApi.sendMessage(sid, {
        content: note,
        idempotency_key: createIdempotencyKey(),
      }, context)
      lastMessageId.value = res.message_id
      intent.value = res.intent
      plan.value = res.plan ?? null
      taskCard.value = res.task_card ?? null
      clarification.value = res.clarification ?? null
      requiresConfirmation.value = res.requires_confirmation
      if (res.task_card?.base_version_id) baseVersionId.value = res.task_card.base_version_id
      awaiting.value = res.intent_unsupported
        ? 'idle' // v2.4：识别但拒绝执行 → 不得静默回空闲，以 unsupported_reason 提示
        : res.clarification?.needs_clarification
          ? 'clarification'
          : res.task_card && (res.requires_confirmation || res.task_card.confirmation_required)
            ? 'task_card_confirm'
            : 'idle'
      if (res.intent_unsupported) {
        error.value = res.unsupported_reason ?? '该指令暂不支持自动执行。'
      }
      messages.value.push({
        id: res.message_id,
        session_id: sid,
        role: 'assistant',
        content: res.content ?? res.reply ?? assistantReply(res),
        intent: res.intent,
        plan: res.plan ?? null,
        task_card: res.task_card ?? null,
        created_at: new Date().toISOString(),
      })
    } catch (e: unknown) {
      error.value = errMessage(e, '提交澄清失败')
      throw e
    } finally {
      sending.value = false
    }
  }

  /**
   * 确认任务卡 → POST confirm。
   * 若后端直接返回候选稿（completed），进入 candidate_review；
   * 若返回 run（running），由 AgentPanel 桥接现有 SSE 生成流程，
   * 生成完成后通过 syncCandidateFromGeneration 回填候选稿。
   */
  async function confirmTaskCard(_projectId: string, params: {
    messageId: string
    taskCard: ConversationTaskCard
    overrides?: Record<string, unknown>
    baseVersionId?: string | null
    idempotencyKey?: string
    context: ConversationContextInput
  }): Promise<void> {
    if (!sessionId.value) throw new Error('会话未就绪')
    error.value = ''
    sending.value = true
    try {
      const sid = sessionId.value
      baseVersionId.value = params.baseVersionId ?? params.taskCard.base_version_id ?? baseVersionId.value
      baseDraft.value = params.context.draft ?? null
      const res = await chatApi.confirmMessage(sid, params.messageId, {
        confirmed: true,
        overrides: params.overrides ?? {},
        base_version_id: baseVersionId.value,
        idempotency_key: params.idempotencyKey ?? idempotencyKey.value ?? undefined,
      }, params.context)
      executionId.value = res.execution_id
      changeSet.value = res.change_set ?? null
      if (res.candidate) {
        candidate.value = res.candidate
        awaiting.value = 'candidate_review'
      } else if (res.status === 'failed') {
        // 修复 A：生成失败不得静默回 idle —— 明确展示失败原因并保留重试入口
        candidate.value = null
        error.value = res.error_message ?? '候选稿生成失败，请检查模型配置后重试。'
        awaiting.value = 'task_card_confirm'
      } else if (res.status === 'running') {
        // 修复 2：并发/超时返回 running → 保留执行状态，显示"正在生成"，
        // 并继续用同一个幂等号轮询（不得进入 idle 让任务卡消失）
        candidate.value = null
        error.value = ''
        awaiting.value = 'running'
        void pollRunning(sid, params.messageId, res.execution_id, {
          baseVersionId: baseVersionId.value,
          idempotencyKey: params.idempotencyKey ?? idempotencyKey.value ?? undefined,
          context: params.context,
        })
      } else {
        awaiting.value = 'idle'
      }
    } catch (e: unknown) {
      error.value = errMessage(e, '确认任务卡失败')
      throw e
    } finally {
      sending.value = false
    }
  }

  /** running 轮询：并发/超时返回 running 后用同一幂等号继续查询，直到出结果或超限 */
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let pollToken = 0
  function stopPolling(): void {
    // 生命周期失效：token 递增使所有在途轮询回写前被拦截（防旧任务覆盖新状态）
    pollToken += 1
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
  }
  async function pollRunning(
    sid: string,
    messageId: string,
    execId: string | null,
    opts: { baseVersionId?: string | null; idempotencyKey?: string; context?: ConversationContextInput },
    attempt = 0,
    token?: number,
  ): Promise<void> {
    // 后端 _wait_for_placeholder 每轮最多等 15s（SQLite 锁等待 + 轮询），
    // 前端 2s 间隔 × 60 次覆盖约 120s+，注释不做精确承诺；到限必须落定。
    // token 在首次调用捕获并跨递归传递：stopPolling() 递增 pollToken 后，
    // 所有在途轮询（含已排队的 setTimeout）下一次执行即被拦截。
    const MAX_ATTEMPTS = 60
    const currentToken = token ?? pollToken
    if (currentToken !== pollToken || awaiting.value !== 'running') return
    if (attempt >= MAX_ATTEMPTS) {
      error.value = '生成超时（后端处理超过 2 分钟），请重新发起。'
      awaiting.value = 'task_card_confirm'
      return
    }
    try {
      const res = await chatApi.confirmMessage(sid, messageId, {
        confirmed: true,
        overrides: {},
        base_version_id: opts.baseVersionId,
        idempotency_key: opts.idempotencyKey,
      }, opts.context ?? {})
      if (currentToken !== pollToken) return  // 生命周期失效：不回写
      if (res.candidate) {
        candidate.value = res.candidate
        awaiting.value = 'candidate_review'
        return
      }
      if (res.status === 'failed') {
        error.value = res.error_message ?? '候选稿生成失败，请检查模型配置后重试。'
        awaiting.value = 'task_card_confirm'
        return
      }
      if (res.status === 'running' || res.status === 'pending') {
        pollTimer = setTimeout(() => {
          void pollRunning(sid, messageId, execId, opts, attempt + 1, currentToken)
        }, 2000)
        return
      }
      awaiting.value = 'idle'
    } catch (e: unknown) {
      if (currentToken !== pollToken) return
      error.value = errMessage(e, '查询生成状态失败')
      awaiting.value = 'task_card_confirm'
    }
  }

  /** 桥接现有 SSE 生成完成后回填候选稿内容 */
  function syncCandidateFromGeneration(content: string, execId: string | null, baseVersion: string | null): void {
    if (!content) return
    candidate.value = {
      id: `generated-${Date.now()}`,
      execution_id: execId ?? 'execution-bridged',
      content,
      base_version_id: baseVersion ?? baseVersionId.value,
      status: 'candidate',
      created_at: new Date().toISOString(),
    }
    awaiting.value = 'candidate_review'
  }

  /** 采用候选稿（服务端校验 base_version_id；冲突时记录 versionConflict） */
  async function acceptCandidate(execution: string): Promise<{ changeSetId: string; newVersionId: string }> {
    error.value = ''
    try {
      const res = await chatApi.acceptCandidate(execution)
      if (candidate.value) candidate.value.status = 'applied'
      if (changeSet.value) changeSet.value.status = 'applied'
      versionConflict.value = null
      awaiting.value = 'idle'
      return { changeSetId: res.change_set_id, newVersionId: res.new_version_id }
    } catch (e: unknown) {
      if (e instanceof ChatApiError && e.code === 'version_conflict') {
        const latest = (e.details?.latest_version_id as string | undefined) ?? ''
        versionConflict.value = { latestVersionId: latest }
      }
      error.value = errMessage(e, '采用候选稿失败')
      throw e
    }
  }

  async function discardCandidate(execution: string): Promise<void> {
    error.value = ''
    try {
      await chatApi.discardCandidate(execution)
      if (candidate.value) candidate.value.status = 'discarded'
      candidate.value = null
      changeSet.value = null
      awaiting.value = 'idle'
    } catch (e: unknown) {
      error.value = errMessage(e, '丢弃候选稿失败')
      throw e
    }
  }

  async function reviseCandidate(instruction: string): Promise<void> {
    if (!executionId.value) {
      error.value = '缺少执行上下文，无法继续调整'
      throw new Error(error.value)
    }
    error.value = ''
    try {
      const revised = await chatApi.reviseCandidate(
        executionId.value,
        instruction,
        candidate.value?.content ?? '',
        candidate.value?.base_version_id ?? null,
      )
      candidate.value = revised
      awaiting.value = 'candidate_review'
    } catch (e: unknown) {
      error.value = errMessage(e, '继续调整失败')
      throw e
    }
  }

  /** 撤销本轮会话所有操作（§12.5 revert-session） */
  async function revertSession(_projectId: string): Promise<void> {
    if (!sessionId.value) return
    error.value = ''
    try {
      await chatApi.revertSession(sessionId.value)
      clearRoundState()
    } catch (e: unknown) {
      error.value = errMessage(e, '撤销本轮失败')
      throw e
    }
  }

  function reset(): void {
    sessions.value = []
    sessionId.value = null
    summary.value = null
    messages.value = []
    clearRoundState()
    baseVersionId.value = null
    baseDraft.value = null
    idempotencyKey.value = null
    sending.value = false
    loading.value = false
  }

  return {
    sessions, sessionId, summary, messages,
    intent, plan, taskCard, clarification, requiresConfirmation, awaiting,
    candidate, executionId, changeSet, baseVersionId, baseDraft, idempotencyKey, versionConflict,
    sending, loading, error, lastMessageId,
    ensureSession, sendMessage, submitClarification, confirmTaskCard, syncCandidateFromGeneration, stopPolling,
    acceptCandidate, discardCandidate, reviseCandidate, revertSession, reset,
  }
})
