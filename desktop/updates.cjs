// Packaged NSIS updates use release assets, never the managed project's git (§12.1).
const { navigationKind } = require('./policy.cjs');
const registrations = new WeakMap();

async function launchInstaller(updater, installFailure) {
  // NSIS quitAndInstall returns before spawnLog settles and schedules app.quit
  // even when that spawn subsequently fails. Observe its existing launcher (and
  // elevation/openPath fallbacks), never start a second installer ourselves (§12.1).
  const spawn = updater.spawnLog, launches = [];
  const installerPath = updater.installerPath;
  const shell = typeof spawn === 'function' && installerPath != null ? require('electron').shell : null;
  const openPath = shell?.openPath;
  let launched = false, fallbackExpected = false;
  const tracked = function (...args) {
    const work = spawn.apply(this, args);
    // Attach directly so ENOENT is observed before NSIS's own catch, including
    // when an adapter returns a Promise from another realm.
    launches.push(work.then(ok => { if (ok) launched = true; }, error => {
      if (error?.code === 'ENOENT' && args[0] === installerPath) fallbackExpected = true;
    }));
    return work; // Preserve NSIS's own failure/elevation handling.
  };
  const trackedOpenPath = function (...args) {
    const work = openPath.apply(this, args);
    // Observe only NSIS's ENOENT fallback for this installer, not other shell work.
    if (fallbackExpected && args[0] === installerPath) {
      launches.push(Promise.resolve(work).then(error => { if (error === '') launched = true; }, () => {}));
    }
    return work; // NSIS still owns dispatchError on rejection.
  };
  if (typeof spawn === 'function') updater.spawnLog = tracked;
  try {
    if (typeof openPath === 'function') shell.openPath = trackedOpenPath;
    await updater.quitAndInstall(false, true);
    for (let seen = 0; seen < launches.length;) {
      const batch = launches.slice(seen); seen = launches.length;
      await Promise.all(batch);
    }
    if (typeof spawn === 'function' && !launched) throw new Error(installFailure());
  } finally {
    if (updater.spawnLog === tracked) updater.spawnLog = spawn;
    if (shell?.openPath === trackedOpenPath) shell.openPath = openPath;
  }
}

// authorize owns exact role-URL/current-owner checks (§5.3.4); broadcast sends to
// every live registered contents. Controller lifetime ends only with dispose().
function registerUpdates({ ipcMain, window, authorize, broadcast, updater, version, install, prepareInstall,
  installFailure = () => 'Installer process launch was not confirmed', configureFeed = async () => {}, supported = true, reason = null }) {
  if (registrations.has(ipcMain)) return registrations.get(ipcMain);
  const controllerMode = typeof authorize === 'function';
  let disposed = false;
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
    if (disposed) return;
    state = { ...state, ...patch };
    if (broadcast) broadcast('studio:update-state', state);
    else if (window && !window.isDestroyed() && !window.webContents.isDestroyed?.())
      window.webContents.send('studio:update-state', state);
  };
  const fail = error => {
    // Even a late native error after restart was requested must not arm an
    // unattended install on the next ordinary Quit (§12.1). Keep the download.
    updater.autoInstallOnAppQuit = false;
    // BaseUpdater leaves this latch set on asynchronous NSIS spawn failure.
    // Reset only after an error so the retained download can actually retry.
    if (state.downloaded && typeof updater.quitAndInstallCalled === 'boolean') updater.quitAndInstallCalled = false;
    publish({ state: 'failed', error: error?.message || String(error) });
  };
  const listeners = {
    'update-available': info => publish({ latest: info.version, available: true }),
    'update-not-available': info => publish({ latest: info.version, available: false }),
    'download-progress': info => publish({ progress: Math.round(info.percent) }),
    'update-downloaded': () => publish({ state: 'idle', downloaded: true, progress: 100 }),
    error: fail,
  };
  for (const [name, listener] of Object.entries(listeners)) updater.on(name, listener);
  async function action(name) {
    if (disposed || name === 'state' || !supported) return state;
    if (!['check', 'download', 'install'].includes(name)) throw new Error('Unknown update action');
    if (pending || state.state === 'restarting') return state;
    pending = true;
    try {
      if (name === 'check') {
        if (state.downloaded) return state;
        publish({ state: 'checking', error: null });
        await configureFeed();
        if (disposed) return state;
        await updater.checkForUpdates();
        publish({ state: 'idle', checked_at: new Date().toISOString() });
      } else if (name === 'download') {
        if (!state.available || state.downloaded) return state;
        publish({ state: 'updating', error: null, progress: 0 });
        await updater.downloadUpdate();
      } else {
        if (!state.downloaded) throw new Error('Update has not been downloaded');
        // Main owns the shared Quit/install guard through the actual native call
        // (§5.3.5). install: async (commit) => boolean; false means cancelled.
        const commit = async () => {
          if (disposed) return;
          updater.autoInstallOnAppQuit = false;
          publish({ state: 'restarting', error: null });
          // Some adapters reject asynchronously rather than throwing. Let main
          // see either failure so it can release its shutdown authorization.
          try {
            await launchInstaller(updater, installFailure);
            if (state.state === 'failed') throw new Error(state.error);
          } catch (error) {
            // Keep the failure visible even if main handles it and returns false.
            fail(error);
            throw error;
          }
        };
        if (install) await install(commit);
        else {
          // Compatibility for embedders still supplying the old consent hook.
          if (!await prepareInstall()) return state;
          await commit();
        }
      }
    } catch (error) { fail(error); }
    finally { pending = false; }
    return state;
  }
  ipcMain.handle('studio:update', (event, name) => {
    const owner = controllerMode ? authorize(event) : { contents: window.webContents, window };
    if (disposed || !owner || owner.contents !== event.sender || owner.window?.isDestroyed?.()
      || event.sender?.isDestroyed?.() || event.senderFrame !== event.sender?.mainFrame
      || ((!controllerMode || owner.role === 'project') && navigationKind(event.senderFrame?.url) !== 'internal')
      || (controllerMode && !['shell', 'project'].includes(owner.role)))
      throw new Error('Untrusted update request');
    return action(name);
  });
  const first = supported && setTimeout(() => void action('check'), 15_000);
  const timer = supported && setInterval(() => void action('check'), 180 * 60_000);
  // Closing any/all shell windows leaves the updater alive in the tray (§5.3.5).
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    clearTimeout(first);
    clearInterval(timer);
    if (!controllerMode) window.removeListener?.('closed', dispose);
    ipcMain.removeHandler('studio:update');
    for (const [name, listener] of Object.entries(listeners)) updater.removeListener(name, listener);
    registrations.delete(ipcMain);
  };
  if (!controllerMode) window.once('closed', dispose);
  const api = { action, dispose };
  registrations.set(ipcMain, api);
  return api;
}
module.exports = { registerUpdates };
