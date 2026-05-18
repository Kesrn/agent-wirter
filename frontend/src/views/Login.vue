<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, ApiError } from '../api/client'

const router = useRouter()
const username = ref('')
const password = ref('')
const loginError = ref('')

async function doLogin(user: string, pass: string) {
  loginError.value = ''
  try {
    const payload = { username: user, password: pass }
    const res = await api.login(payload)
    localStorage.setItem('ai_write_logged_in', 'true')
    localStorage.setItem('ai_write_token', res.access_token)
    localStorage.setItem('ai_write_username', res.user.username)
    router.push('/projects')
  } catch (e: unknown) {
    if (e instanceof ApiError) {
      if (e.status === 401 || e.status === 403) {
        loginError.value = '账号或密码错误'
      } else if (e.status === 422) {
        loginError.value = '请输入账号和密码'
      } else {
        loginError.value = e.message
      }
    } else {
      loginError.value = '无法连接服务器，请确认后端已启动'
    }
  }
}

async function handleLogin() {
  loginError.value = ''
  if (!username.value.trim()) {
    loginError.value = '请输入账号'
    return
  }
  if (!password.value) {
    loginError.value = '请输入密码'
    return
  }
  await doLogin(username.value.trim(), password.value)
}

async function handleDemoLogin() {
  username.value = 'admin'
  password.value = 'admin123'
  await doLogin('admin', 'admin123')
}

const features = [
  { icon: '✦', title: '多专家协作', desc: '6 位内置专家从创意到审核全流程覆盖' },
  { icon: '◆', title: '自定义创作 Agent', desc: '按需创建角色、设定触发时机和上下文范围' },
  { icon: '▸', title: '世界观与角色连贯', desc: '大纲、角色弧线、暗线贯穿全篇' },
  { icon: '↯', title: '流式生成', desc: '实时查看 AI 输出，逐段迭代' },
  { icon: '✓', title: '人工审核入库', desc: '采纳、修改后采纳或拒绝，全程可控' },
]
</script>

<template>
  <div class="login-page">
    <!-- Left: product intro -->
    <div class="intro-side">
      <div class="intro-content">
        <h1 class="brand-name">AI 小说创作平台</h1>
        <p class="brand-tagline">多专家协作 · 流式生成 · 人工审核</p>
        <div class="feature-list">
          <div v-for="f in features" :key="f.title" class="feature-item">
            <span class="feature-icon">{{ f.icon }}</span>
            <div>
              <div class="feature-title">{{ f.title }}</div>
              <div class="feature-desc">{{ f.desc }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Right: login form -->
    <div class="form-side">
      <div class="login-card">
        <h2>登录</h2>
        <form @submit.prevent="handleLogin" class="login-form">
          <div class="form-group">
            <label>账号</label>
            <input v-model="username" type="text" placeholder="输入账号" />
          </div>
          <div class="form-group">
            <label>密码</label>
            <input v-model="password" type="password" placeholder="输入密码" />
          </div>
          <div v-if="loginError" class="login-error">{{ loginError }}</div>
          <button type="submit" class="btn-login">登录</button>
          <button type="button" class="btn-demo" @click="handleDemoLogin">使用演示账号</button>
        </form>
        <p class="demo-hint">演示账号：admin / admin123</p>
        <p class="switch-hint">
          没有账号？<router-link to="/register" class="switch-link">注册新账号</router-link>
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  height: 100vh;
  background: linear-gradient(135deg, #1e3a5f 0%, #312e81 50%, #4c1d95 100%);
}
.intro-side {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-8);
}
.intro-content {
  max-width: 440px;
}
.brand-name {
  font-size: 2rem;
  font-weight: 700;
  color: #fff;
  margin: 0 0 var(--sp-2);
}
.brand-tagline {
  font-size: var(--text-base);
  color: rgba(255,255,255,0.7);
  margin: 0 0 var(--sp-8);
}
.feature-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.feature-item {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
}
.feature-icon {
  color: rgba(255,255,255,0.6);
  font-size: var(--text-lg);
  width: 24px;
  text-align: center;
  flex-shrink: 0;
}
.feature-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: #fff;
  margin-bottom: 2px;
}
.feature-desc {
  font-size: var(--text-xs);
  color: rgba(255,255,255,0.55);
  line-height: 1.4;
}

.form-side {
  flex: 0 0 420px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255,255,255,0.06);
  padding: var(--sp-8);
}
.login-card {
  background: #fff;
  border-radius: var(--radius-lg);
  padding: var(--sp-8);
  width: 100%;
  max-width: 340px;
  box-shadow: var(--shadow-lg);
}
.login-card h2 {
  font-size: var(--text-xl);
  font-weight: 700;
  margin: 0 0 var(--sp-6);
  color: var(--text);
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.form-group label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--text-secondary);
}
.btn-login {
  padding: var(--sp-3);
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  color: #fff;
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  transition: opacity var(--transition);
}
.btn-login:hover { opacity: 0.9; }
.btn-demo {
  padding: var(--sp-3);
  background: none;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: opacity var(--transition);
}
.btn-demo:hover { opacity: 0.8; }
.login-error {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  text-align: center;
}
.demo-hint {
  text-align: center;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin: var(--sp-4) 0 0;
}
.switch-hint {
  text-align: center;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin: var(--sp-2) 0 0;
}
.switch-link {
  color: var(--accent);
}

@media (max-width: 768px) {
  .login-page {
    flex-direction: column;
    background: linear-gradient(180deg, #1e3a5f 0%, #4c1d95 100%);
  }
  .intro-side {
    padding: var(--sp-6) var(--sp-4);
    flex: 0 0 auto;
  }
  .brand-name { font-size: 1.5rem; }
  .feature-list { display: none; }
  .form-side {
    flex: 1;
    background: transparent;
    padding: var(--sp-4);
  }
  .login-card {
    max-width: 100%;
    background: rgba(255,255,255,0.95);
  }
}
</style>