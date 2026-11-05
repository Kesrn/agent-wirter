// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import router from './index'
import { useUiStore } from '../stores'
import * as authSession from '../utils/authSession'

describe('router lazy loading and error handling', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('resolves a lazy loaded route to its dynamic component when navigated', async () => {
    vi.spyOn(authSession, 'hasAuthSession').mockReturnValue(true)

    await router.push('/settings')
    await router.isReady()

    const matchedRoute = router.currentRoute.value
    expect(matchedRoute.name).toBe('settings')
    expect(matchedRoute.path).toBe('/settings')

    // Verify component in matched route is resolved
    const matched = matchedRoute.matched[0]
    expect(matched).toBeDefined()
    // In vue-router with () => import(...), components.default is either a loader function or the resolved component
    const comp = matched.components?.default
    expect(comp).toBeDefined()
    if (typeof comp === 'function') {
      const resolved = await (comp as () => Promise<unknown>)()
      expect(resolved).toBeDefined()
    }
  })

  it('triggers router.onError and shows toast when dynamic route import fails', async () => {
    vi.spyOn(authSession, 'hasAuthSession').mockReturnValue(true)
    const ui = useUiStore()
    const toastSpy = vi.spyOn(ui, 'showToast')

    router.addRoute({
      path: '/test-error-chunk',
      name: 'test-error-chunk',
      component: () => Promise.reject(new Error('Network error loading chunk')),
    })

    await router.push('/test-error-chunk').catch(() => {})

    expect(toastSpy).toHaveBeenCalledWith('页面加载失败，请重试', 'error')
  })
})
