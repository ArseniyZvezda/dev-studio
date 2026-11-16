// Per-user Windows login setting (dev spec §2.7), app-owned IPC (§5.3.4).
const { navigationKind } = require('./policy.cjs');
const registrations = new WeakMap();
// authorize validates registration, owner and exact role URL (including the local
// shell document). broadcast(channel, state) fans out to live registered contents.
// Controller mode lives until explicit dispose(); window is legacy-only.
function registerStartup({ app, ipcMain, window, authorize, broadcast, platform = process.platform, executable = process.execPath }) {
  if (registrations.has(ipcMain)) return registrations.get(ipcMain);
  const controllerMode = typeof authorize === 'function';
  let disposed = false;
  const supported = platform === 'win32';
  const options = { path: executable, args: app.isPackaged ? [] : [app.getAppPath()] };
  const read = () => {
    if (!supported) return { supported: false, enabled: false };
    const state = app.getLoginItemSettings(options);
    const item = state.launchItems?.find(item => item.name === 'Voxarium Studio');
    return { supported: true, enabled: state.openAtLogin && item?.enabled !== false };
  };
  ipcMain.handle('studio:startup', (event, enabled) => {
    const owner = controllerMode ? authorize(event) : { contents: window.webContents, window };
    if (disposed || !owner || owner.contents !== event.sender || owner.window?.isDestroyed?.()
      || event.sender?.isDestroyed?.() || event.senderFrame !== event.sender?.mainFrame
      || ((!controllerMode || owner.role === 'project') && navigationKind(event.senderFrame?.url) !== 'internal')
      || (controllerMode && !['shell', 'project'].includes(owner.role)))
      throw new Error('Untrusted startup request');
    if (enabled !== undefined) {
      if (!supported || typeof enabled !== 'boolean') throw new Error('Invalid startup setting');
      app.setLoginItemSettings({ ...options, name: 'Voxarium Studio', openAtLogin: enabled, enabled });
    }
    const state = read();
    if (enabled !== undefined) broadcast?.('studio:startup-state', state);
    return state;
  });
  const api = { read, dispose() {
    if (disposed) return;
    disposed = true;
    if (!controllerMode) window.removeListener?.('closed', api.dispose);
    ipcMain.removeHandler('studio:startup');
    registrations.delete(ipcMain);
  } };
  registrations.set(ipcMain, api);
  if (!controllerMode) window.once('closed', api.dispose);
  return api;
}
module.exports = { registerStartup };
