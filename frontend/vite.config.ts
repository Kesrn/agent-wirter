import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backendProxyTarget = env.VITE_BACKEND_PROXY_TARGET || 'http://localhost:8000'
  const isDesktop = env.VITE_DESKTOP === 'true'

  return {
    base: isDesktop ? './' : '/',
    plugins: [vue()],
    build: {
      outDir: isDesktop ? 'dist-desktop' : 'dist',
    },
    server: {
      proxy: {
        '/api': {
          target: backendProxyTarget,
          changeOrigin: true,
        },
        '/media': {
          target: backendProxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
