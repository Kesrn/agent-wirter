<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, ApiError } from '../api/client'

const router = useRouter()
const username = ref('')
const password = ref('')
const email = ref('')
const registerError = ref('')

async function handleRegister() {
  registerError.value = ''
  if (!username.value.trim() || username.value.trim().length < 3) {
    registerError.value = '用户名至少3个字符'
    return
  }
  if (!password.value || password.value.length < 6) {
    registerError.value = '密码至少6个字符'
    return
  }
  try {
    await api.register({
      username: username.value.trim(),
      password: password.value,
      email: email.value.trim() || undefined,
    })
    // 注册成功后自动登录
    const res = await api.login({ username: username.value.trim(), password: password.value })
    localStorage.setItem('ai_write_logged_in', 'true')
    localStorage.setItem('ai_write_token', res.access_token)
    localStorage.setItem('ai_write_username', res.user.username)
    router.push('/projects')
  } catch (e: unknown) {
    if (e instanceof ApiError) {
      if (e.status === 409 || e.status === 400) {
        registerError.value = e.message || '该用户名已被注册'
      } else if (e.status === 422) {
        registerError.value = '请输入账号和密码'
      } else {
        registerError.value = e.message
      }
    } else {
      registerError.value = '无法连接服务器，请确认后端已启动'
    }
  }
}
</script>

<template>
  <div class="register-page">
    <div class="intro-side">
      <div class="intro-content">
        <h1 class="brand-name">AI 小说创作平台</h1>
        <p class="brand-tagline">注册新账号，开始创作之旅</p>
      </div>
    </div>

    <div class="form-side">
      <div class="register-card">
        <h2>注册</h2>
        <form @submit.prevent="handleRegister" class="register-form">
          <div class="form-group">
            <label>用户名 <span class="required">*</span></label>
            <input v-model="username" type="text" placeholder="至少3个字符" />
          </div>
          <div class="form-group">
            <label>密码 <span class="required">*</span></label>
            <input v-model="password" type="password" placeholder="至少6个字符" />
          </div>
          <div class="form-group">
            <label>邮箱（可选）</label>
            <input v-model="email" type="email" placeholder="example@mail.com" />
          </div>
          <div v-if="registerError" class="register-error">{{ registerError }}</div>
          <button type="submit" class="btn-register">注册</button>
        </form>
        <p class="switch-hint">
          已有账号？<router-link to="/login" class="switch-link">返回登录</router-link>
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.register-page {
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
  margin: 0;
}
.form-side {
  flex: 0 0 420px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255,255,255,0.06);
  padding: var(--sp-8);
}
.register-card {
  background: #fff;
  border-radius: var(--radius-lg);
  padding: var(--sp-8);
  width: 100%;
  max-width: 340px;
  box-shadow: var(--shadow-lg);
}
.register-card h2 {
  font-size: var(--text-xl);
  font-weight: 700;
  margin: 0 0 var(--sp-6);
  color: var(--text);
}
.register-form {
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
.required { color: var(--status-reviewing); }
.register-error {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  text-align: center;
}
.btn-register {
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
.btn-register:hover { opacity: 0.9; }
.switch-hint {
  text-align: center;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  margin: var(--sp-4) 0 0;
}
.switch-link {
  color: var(--accent);
}

@media (max-width: 768px) {
  .register-page {
    flex-direction: column;
    background: linear-gradient(180deg, #1e3a5f 0%, #4c1d95 100%);
  }
  .intro-side {
    padding: var(--sp-6) var(--sp-4);
    flex: 0 0 auto;
  }
  .brand-name { font-size: 1.5rem; }
  .form-side {
    flex: 1;
    background: transparent;
    padding: var(--sp-4);
  }
  .register-card {
    max-width: 100%;
    background: rgba(255,255,255,0.95);
  }
}
</style>