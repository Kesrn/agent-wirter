// @vitest-environment happy-dom
/**
 * ConversationCreativeWorkspace 真实挂载测试（P0 TDZ 回归防护）。

 * 源码字符串测试与生产构建都无法发现 setup 阶段的 TDZ 错误——
 * immediate watcher 在 setup 立即执行时访问尚未声明的 const 会抛
 * ReferenceError，组件直接无法渲染。真实挂载才能捕获这类运行时错误。
 */
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import ConversationCreativeWorkspace from './ConversationCreativeWorkspace.vue'

// 阻断所有网络请求（挂载不应触发真实 API 调用）
vi.mock('../api/http', () => ({ default: vi.fn() }))

describe('ConversationCreativeWorkspace：真实挂载', () => {
  it('immediate watcher 不触发 TDZ——工作区可正常渲染', async () => {
    setActivePinia(createPinia())

    let wrapper
    expect(() => {
      wrapper = mount(ConversationCreativeWorkspace, {
        props: { projectId: 'project-1', mode: 'novel' },
        global: {
          stubs: {
            ConversationAssistant: true,
            WritingEditor: true,
            CreativeContextPanel: true,
            AgentPanel: true,
            ProjectLibraryPanel: true,
          },
        },
      })
    }).not.toThrow()

    await nextTick()
    expect(wrapper!.exists()).toBe(true)
    // 核心布局渲染：左章节栏 / 中创作区 / 右上下文面板
    expect(wrapper!.find('.ccw-layout').exists()).toBe(true)
    expect(wrapper!.find('.ccw-rail').exists()).toBe(true)
    wrapper!.unmount()
  })

  it('切换章节后选区上下文被重置（selection/currentParagraph 为 null）', async () => {
    setActivePinia(createPinia())
    const wrapper = mount(ConversationCreativeWorkspace, {
      props: { projectId: 'project-1', mode: 'novel' },
      global: {
        stubs: {
          ConversationAssistant: true,
          WritingEditor: true,
          CreativeContextPanel: true,
          AgentPanel: true,
          ProjectLibraryPanel: true,
        },
      },
    })
    await nextTick()

    // 变更 projectId 模拟切换项目/章节 → watcher 触发重置
    await wrapper.setProps({ projectId: 'project-2' })
    await nextTick()

    // 对话上下文（通过 stub 的 ConversationAssistant 接收的 props 验证）
    const assistant = wrapper.findComponent({ name: 'ConversationAssistant' })
    const ctx = assistant.props('context') as {
      selection?: string | null
      currentParagraph?: string | null
    }
    expect(ctx.selection).toBeNull()
    expect(ctx.currentParagraph).toBeNull()
    wrapper.unmount()
  })
})
