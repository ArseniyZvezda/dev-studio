// Per-user Windows login setting (dev spec §2.7); never enabled implicitly.
const { ORIGIN } = require('./policy.cjs');
function registerStartup({ app, ipcMain, window, platform = process.platform, executable = process.execPath }) {
  const supported = platform === 'win32';
  const options = { path: executable, args: app.isPackaged ? [] : [app.getAppPath()] };
  const read = () => {
    if (!supported) return { supported: false, enabled: false };
    const state = app.getLoginItemSettings(options);
    const item = state.launchItems?.find(item => item.name === 'Voxarium Studio');
    return { supported: true, enabled: state.openAtLogin && item?.enabled !== false };
  };
  ipcMain.handle('studio:startup', (event, enabled) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
      || new URL(event.senderFrame.url).origin !== ORIGIN) throw new Error('Untrusted startup request');
    if (enabled !== undefined) {
      if (!supported || typeof enabled !== 'boolean') throw new Error('Invalid startup setting');
      app.setLoginItemSettings({ ...options, name: 'Voxarium Studio', openAtLogin: enabled, enabled });
    }
    return read();
  });
  window.once('closed', () => ipcMain.removeHandler('studio:startup'));
}
module.exports = { registerStartup };
