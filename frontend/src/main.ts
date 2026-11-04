import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import { initTheme } from './composables/useTheme'
import { initAuthSession } from './utils/authSession'
import './style.css'

initTheme()
initAuthSession()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
