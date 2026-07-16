// 预加载：给前端暴露 window.electronAPI（判断是否 Electron + 注入 cookie）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  isElectron: true,
  injectCookies: (aid) => ipcRenderer.invoke('inject-cookies', aid),
  pickFolder: () => ipcRenderer.invoke('pick-folder'),
  pickFiles: (defaultPath) => ipcRenderer.invoke('pick-files', defaultPath),
  pickVideoDir: (defaultPath) => ipcRenderer.invoke('pick-video-dir', defaultPath),
  clearPartition: (aid) => ipcRenderer.invoke('clear-partition', aid),
  getPartitionCookies: (aid) => ipcRenderer.invoke('get-partition-cookies', aid),
  platformLogin: (platform, proxy) => ipcRenderer.invoke('platform-login', { platform, proxy }),
  dbg: (msg) => ipcRenderer.send('dbg', msg),
});
