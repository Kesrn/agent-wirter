const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktopApp', {
  getInfo: () => ipcRenderer.invoke('desktop:get-info'),
  checkBackend: () => ipcRenderer.invoke('desktop:check-backend'),
  onRefreshBackendStatus: (callback) => {
    const listener = () => callback()
    ipcRenderer.on('desktop:refresh-backend-status', listener)
    return () => ipcRenderer.removeListener('desktop:refresh-backend-status', listener)
  },
  onSaveRequested: (callback) => {
    const listener = () => callback()
    ipcRenderer.on('desktop:save-requested', listener)
    return () => ipcRenderer.removeListener('desktop:save-requested', listener)
  },
})
