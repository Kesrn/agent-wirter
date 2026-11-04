// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import Register from './Register.vue'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    register: vi.fn(),
    login: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number
    retryAfterSeconds?: number
    constructor(status: number, message: string, retryAfterSeconds?: number) {
      super(message)
      this.status = status
      this.retryAfterSeconds = retryAfterSeconds
      this.name = 'ApiError'
    }
  },
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

describe('Register.vue error handling', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('displays backend detail on 403 when registration is closed', async () => {
    vi.mocked(api.register).mockRejectedValueOnce(
      new ApiError(403, '注册已关闭'),
    )

    const wrapper = mount(Register, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await wrapper.find('#register-username').setValue('newuser')
    await wrapper.find('#register-password').setValue('password123')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    const errorEl = wrapper.find('.register-error')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toBe('注册已关闭')
    wrapper.unmount()
  })

  it('displays username already taken on 409', async () => {
    vi.mocked(api.register).mockRejectedValueOnce(
      new ApiError(409, '用户名已被占用'),
    )

    const wrapper = mount(Register, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await wrapper.find('#register-username').setValue('existing')
    await wrapper.find('#register-password').setValue('password123')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    expect(wrapper.find('.register-error').text()).toBe('用户名已被占用')
    wrapper.unmount()
  })
})
