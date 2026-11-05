// Desktop distribution and ownership rules: dev spec §2.7.
const { app, BrowserWindow, dialog, shell, Menu, ipcMain, Tray, nativeImage } = require('electron');
const { registerWindowControls } = require('./window-controls.cjs');
const { registerTray } = require('./tray.cjs');
const { backendStartPlan } = require('./backend-version.cjs');
const { configureUpdateFeed } = require('./update-feed.cjs');
const { spawn, execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { ORIGIN, navigationKind, registerPermissions } = require('./policy.cjs');
const metadata = require('./package.json');
const productName = metadata.productName;
const appIcon = path.join(__dirname, 'assets/icon.ico');
// The renamed desktop owns a separate profile and single-instance lock (§2.7).
app.setPath('userData', path.join(app.getPath('appData'), 'Voxarium Studio'));
const translate = locale => require(app.isPackaged ? `./locales/${locale}.json` : `../frontend/src/locales/${locale}.json`).desktop;
let window, splash, owned = false, quitting = false, messages, runDir, shutdownFile;
let desktopLocale;
const currentMessages = () => desktopLocale ? translate(desktopLocale) : messages;
const instance = randomUUID();
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function logStartup(event, details = {}) {
  if (!runDir) return;
  try {
    fs.appendFileSync(path.join(runDir, 'desktop.log'),
      `${JSON.stringify({ time: new Date().toISOString(), event, ...details })}\n`);
  } catch (error) { console.error('Could not write desktop startup log:', error); }
}
async function health() {
  try {
    const response = await fetch(`${ORIGIN}/healthz`, { signal: AbortSignal.timeout(1500) });
    return response.ok ? await response.json() : null;
  } catch { return null; }
}
async function stopOwned() {
  if (!shutdownFile) return; // Independently started servers have no owned stop file.
  await new Promise((resolve, reject) => execFile('powershell.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', app.isPackaged ? path.join(process.resourcesPath, 'stop-backend.ps1')
      : path.join(__dirname, 'scripts/stop-backend.ps1'), '-StopFile', shutdownFile,
  ], { windowsHide: true, timeout: 30_000 }, error => error ? reject(error) : resolve()));
  owned = false;
  shutdownFile = null;
}
async function startBackend() {
  const existing = await health();
  // §2.7: require the same tag/commit identity as the backend and built frontend.
  const expectedVersion = app.isPackaged
    ? metadata.studioVersion ?? app.getVersion()
    : require('./scripts/release.cjs').sourceReleaseInfo(path.resolve(__dirname, '..'), metadata.version).version;
  logStartup('backend-check', { releaseVersion: app.getVersion(), expectedVersion, existing });
  let plan;
  try {
    plan = await backendStartPlan(existing, expectedVersion, async () => {
      const token = process.env.VOXARIUM_STUDIO_TOKEN;
      const response = await fetch(`${ORIGIN}/api/project`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: AbortSignal.timeout(5000), cache: 'no-store',
      });
      if (!response.ok) throw new Error('restartProjectUnknown');
      return response.json();
    }, async () => {
      // Commit hashes identify builds but cannot order them (§2.7).
      const result = await dialog.showMessageBox(splash, {
        type: 'question',
        message: messages.confirmBackendSwitch.replace('{frontend}', expectedVersion).replace('{backend}', existing.version),
        buttons: [messages.cancel, messages.switchBackend], defaultId: 0, cancelId: 0,
      });
      return result.response === 1;
    });
  } catch (error) { throw new Error(messages[error.message] || error.message); }
  if (plan.cancelled) {
    logStartup('backend-switch-cancelled', { expectedVersion, actualVersion: existing.version });
    app.exit(0);
    return false;
  }
  if (plan.reuse) return true;
  if (plan.replacement) {
    const message = messages.backendVersionMismatch.replace('{frontend}', expectedVersion).replace('{backend}', existing.version);
    await splash.loadFile(path.join(__dirname, 'splash.html'), { query: { message } });
    // Recheck immediately before the launcher can evict the old listener.
    const current = await health();
    if (current && (current.service !== existing.service || current.version !== existing.version
      || current.desktop_instance !== existing.desktop_instance)) throw new Error(messages.backendChanged);
  }
  const root = app.isPackaged ? path.join(process.resourcesPath, 'runtime') : path.resolve(__dirname, '..');
  shutdownFile = path.join(runDir, `${instance}.stop`);
  const log = fs.openSync(path.join(runDir, 'launcher.log'), 'w');
  // run.ps1 detaches the backend itself. DETACHED_PROCESS makes Windows PowerShell
  // exit successfully without executing the script when its stdin is ignored.
  const launcher = spawn('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(root, 'run.ps1'), ...plan.args], {
    cwd: root, windowsHide: true, 
    stdio: ['ignore', log, log],
    env: { ...process.env, VOXARIUM_STUDIO_RUN_DIR: runDir, VOXARIUM_STUDIO_DESKTOP_INSTANCE: instance,
      VOXARIUM_STUDIO_DESKTOP_PARENT: String(process.pid), VOXARIUM_STUDIO_DESKTOP_STOP_FILE: shutdownFile },
  });
  fs.closeSync(log);
  let failure;
  launcher.on('error', error => { failure = error; });
  launcher.on('exit', code => { if (code) failure = new Error(messages.startFailed); });
  launcher.unref();
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const result = await health();
    if (result?.desktop_instance === instance) {
      owned = true;
      logStartup('backend-started', { expectedVersion, actualVersion: result.version });
      if (result.version !== expectedVersion) throw new Error(messages.unknownBackendVersion);
      return true;
    }
    if (failure) throw failure;
    await delay(250);
  }
  throw new Error(messages.startFailed);
}
function openExternal(url) {
  if (navigationKind(url) === 'external') shell.openExternal(url).catch(() => {});
}
async function boot() {
  messages = translate(app.getLocale().startsWith('ru') ? 'ru' : 'en');
  runDir = path.join(app.getPath('userData'), 'run');
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(path.join(runDir, 'desktop.log'), '');
  app.setAppUserModelId('studio.voxarium.desktop');
  Menu.setApplicationMenu(null);
  splash = new BrowserWindow({ width: 460, height: 300, frame: false, resizable: false,
    center: true, show: false, backgroundColor: '#17191e', title: productName, icon: appIcon,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  splash.once('ready-to-show', () => splash.show());
  await splash.loadFile(path.join(__dirname, 'splash.html'), { query: { message: messages.loading } });
  splash.show();
  if (!await startBackend()) return;
  window = new BrowserWindow({ width: 1440, height: 960, minWidth: 900, minHeight: 600,
    title: productName, show: false, frame: false, backgroundColor: '#14161b', icon: appIcon,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
  });
  const tray = registerTray({ app, window, Tray, Menu,
    icon: nativeImage.createFromPath(path.join(__dirname, 'assets/tray.png')), productName,
    messages: locale => { if (locale) desktopLocale = locale; return currentMessages(); },
  });
  const producerAttention = require('./producer-attention.cjs').registerProducerAttention({
    window,
    icon: nativeImage.createFromPath(path.join(__dirname, 'assets/notification.png')).resize({ width: 16, height: 16 }),
    description: () => currentMessages().producerWaiting,
  });
  registerWindowControls(ipcMain, window, tray, producerAttention);
  require('./startup.cjs').registerStartup({ app, ipcMain, window });
  if (app.isPackaged) {
    const updateRepo = process.env.VOXARIUM_STUDIO_UPDATE_REPO;
    require('./updates.cjs').registerUpdates({ ipcMain, window,
      updater: require('electron-updater').autoUpdater, version: app.getVersion(),
      supported: !!updateRepo, reason: updateRepo ? null : messages.updateNotConfigured,
      configureFeed: () => configureUpdateFeed({ updater: require('electron-updater').autoUpdater,
        accessError: currentMessages().updateAccess }),
      prepareInstall: async () => {
        const m = currentMessages();
        const result = await dialog.showMessageBox(window, { type: 'question',
          message: m.installUpdate, buttons: [m.cancel, m.installAndRestart], defaultId: 0, cancelId: 0 });
        if (result.response !== 1) return false;
        await stopOwned();
        if (owned && (await health())?.desktop_instance === instance) throw new Error(m.stopFailed);
        owned = false;
        quitting = true;
        return true;
      },
    });
  } else ipcMain.handle('studio:update', () => null);
  window.once('ready-to-show', () => { window.show(); if (splash && !splash.isDestroyed()) splash.close(); });
  registerPermissions(window.webContents);
  window.webContents.on('will-navigate', (event, url) => {
    if (navigationKind(url) !== 'internal') { event.preventDefault(); openExternal(url); }
  });
  window.webContents.on('will-redirect', (event, url) => {
    if (navigationKind(url) !== 'internal') event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (navigationKind(url) === 'internal') window.loadURL(url);
    else openExternal(url);
    return { action: 'deny' };
  });
  let closePending = false;
  window.on('close', event => {
    if (quitting || !owned) return;
    event.preventDefault();
    if (closePending) return;
    closePending = true;
    void (async () => {
      const m = currentMessages();
      const result = await dialog.showMessageBox(window, { type: 'question', message: m.close,
        buttons: [m.cancel, m.quit], defaultId: 0, cancelId: 0 });
      if (result.response === 1) { quitting = true; app.quit(); }
    })().finally(() => { closePending = false; });
  });
  await window.loadURL(ORIGIN);
  logStartup('window-loaded');
  window.show();
  if (splash && !splash.isDestroyed()) splash.close();
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('window-all-closed', () => app.quit());
  let stopping = false;
  app.on('before-quit', event => {
    if (!shutdownFile) return;
    event.preventDefault();
    if (stopping) return;
    quitting = true;
    stopping = true;
    stopOwned().then(() => app.quit()).catch(error => {
      stopping = false;
      quitting = false;
      dialog.showErrorBox(productName, `${currentMessages().stopFailed}\n${error.message}`);
    });
  });
  app.whenReady().then(boot).catch(async error => {
    logStartup('startup-failed', { error: error.stack || String(error) });
    dialog.showErrorBox(productName, `${messages?.startFailed || error.message}\n${error.message}\n${runDir || ''}`);
    await stopOwned();
    app.exit(1);
  });
}
