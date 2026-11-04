// Packaged NSIS updates use release assets, never the managed project's git (§12.1).
const { ORIGIN } = require('./policy.cjs');

function registerUpdates({ ipcMain, window, updater, version, prepareInstall, configureFeed = async () => {}, supported = true, reason = null }) {
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  updater.allowPrerelease = false;
  updater.allowDowngrade = false;
  let state = {
    supported, reason, state: 'idle', enabled: supported, interval_minutes: 180,
    current: version, latest: null, available: false, checked_at: null,
    branch: null, upstream: null, dirty: false, step: null, error: null, log: [],
    downloaded: false, progress: 0,
  };
  let pending = false;
  const publish = patch => {
    state = { ...state, ...patch };
    if (!window.isDestroyed()) window.webContents.send('studio:update-state', state);
  };
  const fail = error => publish({ state: 'failed', error: error.message || String(error) });
  const listeners = {
    'update-available': info => publish({ latest: info.version, available: true }),
    'update-not-available': info => publish({ latest: info.version, available: false }),
    'download-progress': info => publish({ progress: Math.round(info.percent) }),
    'update-downloaded': () => publish({ state: 'idle', downloaded: true, progress: 100 }),
    error: fail,
  };
  for (const [name, listener] of Object.entries(listeners)) updater.on(name, listener);
  async function action(name) {
    if (name === 'state' || !supported) return state;
    if (!['check', 'download', 'install'].includes(name)) throw new Error('Unknown update action');
    if (pending || state.state === 'restarting') return state;
    pending = true;
    try {
      if (name === 'check') {
        if (state.downloaded) return state;
        publish({ state: 'checking', error: null });
        await configureFeed();
        await updater.checkForUpdates();
        publish({ state: 'idle', checked_at: new Date().toISOString() });
      } else if (name === 'download') {
        if (!state.available || state.downloaded) return state;
        publish({ state: 'updating', error: null, progress: 0 });
        await updater.downloadUpdate();
      } else {
        if (!state.downloaded) throw new Error('Update has not been downloaded');
        // Confirmation and shutdown finish before NSIS can replace the bundled runtime.
        if (!await prepareInstall()) return state;
        publish({ state: 'restarting', error: null });
        updater.quitAndInstall(false, true);
      }
    } catch (error) { fail(error); }
    finally { pending = false; }
    return state;
  }
  ipcMain.handle('studio:update', (event, name) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
      || new URL(event.senderFrame.url).origin !== ORIGIN) throw new Error('Untrusted update request');
    return action(name);
  });
  const first = supported && setTimeout(() => void action('check'), 15_000);
  const timer = supported && setInterval(() => void action('check'), 180 * 60_000);
  window.once('closed', () => {
    clearTimeout(first);
    clearInterval(timer);
    ipcMain.removeHandler('studio:update');
    for (const [name, listener] of Object.entries(listeners)) updater.removeListener(name, listener);
  });
  return { action };
}
module.exports = { registerUpdates };
