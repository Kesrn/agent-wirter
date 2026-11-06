import { describe, expect, it } from 'vitest'
import mainSource from './main.cjs?raw'
import preloadSource from './preload.cjs?raw'

describe('desktop Cmd/Ctrl+S save shortcut', () => {
  it('captures Cmd/Ctrl+S at the browser-process level before the renderer', () => {
    // before-input-event 早于页面 keydown：即使应用菜单或 Chromium 默认行为
    // 吞掉了快捷键，桌面端仍然能收到并转发给渲染进程。
    expect(mainSource).toContain("win.webContents.on('before-input-event'")
    expect(mainSource).toContain('if (input.isAutoRepeat) return')
    expect(mainSource).toContain('(input.meta || input.control)')
    expect(mainSource).toContain("input.key.toLowerCase() === 's'")
    expect(mainSource).toContain('event.preventDefault()')
    expect(mainSource).toContain("win.webContents.send('desktop:save-requested')")
  })

  it('exposes the save-requested IPC through the preload bridge', () => {
    expect(preloadSource).toContain('onSaveRequested: (callback) => {')
    expect(preloadSource).toContain("ipcRenderer.on('desktop:save-requested', listener)")
    expect(preloadSource).toContain("ipcRenderer.removeListener('desktop:save-requested', listener)")
  })
})
