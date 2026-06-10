<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, ApiError } from '../api/client'
import { saveAuthSession } from '../utils/authSession'

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
    saveAuthSession({
      token: res.access_token,
      username: res.user.username,
      remember: true,
    })
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

const setupSteps = [
  { icon: '01', title: '创建账号', desc: '保存你的创作空间' },
  { icon: '02', title: '配置项目', desc: '小说与文章模式自由切换' },
  { icon: '03', title: '开始协作', desc: '让 Agent 进入你的工作流' },
]
</script>

<template>
  <div class="register-page">
    <main class="auth-shell">
      <section class="form-side" aria-labelledby="register-title">
        <div class="brand-row">
          <span class="brand-mark">AI</span>
          <span class="brand-text">小说创作平台</span>
        </div>

        <div class="register-card">
          <div class="card-heading">
            <p class="eyebrow">新创作空间</p>
            <h1 id="register-title">创建账号</h1>
          </div>

          <form @submit.prevent="handleRegister" class="register-form">
            <div class="form-group">
              <label for="register-username">用户名 <span class="required">*</span></label>
              <input id="register-username" v-model="username" type="text" autocomplete="username" placeholder="至少 3 个字符" />
            </div>
            <div class="form-group">
              <label for="register-password">密码 <span class="required">*</span></label>
              <input id="register-password" v-model="password" type="password" autocomplete="new-password" placeholder="至少 6 个字符" />
            </div>
            <div class="form-group">
              <label for="register-email">邮箱（可选）</label>
              <input id="register-email" v-model="email" type="email" autocomplete="email" placeholder="example@mail.com" />
            </div>
            <div v-if="registerError" class="register-error" role="alert">{{ registerError }}</div>
            <button type="submit" class="btn-register">注册并进入</button>
          </form>

          <div class="card-footer">
            <span>注册后自动登录</span>
            <router-link to="/login" class="switch-link">返回登录</router-link>
          </div>
        </div>
      </section>

      <section class="visual-side" aria-hidden="true">
        <div class="visual-card">
          <div class="visual-header">
            <div>
              <p class="visual-kicker">New Workspace</p>
              <h2>把草稿、角色和审校放进同一个工作台</h2>
            </div>
            <img class="hero-stack" src="../assets/hero.png" alt="" />
          </div>

          <div class="manuscript-preview">
            <div class="preview-toolbar">
              <span></span>
              <span></span>
              <span></span>
            </div>
            <div class="preview-line strong"></div>
            <div class="preview-line"></div>
            <div class="preview-line short"></div>
            <div class="preview-quote">新账号会自动进入项目列表，立即开始规划你的下一部作品。</div>
          </div>

          <div class="feature-list">
            <div v-for="step in setupSteps" :key="step.title" class="feature-item">
              <span class="feature-icon">{{ step.icon }}</span>
              <div>
                <div class="feature-title">{{ step.title }}</div>
                <div class="feature-desc">{{ step.desc }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="status-strip">
          <div>
            <span class="status-num">1</span>
            <span>Account</span>
          </div>
          <div>
            <span class="status-num">2</span>
            <span>Modes</span>
          </div>
          <div>
            <span class="status-num">Ready</span>
            <span>Workspace</span>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.register-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: clamp(16px, 4vw, 48px);
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--accent) 8%, transparent), transparent 34%),
    linear-gradient(180deg, var(--bg-panel) 0%, var(--bg) 72%);
  overflow-x: hidden;
  overflow-y: auto;
}
.auth-shell {
  width: min(1120px, 100%);
  height: min(720px, calc(100dvh - clamp(32px, 8vw, 96px)));
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(340px, 0.9fr) minmax(440px, 1.1fr);
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 76%, transparent);
  border-radius: 8px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.form-side {
  display: grid;
  align-content: center;
  min-height: 0;
  gap: clamp(var(--sp-4), 3vh, var(--sp-7, 28px));
  padding: clamp(22px, 4vw, 52px);
  background: var(--auth-card-bg);
}
.brand-row {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-3);
  color: var(--text);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--accent);
  color: var(--text-inverse);
  font-size: var(--text-sm);
  font-weight: 800;
  box-shadow: 0 14px 30px color-mix(in srgb, var(--accent) 24%, transparent);
}
.brand-text {
  font-size: var(--text-base);
  font-weight: 760;
}
.register-card {
  width: min(380px, 100%);
}
.card-heading {
  margin-bottom: var(--sp-8);
}
.eyebrow {
  margin: 0 0 var(--sp-2);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 760;
}
.card-heading h1 {
  margin: 0;
  color: var(--text);
  font-size: clamp(2rem, 4vw, 2.7rem);
  line-height: 1.08;
  font-weight: 820;
  letter-spacing: 0;
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
  font-weight: 650;
  color: var(--text-secondary);
}
.form-group input {
  min-height: 44px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-input) 92%, var(--bg));
}
.required { color: var(--status-reviewing); }
.register-error {
  font-size: var(--text-xs);
  color: var(--status-reviewing);
  text-align: center;
}
.btn-register {
  min-height: 46px;
  padding: var(--sp-3);
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  border-radius: 8px;
  font-size: var(--text-sm);
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 12px 28px color-mix(in srgb, var(--accent) 24%, transparent);
  transition: background var(--transition), transform var(--transition), box-shadow var(--transition);
}
.btn-register:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
  box-shadow: 0 16px 34px color-mix(in srgb, var(--accent) 28%, transparent);
}
.card-footer {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-top: var(--sp-5);
  font-size: var(--text-xs);
  color: var(--text-tertiary);
}
.switch-link {
  color: var(--accent);
  font-weight: 650;
  white-space: nowrap;
}
.visual-side {
  position: relative;
  display: grid;
  align-content: center;
  min-height: 0;
  gap: clamp(var(--sp-3), 2vh, var(--sp-5));
  padding: clamp(24px, 4vh, 52px);
  background:
    linear-gradient(145deg, color-mix(in srgb, var(--accent) 16%, var(--bg-panel)), var(--paper-stage));
  border-left: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
}
.visual-card {
  padding: clamp(20px, 3vh, 32px);
  border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel) 84%, transparent);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(12px);
}
.visual-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-5);
  margin-bottom: var(--sp-6);
}
.visual-kicker {
  margin: 0 0 var(--sp-2);
  color: var(--text-tertiary);
  font-size: var(--text-xs);
  font-weight: 700;
}
.visual-header h2 {
  margin: 0;
  font-size: clamp(1.35rem, 2.2vw, 1.9rem);
  line-height: 1.16;
  color: var(--text);
  letter-spacing: 0;
}
.hero-stack {
  width: 112px;
  flex: 0 0 auto;
  filter: drop-shadow(0 18px 28px color-mix(in srgb, var(--accent) 18%, transparent));
}
.manuscript-preview {
  padding: var(--sp-5);
  border: 1px solid var(--paper-border);
  border-radius: 8px;
  background:
    repeating-linear-gradient(
      to bottom,
      transparent 0,
      transparent 29px,
      var(--paper-line) 30px
    ),
    var(--paper-bg);
  box-shadow: var(--shadow);
}
.preview-toolbar {
  display: flex;
  gap: 6px;
  margin-bottom: var(--sp-5);
}
.preview-toolbar span {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 42%, var(--border));
}
.preview-line {
  width: 92%;
  height: 10px;
  margin-bottom: 13px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--text-tertiary) 26%, transparent);
}
.preview-line.strong {
  width: 52%;
  height: 14px;
  background: color-mix(in srgb, var(--accent) 38%, transparent);
}
.preview-line.short { width: 68%; }
.preview-quote {
  margin-top: var(--sp-5);
  padding-left: var(--sp-3);
  border-left: 3px solid var(--accent);
  color: var(--text-secondary);
  font-family: var(--font-serif);
  font-size: var(--text-base);
  line-height: 1.8;
}
.feature-list {
  display: grid;
  gap: var(--sp-3);
  margin-top: var(--sp-5);
}
.feature-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel) 66%, transparent);
}
.feature-icon {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 800;
}
.feature-title {
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 720;
}
.feature-desc {
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}
.status-strip {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--sp-3);
}
.status-strip > div {
  display: grid;
  gap: 2px;
  padding: var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel) 72%, transparent);
  color: var(--text-tertiary);
  font-size: var(--text-xs);
}
.status-num {
  color: var(--text);
  font-size: var(--text-lg);
  font-weight: 780;
}

