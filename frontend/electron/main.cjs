const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const isDev = !app.isPackaged
app.setName('AI 创作平台')
app.setPath('userData', path.join(app.getPath('appData'), 'AI 创作平台'))
const backendHost = process.env.AI_CREATIVE_BACKEND_HOST || '127.0.0.1'
const backendPort = process.env.AI_CREATIVE_BACKEND_PORT || '8765'
const backendBaseUrl = process.env.AI_CREATIVE_BACKEND_URL || `http://${backendHost}:${backendPort}`
let mainWindow = null
let backendProcess = null

const gotSingleInstanceLock = app.requestSingleInstanceLock()

if (!gotSingleInstanceLock) {
  app.quit()
}

function buildMenu() {
  const template = [
    ...(process.platform === 'darwin'
      ? [{
          label: app.name,
          submenu: [
            { role: 'about', label: '关于 AI 创作平台' },
            { type: 'separator' },
            { role: 'services' },
            { type: 'separator' },
            { role: 'hide', label: '隐藏 AI 创作平台' },
            { role: 'hideOthers', label: '隐藏其他' },
            { role: 'unhide', label: '全部显示' },
            { type: 'separator' },
            { role: 'quit', label: '退出 AI 创作平台' },
          ],
        }]
      : []),
    {
      label: '文件',
      submenu: [
        {
          label: '检查后端连接',
          accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow?.webContents.send('desktop:refresh-backend-status'),
        },
        { type: 'separator' },
        { role: 'close', label: '关闭窗口' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新载入' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '进入全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '打开后端健康检查',
          click: () => shell.openExternal(`${backendBaseUrl}/health`),
        },
      ],
    },
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

async function checkBackendStatus() {
  const url = `${backendBaseUrl}/health`
  try {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 2400)
    const res = await fetch(url, { signal: controller.signal })
    clearTimeout(timeout)
    if (!res.ok) {
      return { ok: false, url, message: `后端响应异常 (${res.status})` }
    }
    const data = await res.json().catch(() => ({}))
    return {
      ok: true,
      url,
      service: data.service || 'AI 创作平台',
      llmProvider: data.llm_provider || '',
      embeddingProvider: data.embedding_provider || '',
      message: '后端已连接',
    }
  } catch {
    return { ok: false, url, message: '未连接到本机后端' }
  }
}

function getBackendCommand() {
  if (isDev) {
    const projectRoot = path.resolve(__dirname, '../..')
    const pythonExecutable = process.platform === 'win32'
      ? path.join(projectRoot, 'backend', 'venv', 'Scripts', 'python.exe')
      : path.join(projectRoot, 'backend', 'venv', 'bin', 'python')
    return {
      command: pythonExecutable,
      args: [path.join(projectRoot, 'backend/desktop_server.py')],
      cwd: path.join(projectRoot, 'backend'),
    }
  }

  const executableName = process.platform === 'win32' ? 'ai-creative-backend.exe' : 'ai-creative-backend'
  return {
    command: path.join(process.resourcesPath, 'backend', executableName),
    args: [],
    cwd: process.resourcesPath,
  }
}

function startBackend() {
  if (process.env.AI_CREATIVE_EXTERNAL_BACKEND === 'true') return

  const backend = getBackendCommand()
  if (!fs.existsSync(backend.command)) {
    console.error(`Backend executable not found: ${backend.command}`)
    return
  }

  backendProcess = spawn(backend.command, backend.args, {
    cwd: backend.cwd,
    env: {
      ...process.env,
      AI_CREATIVE_BACKEND_HOST: backendHost,
      AI_CREATIVE_BACKEND_PORT: backendPort,
      AI_CREATIVE_DESKTOP_DATA_DIR: path.join(app.getPath('userData'), 'backend-data'),
    },
    stdio: isDev ? 'inherit' : 'ignore',
    windowsHide: true,
  })

  backendProcess.on('exit', (code, signal) => {
    console.log(`Embedded backend exited: code=${code ?? 'null'} signal=${signal ?? 'null'}`)
    backendProcess = null
    mainWindow?.webContents.send('desktop:refresh-backend-status')
  })
}

function stopBackend() {
  if (!backendProcess) return
  backendProcess.removeAllListeners('exit')
  backendProcess.kill()
  backendProcess = null
}

async function waitForBackend(timeoutMs = 12000) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    const status = await checkBackendStatus()
    if (status.ok) return status
    await new Promise((resolve) => setTimeout(resolve, 350))
  }
  return checkBackendStatus()
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1120,
    minHeight: 720,
    title: 'AI 创作平台',
    icon: path.join(__dirname, '../build-assets/app-icon.png'),
    backgroundColor: '#0f172a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })
  mainWindow = win

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  win.webContents.on('will-navigate', (event, url) => {
    const allowedDevUrl = isDev && url.startsWith(process.env.ELECTRON_RENDERER_URL || 'http://127.0.0.1:5173')
    const allowedFileUrl = !isDev && url.startsWith('file://')
    if (allowedDevUrl || allowedFileUrl) return

    event.preventDefault()
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url)
    }
  })

  if (isDev) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL || 'http://127.0.0.1:5173')
  } else {
    win.loadFile(path.join(__dirname, '../dist-desktop/index.html'))
  }

  return win
}

ipcMain.handle('desktop:get-info', () => ({
  isDesktop: true,
  platform: process.platform,
  backendBaseUrl,
  appVersion: app.getVersion(),
}))

ipcMain.handle('desktop:check-backend', checkBackendStatus)

app.whenReady().then(() => {
  buildMenu()
  startBackend()
  waitForBackend().finally(() => {
    createMainWindow()
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
  })
})

app.on('second-instance', () => {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.focus()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  stopBackend()
})
