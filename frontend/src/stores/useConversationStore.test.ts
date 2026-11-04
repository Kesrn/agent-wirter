import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { chatApi, ChatApiError } from '../api/chat'
import type { ConversationSendMessageResponse, ConversationSession } from '../api/chat'
import { useConversationStore } from './useConversationStore'

function session(id = 'session-1'): ConversationSession {
  return {
    id,
    project_id: 'project-1',
    title: '对话创作',
    summary: null,
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
  }
}

function rewriteResponse(messageId = 'msg-1'): ConversationSendMessageResponse {
  return {
    message_id: messageId,
    intent: 'rewrite',
    requires_confirmation: true,
    task_card: {
      schema_version: 1,
      operation_type: 'chapter_rewrite',
      target: { chapter_no: 5 },
      base_version_id: 'version-9',
      constraints: { must_preserve: ['结尾伏笔'], must_not_change: ['人物设定'] },
      risk_level: 'L2',
      confirmation_required: true,
      idempotency_key: null,
    },
    clarification: null,
    content: '请核对任务卡。',
  }
}

const ctx = {
  chapterNo: 5,
  chapterTitle: '第五章',
  draft: '正文',
  currentVersionId: 'version-9',
}

describe('useConversationStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('sends a message, appends user/assistant messages and exposes intent + task card', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    const sendMessage = vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse())

    const store = useConversationStore()
    await store.sendMessage('project-1', '压缩本章前半段，保留结尾伏笔。', ctx)

    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(sendMessage).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({ content: '压缩本章前半段，保留结尾伏笔。', idempotency_key: expect.any(String) }),
      expect.objectContaining({ chapterNo: 5 }),
    )
    expect(store.messages).toHaveLength(2)
    expect(store.messages[0].role).toBe('user')
    expect(store.messages[1].role).toBe('assistant')
    expect(store.messages[1].intent).toBe('rewrite')
    expect(store.intent).toBe('rewrite')
    expect(store.taskCard?.operation_type).toBe('chapter_rewrite')
    expect(store.taskCard?.base_version_id).toBe('version-9')
    expect(store.baseVersionId).toBe('version-9')
    expect(store.awaiting).toBe('task_card_confirm')
    expect(store.lastMessageId).toBe('msg-1')
  })

  it('reuses the same session across messages in one project', async () => {
    const createSession = vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))

    const store = useConversationStore()
    await store.sendMessage('project-1', '第一句', ctx)
    await store.sendMessage('project-1', '第二句', ctx)

    expect(createSession).toHaveBeenCalledTimes(1)
    expect(store.sessionId).toBe('session-1')
    expect(store.messages).toHaveLength(4)
  })

  it('confirmTaskCard sets candidate and awaiting=candidate_review when backend returns a candidate', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    const confirmMessage = vi.spyOn(chatApi, 'confirmMessage').mockResolvedValue({
      execution_id: 'exec-1',
      status: 'completed',
      candidate: {
        id: 'candidate-1',
        execution_id: 'exec-1',
        content: '候选正文',
        base_version_id: 'version-9',
        status: 'candidate',
        created_at: '2026-08-02T00:00:00Z',
      },
      change_set: {
        id: 'change-set-1',
        session_id: 'session-1',
        execution_id: 'exec-1',
        operation_type: 'chapter_rewrite',
        intent: 'rewrite',
        status: 'pending',
        objects: [],
        base_version_id: 'version-9',
        created_at: '2026-08-02T00:00:00Z',
      },
    })

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', {
      messageId: 'msg-1',
      taskCard: store.taskCard!,
      context: ctx,
    })

    expect(confirmMessage).toHaveBeenCalledTimes(1)
    expect(store.candidate?.content).toBe('候选正文')
    expect(store.executionId).toBe('exec-1')
    expect(store.awaiting).toBe('candidate_review')
  })

  it('acceptCandidate surfaces version_conflict with latest_version_id', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    vi.spyOn(chatApi, 'confirmMessage').mockResolvedValue({
      execution_id: 'exec-1',
      status: 'completed',
      candidate: { id: 'candidate-1', execution_id: 'exec-1', content: '正文', base_version_id: 'version-9', status: 'candidate', created_at: '' },
      change_set: null,
    })
    const acceptCandidate = vi.spyOn(chatApi, 'acceptCandidate').mockRejectedValue(
      new ChatApiError(409, '版本冲突', 'version_conflict', { latest_version_id: 'version-10' }),
    )

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', { messageId: 'msg-1', taskCard: store.taskCard!, context: ctx })

    await expect(store.acceptCandidate('exec-1')).rejects.toBeTruthy()
    expect(acceptCandidate).toHaveBeenCalledWith('exec-1')
    expect(store.versionConflict?.latestVersionId).toBe('version-10')
  })

  it('revertSession clears round state but keeps session and message history', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    vi.spyOn(chatApi, 'confirmMessage').mockResolvedValue({
      execution_id: 'exec-1',
      status: 'completed',
      candidate: { id: 'candidate-1', execution_id: 'exec-1', content: '正文', base_version_id: 'version-9', status: 'candidate', created_at: '' },
      change_set: null,
    })
    const revertSession = vi.spyOn(chatApi, 'revertSession').mockResolvedValue({ ok: true })

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', { messageId: 'msg-1', taskCard: store.taskCard!, context: ctx })
    await store.revertSession('project-1')

    expect(revertSession).toHaveBeenCalledWith('session-1')
    expect(store.candidate).toBeNull()
    expect(store.taskCard).toBeNull()
    expect(store.awaiting).toBe('idle')
    expect(store.sessionId).toBe('session-1')
    expect(store.messages.length).toBeGreaterThan(0)
  })
})