@media (max-height: 820px) {
  .form-side {
    gap: var(--sp-4);
    padding-top: var(--sp-6);
    padding-bottom: var(--sp-6);
  }
  .card-heading {
    margin-bottom: var(--sp-5);
  }
  .card-heading h1 {
    font-size: clamp(1.85rem, 3.4vw, 2.35rem);
  }
  .register-form {
    gap: var(--sp-3);
  }
  .form-group input {
    min-height: 40px;
  }
  .btn-register {
    min-height: 42px;
  }
  .visual-side {
    padding-top: var(--sp-6);
    padding-bottom: var(--sp-6);
  }
  .visual-card {
    padding: var(--sp-5);
  }
  .visual-header {
    margin-bottom: var(--sp-4);
  }
  .hero-stack {
    width: 92px;
  }
  .manuscript-preview {
    padding: var(--sp-4);
  }
  .preview-toolbar {
    margin-bottom: var(--sp-3);
  }
  .preview-line {
    height: 8px;
    margin-bottom: 10px;
  }
  .preview-line.strong {
    height: 11px;
  }
  .preview-quote {
    margin-top: var(--sp-3);
    font-size: var(--text-sm);
    line-height: 1.55;
  }
  .feature-list {
    gap: var(--sp-2);
    margin-top: var(--sp-3);
  }
  .feature-item {
    padding: var(--sp-2);
  }
  .feature-icon {
    width: 30px;
    height: 30px;
  }
  .status-strip {
    display: none;
  }
}

