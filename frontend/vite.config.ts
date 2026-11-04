import { defineConfig, loadEnv, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { buildCsp } from './src/utils/csp'

function productionCspPlugin(cspContent: string): Plugin {
  return {
    name: 'vite-plugin-production-csp',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: {
            'http-equiv': 'Content-Security-Policy',
            content: cspContent,
          },
          injectTo: 'head-prepend',
        },
      ]
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backendProxyTarget = env.VITE_BACKEND_PROXY_TARGET || 'http://localhost:8000'
  const isDesktop = env.VITE_DESKTOP === 'true'

  let apiOrigin = ''
  const apiBaseUrl = env.VITE_API_BASE_URL || ''
  try {
    if (apiBaseUrl.startsWith('http://') || apiBaseUrl.startsWith('https://')) {
      apiOrigin = new URL(apiBaseUrl).origin
    }
  } catch {
    apiOrigin = ''
  }

  const cspContent = buildCsp({
    desktop: isDesktop,
    apiOrigin,
  })

  return {
    base: isDesktop ? './' : '/',
    plugins: [vue(), productionCspPlugin(cspContent)],
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
