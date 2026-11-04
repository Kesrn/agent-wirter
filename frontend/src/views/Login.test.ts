// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import Login from './Login.vue'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
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

describe('Login.vue error handling and 429 countdown', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.restoreAllMocks()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('displays backend detail on 401 without reload or hardcoded message', async () => {
    vi.mocked(api.login).mockRejectedValueOnce(
      new ApiError(401, '用户名或密码错误'),
    )

    const wrapper = mount(Login, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    const usernameInput = wrapper.find('#login-username')
    const passwordInput = wrapper.find('#login-password')
    await usernameInput.setValue('testuser')
    await passwordInput.setValue('secret')

    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    const errorEl = wrapper.find('.login-error')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toBe('用户名或密码错误')
    wrapper.unmount()
  })

  it('displays backend detail on 403', async () => {
    vi.mocked(api.login).mockRejectedValueOnce(
      new ApiError(403, '账号已被锁定'),
    )

    const wrapper = mount(Login, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await wrapper.find('#login-username').setValue('testuser')
    await wrapper.find('#login-password').setValue('secret')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    expect(wrapper.find('.login-error').text()).toBe('账号已被锁定')
    wrapper.unmount()
  })

  it('handles 429 with Retry-After: disables button and displays countdown', async () => {
    vi.mocked(api.login).mockRejectedValueOnce(
      new ApiError(429, '操作过于频繁，请 10 秒后重试', 10),
    )

    const wrapper = mount(Login, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await wrapper.find('#login-username').setValue('testuser')
    await wrapper.find('#login-password').setValue('secret')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    expect(wrapper.find('.login-error').text()).toBe('操作过于频繁，请 10 秒后重试')
    const submitBtn = wrapper.find('.btn-login')
    expect(submitBtn.attributes('disabled')).toBeDefined()
    expect(submitBtn.text()).toContain('登录 (10s)')

    // Advance 5 seconds
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(submitBtn.text()).toContain('登录 (5s)')

    // Advance remaining 5 seconds
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(submitBtn.attributes('disabled')).toBeUndefined()
    expect(submitBtn.text()).toBe('登录')

    wrapper.unmount()
  })

  it('handles 429 without Retry-After: displays message without disabling button', async () => {
    vi.mocked(api.login).mockRejectedValueOnce(
      new ApiError(429, '操作过于频繁，请稍后再试'),
    )

    const wrapper = mount(Login, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await wrapper.find('#login-username').setValue('testuser')
    await wrapper.find('#login-password').setValue('secret')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()

    expect(wrapper.find('.login-error').text()).toBe('操作过于频繁，请稍后再试')
    const submitBtn = wrapper.find('.btn-login')
    expect(submitBtn.attributes('disabled')).toBeUndefined()
    expect(submitBtn.text()).toBe('登录')
    wrapper.unmount()
  })
})