@media (max-width: 880px) {
  .register-page {
    padding: var(--sp-3);
  }
  .auth-shell {
    height: auto;
    min-height: calc(100dvh - var(--sp-6));
    max-height: none;
    grid-template-columns: 1fr;
  }
  .form-side {
    gap: var(--sp-5);
    padding: var(--sp-5);
  }
  .register-card {
    width: 100%;
  }
  .card-heading {
    margin-bottom: var(--sp-5);
  }
  .card-heading h1 {
    font-size: clamp(1.75rem, 8vw, 2.2rem);
  }
  .register-form {
    gap: var(--sp-3);
  }
  .form-group input {
    min-height: 42px;
  }
  .btn-register {
    min-height: 44px;
  }
  .card-footer {
    margin-top: var(--sp-3);
  }
  .visual-side {
    display: none;
  }
}

@media (max-width: 480px) {
  .register-page {
    padding: var(--sp-2);
  }
  .auth-shell {
    min-height: calc(100dvh - var(--sp-4));
  }
  .form-side {
    gap: var(--sp-4);
    padding: var(--sp-4);
  }
  .brand-mark {
    width: 32px;
    height: 32px;
  }
  .card-heading {
    margin-bottom: var(--sp-4);
  }
  .card-footer {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--sp-1);
  }
}

@media (max-height: 640px) {
  .register-page {
    padding: var(--sp-2);
  }
  .auth-shell {
    height: auto;
    max-height: none;
    grid-template-columns: minmax(320px, 420px);
    justify-content: center;
  }
  .form-side {
    gap: var(--sp-3);
    padding: var(--sp-4);
  }
  .brand-row {
    display: none;
  }
  .card-heading {
    margin-bottom: var(--sp-3);
  }
  .eyebrow {
    margin-bottom: 2px;
  }
  .card-heading h1 {
    font-size: 1.65rem;
  }
  .register-form {
    gap: var(--sp-2);
  }
  .form-group input {
    min-height: 36px;
  }
  .btn-register {
    min-height: 38px;
  }
  .card-footer {
    margin-top: var(--sp-2);
  }
  .visual-side {
    display: none;
  }
}
</style>
