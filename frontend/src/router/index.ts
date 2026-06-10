import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import Login from '../views/Login.vue'
import Register from '../views/Register.vue'
import ProjectList from '../views/ProjectList.vue'
import Workspace from '../views/Workspace.vue'
import AgentStudio from '../views/AgentStudio.vue'
import Settings from '../views/Settings.vue'
import EvaluationLab from '../views/EvaluationLab.vue'
import { hasAuthSession } from '../utils/authSession'

const router = createRouter({
  history: import.meta.env.VITE_DESKTOP === 'true' ? createWebHashHistory() : createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: Login, meta: { public: true } },
    { path: '/register', name: 'register', component: Register, meta: { public: true } },
    { path: '/', redirect: () => hasAuthSession() ? '/projects' : '/login' },
    { path: '/projects', name: 'projects', component: ProjectList },
    { path: '/projects/:id', name: 'workspace', component: Workspace },
    { path: '/projects/:id/experts', name: 'experts', component: AgentStudio },
    { path: '/projects/:id/evaluations', name: 'evaluations', component: EvaluationLab },
    { path: '/settings', name: 'settings', component: Settings },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public && to.name === 'login' && hasAuthSession()) return { path: '/projects' }
  if (to.meta.public) return true
  if (!hasAuthSession()) return { path: '/login' }
  return true
})

export default router