// ── running 轮询生命周期（fake timers） ─────────────────────────────

function runningConfirmResponse(execution = 'exec-poll-1') {
  return {
    execution_id: execution,
    status: 'running' as const,
    candidate: null,
    error_message: null,
    change_set: null,
  }
}

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('useConversationStore polling lifecycle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('unmount (stopPolling) drops in-flight poll result: 旧请求结果不能回写', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    // 第一次 confirm → running 启动轮询；轮询请求挂起（in-flight）
    const poll = deferred<{ execution_id: string; status: 'running' }>()
    const confirmMessage = vi.spyOn(chatApi, 'confirmMessage')
    confirmMessage
      .mockResolvedValueOnce(runningConfirmResponse('exec-1'))
      .mockImplementationOnce(() => poll.promise)

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', { messageId: 'msg-1', taskCard: store.taskCard!, context: ctx })
    expect(store.awaiting).toBe('running')

    // 组件卸载 → 停止轮询（token 递增，在途请求作废）
    store.stopPolling()

    // 旧请求此刻才返回"成功候选"（迟到结果）→ 必须被丢弃，不得回写
    poll.resolve({ execution_id: 'exec-1', status: 'running' })
    await vi.advanceTimersByTimeAsync(10)
    await Promise.resolve()

    expect(store.candidate).toBeNull()
    expect(store.error).toBe('')  // 迟到结果未写入错误
    expect(store.awaiting).toBe('running')  // 未被旧结果推进
  })

  it('poll reaches MAX_ATTEMPTS and settles to error + task_card_confirm (超限落定)', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    // 每次轮询都返回 running（后端永远不出结果）
    vi.spyOn(chatApi, 'confirmMessage').mockResolvedValue(runningConfirmResponse('exec-1'))

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', { messageId: 'msg-1', taskCard: store.taskCard!, context: ctx })
    expect(store.awaiting).toBe('running')

    // 2s × 60 次轮询 → 超限落定：error + task_card_confirm（任务卡重试入口保留）
    for (let i = 0; i < 61; i++) {
      await vi.advanceTimersByTimeAsync(2000)
    }

    expect(store.awaiting).toBe('task_card_confirm')
    expect(store.error).toContain('超时')
    expect(store.taskCard).not.toBeNull()
  })

  it('stopPolling in clearRoundState prevents late result after new message', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue(rewriteResponse('msg-1'))
    const poll = deferred<{ execution_id: string; status: 'running' }>()
    const confirmMessage = vi.spyOn(chatApi, 'confirmMessage')
    confirmMessage
      .mockResolvedValueOnce(runningConfirmResponse('exec-1'))
      .mockImplementationOnce(() => poll.promise)

    const store = useConversationStore()
    await store.sendMessage('project-1', '重写本章', ctx)
    await store.confirmTaskCard('project-1', { messageId: 'msg-1', taskCard: store.taskCard!, context: ctx })
    expect(store.awaiting).toBe('running')

    // 新任务开始 → 旧轮询失效
    await store.sendMessage('project-1', '换一个指令', ctx)
    // 旧请求迟到返回 → 不得回写 candidate / 覆盖新状态
    poll.resolve({ execution_id: 'exec-1', status: 'running' })
    await vi.advanceTimersByTimeAsync(10)
    await Promise.resolve()

    expect(store.candidate).toBeNull()
    // 新消息已进入 task_card_confirm（旧轮询未覆盖）
    expect(store.awaiting).toBe('task_card_confirm')
    expect(store.messages[store.messages.length - 1].content).toBe('请核对任务卡。')
  })

  // ── v2.4：unsupported 分支（识别但拒绝执行，不得静默回空闲） ──

  it('intent_unsupported sets error and reply reason instead of default copy', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue({
      message_id: 'msg-unsupported',
      intent: 'flow_control',
      requires_confirmation: false,
      task_card: null,
      clarification: null,
      content: null,
      intent_unsupported: true,
      unsupported_reason: '第一版不支持定稿操作，仅支持单章局部重写。',
    })

    const store = useConversationStore()
    await store.sendMessage('project-1', '可以定稿了', ctx)

    // 不静默回空闲：error 承载具体原因
    expect(store.awaiting).toBe('idle')
    expect(store.error).toBe('第一版不支持定稿操作，仅支持单章局部重写。')
    expect(store.taskCard).toBeNull()
    // assistant 气泡展示 unsupported_reason，不得用默认流程控制文案
    expect(store.messages[store.messages.length - 1].content).toBe('第一版不支持定稿操作，仅支持单章局部重写。')
  })

  it('intent_unsupported without reason uses fallback copy', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue({
      message_id: 'msg-unsupported-2',
      intent: 'rewrite',
      requires_confirmation: false,
      task_card: null,
      clarification: null,
      content: null,
      intent_unsupported: true,
      unsupported_reason: null,
    })

    const store = useConversationStore()
    await store.sendMessage('project-1', '查穆白最后状态然后修改第十章', ctx)

    expect(store.error).toBe('该指令暂不支持自动执行。')
    // 复合任务：即使 intent=rewrite 也不得显示"已为你整理任务卡"默认文案
    expect(store.messages[store.messages.length - 1].content).toBe('该指令暂不支持自动执行。')
    expect(store.awaiting).toBe('idle')
  })

  it('task card carries v2.4 resolved_instruction fields', async () => {
    vi.spyOn(chatApi, 'createSession').mockResolvedValue(session())
    vi.spyOn(chatApi, 'sendMessage').mockResolvedValue({
      message_id: 'msg-resolved',
      intent: 'rewrite',
      requires_confirmation: true,
      task_card: {
        schema_version: 1,
        operation_type: 'chapter_rewrite',
        target: { chapter_no: 3 },
        base_version_id: 'version-9',
        constraints: { user_instruction: '润色一下第三章，重点写穆白' },
        risk_level: 'L2',
        confirmation_required: true,
        idempotency_key: null,
        resolved_instruction: '执行润色章节；重点处理：穆白；加强穆白内心戏',
        operation: 'polish_chapter',
        mentioned_entities: ['穆白'],
        requirements: ['加强穆白内心戏'],
        router_source: 'llm',
        prompt_version: 'intent-router-v2',
      },
      clarification: null,
      content: '请核对任务卡。',
    })

    const store = useConversationStore()
    await store.sendMessage('project-1', '润色一下第三章，重点写穆白', ctx)

    expect(store.taskCard?.resolved_instruction).toContain('执行润色章节')
    expect(store.taskCard?.operation).toBe('polish_chapter')
    expect(store.taskCard?.mentioned_entities).toEqual(['穆白'])
    expect(store.taskCard?.router_source).toBe('llm')
    expect(store.taskCard?.prompt_version).toBe('intent-router-v2')
    expect(store.awaiting).toBe('task_card_confirm')
  })
})
