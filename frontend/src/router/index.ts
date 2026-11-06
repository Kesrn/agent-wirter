import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import Login from '../views/Login.vue'
import Register from '../views/Register.vue'
import { hasAuthSession } from '../utils/authSession'
import { useUiStore } from '../stores'

const router = createRouter({
  history: import.meta.env.VITE_DESKTOP === 'true' ? createWebHashHistory() : createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: Login, meta: { public: true } },
    { path: '/register', name: 'register', component: Register, meta: { public: true } },
    { path: '/', redirect: () => hasAuthSession() ? '/projects' : '/login' },
    { path: '/projects', name: 'projects', component: () => import('../views/ProjectList.vue') },
    { path: '/projects/:id', name: 'workspace', component: () => import('../views/Workspace.vue') },
    { path: '/projects/:id/knowledge', name: 'knowledge', component: () => import('../components/KnowledgeLibrary.vue'), props: route => ({ projectId: route.params.id as string }) },
    { path: '/projects/:id/experts', name: 'experts', component: () => import('../views/AgentStudio.vue') },
    { path: '/projects/:id/evaluations', name: 'evaluations', component: () => import('../views/EvaluationLab.vue') },
    { path: '/settings', name: 'settings', component: () => import('../views/Settings.vue') },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public && to.name === 'login' && hasAuthSession()) return { path: '/projects' }
  if (to.meta.public) return true
  if (!hasAuthSession()) return { path: '/login' }
  return true
})

router.onError((error) => {
  console.error('[router error]', error)
  try {
    const ui = useUiStore()
    ui.showToast('页面加载失败，请重试', 'error')
  } catch {
    // Pinia not yet initialized or isolated test context
  }
})

export default router
