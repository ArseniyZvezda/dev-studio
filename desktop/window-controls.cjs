// The title bar can only control its own main window (dev spec §2.7).
const { ORIGIN } = require('./policy.cjs');
function registerWindowControls(ipcMain, window, tray, producerAttention) {
  const state = () => ({ maximized: window.isMaximized(), focused: window.isFocused() });
  const trusted = event => event.sender === window.webContents
    && event.senderFrame === window.webContents.mainFrame
    && new URL(event.senderFrame.url).origin === ORIGIN;
  ipcMain.handle('studio:window', (event, action, locale) => {
    if (!trusted(event)) throw new Error('Untrusted window-control request');
    switch (action) {
      case 'state': break;
      case 'minimize': window.minimize(); break;
      case 'minimize-to-tray': tray.hide(); break;
      case 'set-locale':
        if (!['en', 'ru'].includes(locale)) throw new Error('Invalid desktop locale');
        tray.setLocale(locale);
        producerAttention?.refresh();
        break;
      case 'producer-attention':
        if (typeof locale !== 'boolean') throw new Error('Invalid Producer attention state');
        producerAttention?.set(locale);
        break;
      case 'toggle-maximize': window.isMaximized() ? window.unmaximize() : window.maximize(); break;
      case 'close': window.close(); break; // Existing exit confirmation stays in charge.
      default: throw new Error('Unknown window action');
    }
    return window.isDestroyed() ? null : state();
  });
  const changed = () => window.webContents.send('studio:window-state', state());
  for (const event of ['maximize', 'unmaximize', 'focus', 'blur']) window.on(event, changed);
  window.once('closed', () => ipcMain.removeHandler('studio:window'));
}
module.exports = { registerWindowControls };
