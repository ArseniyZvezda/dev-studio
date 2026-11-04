const { contextBridge, ipcRenderer } = require('electron');
// Deliberately no general IPC or Node access in the renderer (dev spec §2.7).
contextBridge.exposeInMainWorld('voxariumWindow', {
  getState: () => ipcRenderer.invoke('studio:window', 'state'),
  minimize: () => ipcRenderer.invoke('studio:window', 'minimize'),
  minimizeToTray: () => ipcRenderer.invoke('studio:window', 'minimize-to-tray'),
  setLocale: locale => ipcRenderer.invoke('studio:window', 'set-locale', locale),
  setProducerAttention: waiting => ipcRenderer.invoke('studio:window', 'producer-attention', waiting),
  toggleMaximize: () => ipcRenderer.invoke('studio:window', 'toggle-maximize'),
  close: () => ipcRenderer.invoke('studio:window', 'close'),
  onStateChange: callback => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('studio:window-state', listener);
    return () => ipcRenderer.removeListener('studio:window-state', listener);
  },
});

contextBridge.exposeInMainWorld('voxariumUpdates', {
  getState: () => ipcRenderer.invoke('studio:update', 'state'),
  check: () => ipcRenderer.invoke('studio:update', 'check'),
  download: () => ipcRenderer.invoke('studio:update', 'download'),
  install: () => ipcRenderer.invoke('studio:update', 'install'),
  onStateChange: callback => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('studio:update-state', listener);
    return () => ipcRenderer.removeListener('studio:update-state', listener);
  },
});

contextBridge.exposeInMainWorld('voxariumStartup', {
  getState: () => ipcRenderer.invoke('studio:startup'),
  setEnabled: enabled => ipcRenderer.invoke('studio:startup', enabled),
});
