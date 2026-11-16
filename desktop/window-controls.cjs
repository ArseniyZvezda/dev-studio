// App-level IPC, current owner resolution and per-window state (dev spec §5.3.4).
const { navigationKind } = require('./policy.cjs');
const registrations = new WeakMap();
function registerWindowControls(ipcMain, controller, tray, producerAttention) {
  const legacy = typeof controller?.authorize !== 'function';
  const existing = registrations.get(ipcMain);
  if (existing) {
    if (legacy) existing.registerWindow(controller, { tray, producerAttention });
    return existing;
  }
  const windows = new Map();
  let disposed = false;
  const state = window => ({ maximized: window.isMaximized(), focused: window.isFocused() });
  const resolve = event => {
    const owner = legacy
      ? [...windows.values()].find(record => record.window.webContents === event.sender)
      : controller.authorize(event);
    const window = owner?.window;
    const record = window && windows.get(window);
    if (disposed || !record || window.isDestroyed() || event.sender?.isDestroyed?.()
      || (!legacy && (!['shell', 'project'].includes(owner.role) || owner.contents !== event.sender))
      || event.senderFrame !== event.sender?.mainFrame
      || ((legacy || owner.role === 'project') && navigationKind(event.senderFrame?.url) !== 'internal'))
      throw new Error('Untrusted window-control request');
    return record;
  };
  ipcMain.handle('studio:window', (event, action, locale) => {
    // Never accept a renderer-supplied window id, including after live transfer.
    const { window, tray, producerAttention } = resolve(event);
    switch (action) {
      case 'state': break;
      case 'minimize': window.minimize(); break;
      case 'minimize-to-tray': tray?.hide(); break;
      case 'set-locale':
        if (!['en', 'ru'].includes(locale)) throw new Error('Invalid desktop locale');
        tray?.setLocale(locale);
        producerAttention?.refresh();
        break;
      case 'producer-attention':
        if (typeof locale !== 'boolean') throw new Error('Invalid Producer attention state');
        producerAttention?.set(locale);
        break;
      case 'toggle-maximize': window.isMaximized() ? window.unmaximize() : window.maximize(); break;
      case 'close': window.close(); break;
      default: throw new Error('Unknown window action');
    }
    return window.isDestroyed() ? null : state(window);
  });
  const api = {
    registerWindow(window, { tray, producerAttention } = {}) {
      if (disposed) throw new Error('Window controls disposed');
      if (windows.has(window)) return windows.get(window).unregister;
      const changed = () => {
        if (disposed || window.isDestroyed()) return;
        const contents = legacy ? [window.webContents] : controller.contentsForWindow(String(window.id));
        for (const target of new Set(contents)) {
          if (!target || target.isDestroyed?.()) continue;
          try { target.send('studio:window-state', state(window)); } catch { /* Closing renderers do not block others. */ }
        }
      };
      const unregister = () => {
        for (const event of ['maximize', 'unmaximize', 'focus', 'blur']) window.removeListener(event, changed);
        window.removeListener('closed', closed);
        windows.delete(window);
      };
      const closed = () => { unregister(); if (legacy && !windows.size) api.dispose(); };
      windows.set(window, { window, tray, producerAttention, unregister });
      for (const event of ['maximize', 'unmaximize', 'focus', 'blur']) window.on(event, changed);
      window.once('closed', closed);
      return unregister;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const record of [...windows.values()]) record.unregister();
      ipcMain.removeHandler('studio:window');
      registrations.delete(ipcMain);
    },
  };
  registrations.set(ipcMain, api);
  if (legacy) api.registerWindow(controller, { tray, producerAttention });
  return api;
}
module.exports = { registerWindowControls };
