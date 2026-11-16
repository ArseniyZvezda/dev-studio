'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The renderer chooses an operation, never an IPC channel or an Electron API.
contextBridge.exposeInMainWorld('spike', Object.freeze({
  action: (name, payload) => ipcRenderer.invoke('spike:action', name, payload),
  send: (name, payload) => ipcRenderer.send('spike:event', name, payload),
  onState: (callback) => {
    if (typeof callback !== 'function') throw new TypeError('Expected a state callback');
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('spike:state', listener);
    return () => ipcRenderer.removeListener('spike:state', listener);
  },
}));
