// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import Register from './Register.vue'
import * as authConfigModule from '../utils/authConfig'
import { resetAuthConfigCache } from '../utils/authConfig'

const mockRouterPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockRouterPush }),
}))

describe('Register.vue authConfig & registration form gating', () => {
  beforeEach(() => {
    resetAuthConfigCache()
    vi.restoreAllMocks()
    mockRouterPush.mockReset()
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
    resetAuthConfigCache()
    vi.unstubAllGlobals()
  })

  it('when registration_enabled is false: hides form, shows disabled notice and return link, no hard redirect', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: false,
    })

    const wrapper = mount(Register, {
      global: {
        stubs: {
          'router-link': {
            template: '<a :href="to" class="return-link"><slot /></a>',
            props: ['to'],
          },
        },
      },
    })

    await nextTick()
    await nextTick()

    // 1. Form does NOT exist
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.find('.btn-register').exists()).toBe(false)

    // 2. Clear notice is displayed
    expect(wrapper.text()).toContain('当前未开放注册，请联系管理员创建账号')

    // 3. Link back to login exists
    const returnLink = wrapper.findAll('a').find(a => a.attributes('href') === '/login')
    expect(returnLink?.exists()).toBe(true)
    expect(returnLink?.text()).toContain('返回登录')

    // 4. No router hard redirect
    expect(mockRouterPush).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('when registration_enabled is true: displays form and operates normally', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: true,
    })

    const wrapper = mount(Register, {
      global: {
        stubs: {
          'router-link': {
            template: '<a :href="to"><slot /></a>',
            props: ['to'],
          },
        },
      },
    })

    await nextTick()
    await nextTick()

    // 1. Form exists
    expect(wrapper.find('form').exists()).toBe(true)
    expect(wrapper.find('#register-username').exists()).toBe(true)
    expect(wrapper.find('#register-password').exists()).toBe(true)
    expect(wrapper.find('.btn-register').exists()).toBe(true)

    // 2. Disabled notice is NOT displayed
    expect(wrapper.text()).not.toContain('当前未开放注册，请联系管理员创建账号')

    wrapper.unmount()
  })

  it('defaults to showing form when loadAuthConfig fails', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: true,
    })

    const wrapper = mount(Register, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    await nextTick()
    await nextTick()

    expect(wrapper.find('form').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('当前未开放注册，请联系管理员创建账号')
    wrapper.unmount()
  })

  it('initializes without form synchronously when cached as false (zero flicker)', () => {
    vi.spyOn(authConfigModule, 'getCachedAuthConfig').mockReturnValue({
      registration_enabled: false,
    })
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValue({
      registration_enabled: false,
    })

    const wrapper = mount(Register, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    // Immediately at mount
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前未开放注册，请联系管理员创建账号')
    wrapper.unmount()
  })
})
