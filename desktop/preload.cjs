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

contextBridge.exposeInMainWorld('voxariumNotifications', Object.freeze({
  getState: () => ipcRenderer.invoke('studio:notifications', 'state'),
  setEnabled: enabled => ipcRenderer.invoke('studio:notifications', 'set-enabled', enabled),
}));

contextBridge.exposeInMainWorld('voxariumStartup', {
  getState: () => ipcRenderer.invoke('studio:startup'),
  setEnabled: enabled => ipcRenderer.invoke('studio:startup', enabled),
});

// Fixed operations only (dev spec §5.3.4). Main derives role/project/owner from
// the live native sender; these revision assertions cannot grant authority.
contextBridge.exposeInMainWorld('voxariumTabs', Object.freeze({
  bootstrap: () => ipcRenderer.invoke('studio:tabs-bootstrap'),
  openProject: request => ipcRenderer.invoke('studio:tabs-open', request),
  activateTab: request => ipcRenderer.invoke('studio:tabs-activate', request),
  beginDrag: request => ipcRenderer.invoke('studio:tabs-drag-begin', request),
  cancelDrag: request => ipcRenderer.invoke('studio:tabs-drag-cancel', request),
  setDragGeometry: request => ipcRenderer.invoke('studio:tabs-drag-geometry', request),
  detachTab: request => ipcRenderer.invoke('studio:tabs-detach', request),
  reorderTab: request => ipcRenderer.invoke('studio:tabs-reorder', request),
  getState: () => ipcRenderer.invoke('studio:tabs-state'),
  prepareClose: request => ipcRenderer.invoke('studio:tabs-prepare-close', request),
  closeTab: request => ipcRenderer.invoke('studio:tabs-close', request),
  acknowledgeClose: ack => ipcRenderer.invoke('studio:tabs-close-ack', ack),
  reportPage: request => ipcRenderer.invoke('studio:tabs-report-page', request),
  acknowledgePageRestore: ack => ipcRenderer.invoke('studio:tabs-page-restore-ack', ack),
  onRestorePage: callback => {
    if (typeof callback !== 'function') throw new TypeError('A page restore callback is required');
    const listener = (_event, request) => callback(request);
    ipcRenderer.on('studio:tabs-restore-page', listener);
    return () => ipcRenderer.removeListener('studio:tabs-restore-page', listener);
  },
  onPrepareClose: callback => {
    // Fixed close transaction phases: prepare, validate (input fence), release.
    if (typeof callback !== 'function') throw new TypeError('A close callback is required');
    const listener = (_event, request) => callback(request);
    ipcRenderer.on('studio:tabs-close-request', listener);
    return () => ipcRenderer.removeListener('studio:tabs-close-request', listener);
  },
  recentProjects: () => ipcRenderer.invoke('studio:tabs-recent-projects'),
  inspectPath: path => ipcRenderer.invoke('studio:tabs-inspect-path', path),
  browseDirectory: path => ipcRenderer.invoke('studio:tabs-browse-directory', path),
  pickerStatus: () => ipcRenderer.invoke('studio:tabs-picker-status'),
  pickDirectory: initial => ipcRenderer.invoke('studio:tabs-pick-directory', initial),
  forgetRecent: root => ipcRenderer.invoke('studio:tabs-forget-recent', root),
  setMenuOpen: request => ipcRenderer.invoke('studio:tabs-set-menu-open', request),
  onStateChange: callback => {
    if (typeof callback !== 'function') throw new TypeError('A state callback is required');
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('studio:tabs-state-changed', listener);
    return () => ipcRenderer.removeListener('studio:tabs-state-changed', listener);
  },
}));
