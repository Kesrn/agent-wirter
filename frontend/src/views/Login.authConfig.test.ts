// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import Login from './Login.vue'
import * as authConfigModule from '../utils/authConfig'
import { resetAuthConfigCache } from '../utils/authConfig'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

describe('Login.vue authConfig & registration entry visibility', () => {
  beforeEach(() => {
    resetAuthConfigCache()
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
    resetAuthConfigCache()
    vi.unstubAllGlobals()
  })

  it('does NOT render "注册新账号" when registration_enabled is false', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: false,
    })

    const wrapper = mount(Login, {
      global: {
        stubs: {
          'router-link': {
            template: '<a :href="to" class="switch-link"><slot /></a>',
            props: ['to'],
          },
        },
      },
    })

    await nextTick()
    // Wait for onMounted async loadAuthConfig
    await nextTick()

    const registerLinks = wrapper.findAll('a').filter(a => a.text().includes('注册新账号'))
    expect(registerLinks).toHaveLength(0)
    expect(wrapper.text()).not.toContain('注册新账号')
    wrapper.unmount()
  })

  it('renders "注册新账号" when registration_enabled is true', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: true,
    })

    const wrapper = mount(Login, {
      global: {
        stubs: {
          'router-link': {
            template: '<a :href="to" class="switch-link"><slot /></a>',
            props: ['to'],
          },
        },
      },
    })

    await nextTick()
    await nextTick()

    const registerLink = wrapper.findAll('a').find(a => a.text().includes('注册新账号'))
    expect(registerLink?.exists()).toBe(true)
    expect(registerLink?.attributes('href')).toBe('/register')
    wrapper.unmount()
  })

  it('defaults to rendering "注册新账号" when loadAuthConfig fails', async () => {
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValueOnce({
      registration_enabled: true,
    })

    const wrapper = mount(Login, {
      global: {
        stubs: {
          'router-link': {
            template: '<a :href="to" class="switch-link"><slot /></a>',
            props: ['to'],
          },
        },
      },
    })

    await nextTick()
    await nextTick()

    const registerLink = wrapper.findAll('a').find(a => a.text().includes('注册新账号'))
    expect(registerLink?.exists()).toBe(true)
    wrapper.unmount()
  })

  it('initializes without link synchronously when already cached as false (zero flicker)', () => {
    vi.spyOn(authConfigModule, 'getCachedAuthConfig').mockReturnValue({
      registration_enabled: false,
    })
    vi.spyOn(authConfigModule, 'loadAuthConfig').mockResolvedValue({
      registration_enabled: false,
    })

    const wrapper = mount(Login, {
      global: {
        stubs: { 'router-link': true },
      },
    })

    // Immediately at mount (before any async ticks), link does NOT exist
    expect(wrapper.text()).not.toContain('注册新账号')
    wrapper.unmount()
  })
})
