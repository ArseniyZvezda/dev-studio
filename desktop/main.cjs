// Desktop distribution and ownership rules: dev spec §2.7.
const { app, BrowserWindow, WebContentsView, Notification, dialog, shell, Menu, ipcMain, Tray, nativeImage, screen, autoUpdater: nativeUpdater } = require('electron');
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
const localeMessages = locale => require(app.isPackaged ? `./locales/${locale}.json` : `../frontend/src/locales/${locale}.json`);
const translate = locale => localeMessages(locale).desktop;
let window, splash, owned = false, quitting = false, messages, runDir, shutdownFile;
let desktopLocale;
let desktopController;
let shutdownPending, stopPending, backendStopped = false;
let updaterQuitPending = false;
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
function stopOwned() {
  if (stopPending) return stopPending;
  stopPending = stopOwnedImpl().finally(() => { stopPending = undefined; });
  return stopPending;
}
async function stopOwnedImpl() {
  if (!shutdownFile) return; // Independently started servers have no owned stop file.
  await new Promise((resolve, reject) => execFile('powershell.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', app.isPackaged ? path.join(process.resourcesPath, 'stop-backend.ps1')
      : path.join(__dirname, 'scripts/stop-backend.ps1'), '-StopFile', shutdownFile,
  ], { windowsHide: true, timeout: 30_000 }, error => error ? reject(error) : resolve()))
    .catch(error => { throw new Error(`${currentMessages().stopFailed}\n${error.message}`); });
  // The script must finish AND our listener must be gone. Retain the stop
  // capability until both checks succeed so Quit/install failures are retryable.
  if ((await health())?.desktop_instance === instance) throw new Error(currentMessages().stopFailed);
  owned = false;
  shutdownFile = null;
  backendStopped = true;
}
async function activitySummary() {
  // A failed installer may be retried after a successful owned-process stop.
  // That process has no remaining work; do not require its now-dead HTTP API.
  if (backendStopped && !await health()) {
    return { status: 'idle', requires_confirmation: false, projects: [], errors: [] };
  }
  const m = currentMessages();
  try {
    const token = process.env.VOXARIUM_STUDIO_TOKEN;
    const response = await fetch(`${ORIGIN}/api/projects/activity`, {
      redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(5000),
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error();
    const summary = await response.json();
    const status = value => ['idle', 'active', 'unknown'].includes(value);
    if (!summary || !status(summary.status) || typeof summary.requires_confirmation !== 'boolean'
      || !Array.isArray(summary.projects) || !Array.isArray(summary.errors)
      || !summary.projects.every(project => project && typeof project.name === 'string'
        && typeof project.root === 'string' && status(project.status) && Array.isArray(project.errors)
        && project.counts && typeof project.counts === 'object' && !Array.isArray(project.counts)
        && Object.values(project.counts).every(count => Number.isSafeInteger(count) && count >= 0))) throw new Error();
    return summary;
  } catch { throw new Error(m.activityFailed); }
}
function activityDetail(summary, m) {
  const lines = summary.projects.filter(project => project.status !== 'idle' || project.errors.length
    || Object.values(project.counts).some(Boolean)).map(project => {
    const text = project.status === 'unknown' || project.errors.length ? m.projectActivityUnknown : m.projectActivity;
    return text.replace('{project}', project.name).replace('{root}', project.root)
      .replace('{count}', String(Object.values(project.counts).reduce((sum, count) => sum + count, 0)));
  });
  if (summary.status === 'unknown' || summary.errors.length) lines.push(m.activityUnknown);
  return lines.join('\n');
}
// Explicit app-wide Quit/update policy: dev spec §5.3.5 and §12.1.
function requestShutdown(kind, commit) {
  // Quit, repeat Quit and install share one app-level operation, not one per
  // visible window. A competing action cannot inherit another action's consent.
  if (shutdownPending || quitting) return Promise.resolve(false);
  shutdownPending = Promise.resolve().then(async () => {
    if (!desktopController) { // Fatal/incomplete boot has no project surfaces.
      await stopOwned(); quitting = true; await commit(); return true;
    }
    // Restore opens/reorders through the same registry. Never acquire the close
    // gate and then wait for that work while holding its exclusion lock.
    await desktopController.sessionReady?.();
    return desktopController.bridge.withAppCloseBarrier(async validate => {
      const m = currentMessages(), summary = await activitySummary();
      const detail = activityDetail(summary, m);
      const needsConfirmation = kind === 'update' || summary.requires_confirmation
        || summary.status !== 'idle' || !!detail;
      if (needsConfirmation) {
        const options = { type: 'question', message: kind === 'update' ? m.installUpdate : m.close,
          detail: [backendStopped ? m.stoppedShutdown : shutdownFile ? m.ownedShutdown : m.independentShutdown,
            detail].filter(Boolean).join('\n\n'),
          buttons: [m.cancel, kind === 'update' ? m.installAndRestart : m.quit], defaultId: 0, cancelId: 0 };
        const owner = desktopController.getActiveWindow();
        const result = await (owner ? dialog.showMessageBox(owner, options) : dialog.showMessageBox(options));
        if (result.response !== 1) return false;
      }
      validate(); // Recheck navigation/crash after HTTP and the native dialog await.
      // Persist topology while all project surfaces still exist, before stopping
      // an owned backend or Electron's service teardown (dev spec §5.3.6).
      await desktopController.freezeSession?.();
      validate();
      await stopOwned();
      if (kind === 'quit') quitting = true;
      await commit();
      quitting = true;
      return true;
    });
  }).catch(error => { quitting = false; desktopController?.resumeSession?.(); throw error; })
    .finally(() => { shutdownPending = undefined; });
  return shutdownPending;
}
// Keep validated renderer input fences until the updater actually starts app
// teardown, or fails. electron-updater schedules app.quit after quitAndInstall.
function installAndQuit(updater, commit) {
  return new Promise((resolve, reject) => {
    let timer, settled = false;
    const cleanup = () => { clearTimeout(timer); app.removeListener('will-quit', finished); updater.removeListener('error', failed); };
    const finished = () => { if (settled) return; settled = true; cleanup(); resolve(); };
    const failed = error => { if (settled) return; settled = true; quitting = false; cleanup(); reject(error); };
    app.once('will-quit', finished); updater.once('error', failed);
    timer = setTimeout(() => failed(new Error(currentMessages().installFailed)), 30000);
    Promise.resolve().then(commit).then(() => {
      if (!settled) { quitting = true; app.quit(); }
    }).catch(failed);
  });
}
let backendBoot;
function startBackend() {
  // App-only boot capability: opening/focusing surfaces never reaches a launcher (§5.3).
  return backendBoot ??= startBackendImpl();
}
async function startBackendImpl() {
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
// Same SPA assets, separate checked document/IPC role (§5.3.4). The bootstrap
// entry chooses shell before importing project stores, sockets or plugins.
const SHELL_URL = `${ORIGIN}/?desktopShell=1`;

function createDesktopController({ backend, canonicalizeRoot, shellURL = SHELL_URL,
  configureWindow = () => {}, configureContents = () => {}, onTopologyChanged = () => {}, nativeTerminal, sessionState } = {}) {
  const { SHELL_HEADER_HEIGHT, DesktopTabs, createBackendAdapter, createElectronAdapter, canonicalProjectRoot } = require('./tabs.cjs');
  const { registerTabsIpc, createShellServices, documentURL } = require('./tabs-ipc.cjs');
  const windows = new Map();
  const windowLoads = new Map();
  const menuWindows = new Set(), viewBindings = new WeakMap();
  let bridge, drag, session, disposed = false, disposal, activation, windowCreation;
  const pageWaiters = new Set();
  function pagesChanged() {
    session?.changed();
    const pages = new Set(bridge.snapshotPages().map(item => item.tabId));
    for (const waiter of [...pageWaiters]) {
      const tab = registry.getTab(waiter.tabId);
      if (!tab || tab.state === 'crashed') waiter.finish(new Error('Project page is unavailable'));
      else if (pages.has(waiter.tabId)) waiter.finish();
    }
  }
  const pendingCloses = new Map();
  const nativeAdapter = createElectronAdapter({ WebContentsView,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs') },
    configureProjectView(view, binding) {
      viewBindings.set(view, binding);
      const release = bridge.registerProjectView(view, binding);
      const contents = view.webContents;
      const external = (event, address) => {
        if (navigationKind(address) === 'external') { event.preventDefault(); openExternal(address); }
      };
      const cleanup = () => {
        contents.removeListener('will-navigate', external);
        release();
      };
      try {
        configureContents(contents);
        // Keep the external-link protocol allowlist, but never navigate a view
        // to the legacy SPA or another project's URL (§2.7, §5.3.4).
        contents.on('will-navigate', external);
        contents.setWindowOpenHandler(({ url }) => { openExternal(url); return { action: 'deny' }; });
      } catch (error) { cleanup(); throw error; }
      return cleanup;
    },
  });
  const electron = { ...nativeAdapter,
    loadProjectView(view, binding) {
      bridge.publishState(); // Registry has inserted its loading tab already.
      return nativeAdapter.loadProjectView(view, binding);
    },
    focusView(view) {
      const binding = viewBindings.get(view);
      const tab = registry.snapshot().tabs.find(item => item.project === binding
        || (item.project.root === binding?.root && item.project.project_id === binding.project_id));
      const owner = tab && windows.get(tab.ownerWindowId);
      if (owner && (menuWindows.has(tab.ownerWindowId) || tab.state === 'crashed')) owner.webContents.focus();
      else nativeAdapter.focusView(view);
    },
  };
  const registry = new DesktopTabs({ electron,
    onCommit: () => { session?.changed(); onTopologyChanged(); },
    backend: backend || createBackendAdapter({ fetch, token: () => process.env.VOXARIUM_STUDIO_TOKEN }),
    // VS-51: detach/recovery reuse the resident shell factory only. No launcher,
    // project open, renderer reconstruction or backend lifecycle on this path.
    createShell: () => createWindow({ show: false }),
    // Run only after a transfer commits/releases its gate; never destroy the
    // source while its live view is still being attached or rolled back.
    onWindowEmpty: closeEmptyWindow,
    ...(canonicalizeRoot ? { canonicalizeRoot } : {}),
  });
  function closeEmptyWindow(windowId) {
    const id = String(windowId), native = windows.get(id), record = registry.getWindow(id);
    // Shared post-commit path for last-tab, prepared native close and transfer.
    // Destroy only an empty shell: a transferred view belongs to its new owner.
    if (disposed || !native || native.isDestroyed() || !record || record.tabIds.length) return false;
    native.destroy();
    bridge.publishState();
    return true;
  }
  function requestCloseWindow(windowId) {
    const id = String(windowId);
    if (pendingCloses.has(id)) return pendingCloses.get(id);
    const pending = Promise.resolve().then(() => session?.ready()).then(() => bridge.requestCloseWindow(id)).then(prepared => {
      if (prepared) closeEmptyWindow(id);
      return prepared;
    }).catch(error => {
      logStartup('window-close-failed', { windowId: id, error: error.message });
      if (windows.has(id)) bridge.setWindowError(id, error.message);
      return false;
    }).finally(() => { if (pendingCloses.get(id) === pending) pendingCloses.delete(id); });
    pendingCloses.set(id, pending);
    return pending;
  }
  function layoutMenu(id) {
    const native = windows.get(id);
    if (!native || native.isDestroyed()) return;
    // Reserve the entire shell while a menu/dialog is open. The existing live
    // view stays attached with zero hit area, never destroyed/reloaded/remounted.
    const record = registry.getWindow(id);
    const crashed = record?.activeTabId && registry.getTab(record.activeTabId)?.state === 'crashed';
    const top = menuWindows.has(id) || crashed ? native.getContentSize()[1] : SHELL_HEADER_HEIGHT;
    if (record && record.insets.top !== top) {
      registry.setContentInsets(id, { top });
      if (crashed && native.isFocused()) native.webContents.focus();
    }
  }
  bridge = registerTabsIpc({ ipcMain, registry, shellURL, onWindowEmpty: closeEmptyWindow,
    onPageChanged: pagesChanged,
    tabDrag: {
      begin: (...args) => drag.begin(...args),
      cancel: (...args) => drag.cancel(...args),
      setGeometry: (...args) => drag.setGeometry(...args),
    },
    beforePublish() { for (const id of windows.keys()) layoutMenu(id); },
    shellServices: createShellServices({ fetch, token: () => process.env.VOXARIUM_STUDIO_TOKEN }),
    setMenuOpen(id, open) {
      if (open === menuWindows.has(id)) return;
      if (open) menuWindows.add(id); else menuWindows.delete(id);
      layoutMenu(id);
      const native = windows.get(id), active = registry.getWindow(id)?.activeTabId;
      // A deduplicated open may have focused a different owner window. Closing
      // the source menu then restores only its geometry, never steals focus or
      // shows the source again (§5.3.1 D1). Manual close in the focused window
      // may return keyboard input to its existing native project view.
      if (!native?.isFocused()) return;
      if (!open && active && registry.getTab(active)?.state === 'ready') registry.activateTab(active);
      else native?.webContents.focus();
    },
  });
  drag = require('./tab-drag.cjs').createTabDrag({ registry, screen,
    getWindows: () => [...windows.values()],
    authorizeSource: (event, request) => bridge.authorizeDrag(event, request),
    isMenuOpen: id => menuWindows.has(String(id)),
    publishState: () => bridge.publishState(), nativeTerminal, log: logStartup,
  });
  async function createWindow({ show = true, geometry } = {}) {
    if (disposed) throw new Error('Desktop controller is disposed');
    const native = new BrowserWindow({ width: 1600, height: 1000, minWidth: 900, minHeight: 600,
      ...(geometry ? { x: geometry.x, y: geometry.y, width: geometry.width, height: geometry.height,
        minWidth: Math.min(900, geometry.width), minHeight: Math.min(600, geometry.height) } : {}),
      title: productName, show: false, frame: false, backgroundColor: '#14161b', icon: appIcon,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'),
        nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
    });
    const id = String(native.id);
    let registered = false, releaseShell;
    // Shell navigation cannot become a project/remote renderer with shell rights.
    const guard = (event, address) => {
      try { if (documentURL(address) === documentURL(shellURL)) return; } catch {}
      event.preventDefault();
    };
    const resize = () => { layoutMenu(id); bridge.publishState(); session?.changed(); };
    const changed = () => session?.changed();
    const close = event => {
      // Native close is a UI request, never Quit or backend ownership cleanup
      // (dev spec §5.3.5). Explicit app.quit keeps its separate teardown path.
      if (quitting || disposed) return;
      event.preventDefault();
      void requestCloseWindow(id);
    };
    try {
      registry.registerWindow(native, { insets: { top: SHELL_HEADER_HEIGHT } }); registered = true;
      windows.set(id, native);
      releaseShell = bridge.registerWindow(native);
      drag.registerWindow(native);
      native.once('closed', () => { if (windows.get(id) === native) windows.delete(id); });
      native.webContents.on('will-navigate', guard);
      native.webContents.on('will-redirect', guard);
      native.once('closed', () => {
        menuWindows.delete(id); native.removeListener('resize', resize);
        for (const event of ['move', 'focus', 'maximize', 'unmaximize']) native.removeListener(event, changed);
        native.removeListener('close', close);
        native.webContents.removeListener('will-navigate', guard);
        native.webContents.removeListener('will-redirect', guard);
      });
      native.on('resize', resize);
      for (const event of ['move', 'focus', 'maximize', 'unmaximize']) native.on(event, changed);
      native.on('close', close);
      native.webContents.setWindowOpenHandler(({ url }) => { openExternal(url); return { action: 'deny' }; });
      configureContents(native.webContents);
      configureWindow(native);
      const loaded = native.loadURL(shellURL);
      windowLoads.set(id, loaded);
      await loaded;
      windowLoads.delete(id);
      if (native.isDestroyed()) throw new Error('Shell was closed while loading');
      if (geometry?.maximized) native.maximize();
      if (show) native.show();
      bridge.publishState();
      session?.changed();
      return native;
    } catch (error) {
      windowLoads.delete(id);
      releaseShell?.();
      if (registered) registry.unregisterWindow(id);
      if (windows.get(id) === native) windows.delete(id);
      if (!native.isDestroyed()) native.destroy();
      throw error;
    }
  }
  const getActiveWindow = () => [...windows.values()].find(item => !item.isDestroyed() && item.isFocused())
    || [...windows.values()].find(item => !item.isDestroyed());
  async function ensureWindow() {
    await session.ready();
    if (disposed || quitting) throw new Error('Desktop is closing');
    if (windowCreation) return windowCreation;
    const native = getActiveWindow();
    if (native) { await windowLoads.get(String(native.id)); return native; }
    windowCreation = createWindow({ show: false }).finally(() => { windowCreation = undefined; });
    return windowCreation;
  }
  const { createSessionController, createSessionStateAdapter } = require('./session-state.cjs');
  session = createSessionController({ registry, bridge, createWindow,
    adapter: sessionState || createSessionStateAdapter({ fetch, token: () => process.env.VOXARIUM_STUDIO_TOKEN }),
    getWindow: id => windows.get(String(id)), getActiveWindow,
    getDisplays: () => screen.getAllDisplays?.() ?? [],
    reportError(error, { windowId, root } = {}) {
      const m = currentMessages();
      const detail = [m.sessionFailed, root, m[error.code] || error.message].filter(Boolean).join('\n');
      logStartup('desktop-session-failed', { root, error: error.message });
      if (windowId) bridge.setWindowError(windowId, detail);
      // A ready native project view can cover the shell's body. Show each failed
      // entry explicitly even when another restored project remains usable.
      dialog.showErrorBox(productName, detail);
    },
  });
  return { registry, bridge, createWindow, requestCloseWindow, closeEmptyWindow,
    ensureWindow,
    resolveRoot: canonicalizeRoot || canonicalProjectRoot,
    waitForProjectPage(tabId) {
      if (bridge.snapshotPages().some(item => item.tabId === tabId)) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const waiter = { tabId, finish(error) {
          pageWaiters.delete(waiter); clearTimeout(timer); error ? reject(error) : resolve();
        } };
        const timer = setTimeout(() => waiter.finish(new Error('Project page bootstrap timed out')), 30000);
        pageWaiters.add(waiter);
        pagesChanged();
      });
    },
    initializeSession: seed => session.initialize(seed),
    sessionReady: () => session.ready(),
    flushSession: () => session.flush(), freezeSession: () => session.freeze(), resumeSession: () => session.resume(),
    getWindow: id => windows.get(String(id)),
    getActiveWindow,
    showWindow() {
      if (disposed || quitting) return Promise.resolve();
      // Click + double-click + second-instance share one asynchronous creation.
      // This factory is resident UI only: no seed read, ensure or launcher.
      if (activation) return activation;
      activation = (async () => {
        await session.ready();
        const native = await ensureWindow();
        await windowLoads.get(String(native.id));
        if (disposed || quitting || native.isDestroyed()) return;
        const active = registry.getWindow(String(native.id))?.activeTabId;
        if (active) registry.activateTab(active);
        else {
          if (native.isMinimized()) native.restore();
          native.show(); native.focus(); native.webContents.focus();
        }
        bridge.publishState();
        return native;
      })().finally(() => { activation = undefined; });
      return activation;
    },
    async openProject(root, windowId, options = {}) {
      try {
        const tab = await registry.openProject(root, { ...options, windowId });
        bridge.setWindowError(windowId, undefined); return tab;
      } finally { bridge.publishState(); }
    },
    activateTab(tabId) { const tab = registry.activateTab(tabId); bridge.publishState(); return tab; },
    dispose() {
      if (disposed) return disposal;
      disposed = true;
      for (const waiter of [...pageWaiters]) waiter.finish(new Error('Desktop disposed'));
      session.dispose();
      disposal = drag.dispose();
      bridge.dispose(); registry.dispose(); windows.clear(); windowLoads.clear(); menuWindows.clear();
      return disposal;
    },
  };
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
  const permissionServices = new Map();
  let controls, appTray, projectAttention;
  const attentionWindows = new Map();
  desktopController = createDesktopController({
    onTopologyChanged: () => projectAttention?.refresh(),
    configureContents(contents) {
      if (!permissionServices.has(contents.session)) {
        permissionServices.set(contents.session, registerPermissions(contents.session, desktopController.bridge));
      }
    },
    configureWindow(native) {
      const producerAttention = require('./producer-attention.cjs').registerProducerAttention({
        window: native,
        appOwned: true,
        icon: nativeImage.createFromPath(path.join(__dirname, 'assets/notification.png')).resize({ width: 16, height: 16 }),
        description: () => currentMessages().producerWaiting,
      });
      attentionWindows.set(String(native.id), producerAttention);
      native.once('closed', () => { producerAttention.dispose?.(); attentionWindows.delete(String(native.id)); });
      controls.registerWindow(native, { tray: {
        hide: () => { if (!native.isDestroyed()) native.hide(); },
        setLocale: locale => appTray.setLocale?.(locale),
      }, producerAttention: {
        // App stream is authoritative; no renderer can clear another tab's badge.
        set() {}, refresh() { for (const attention of attentionWindows.values()) attention.refresh?.(); },
      } });
    },
  });
  const bridge = desktopController.bridge;
  projectAttention = require('./producer-attention.cjs').createProjectAttention({
    registry: desktopController.registry, getWindowAttention: id => attentionWindows.get(String(id)),
  });
  controls = registerWindowControls(ipcMain, bridge);
  const preferenceFile = path.join(app.getPath('userData'), 'notifications.json');
  const notifications = require('./project-notifications.cjs').createProjectNotifications({
    controller: desktopController, Notification, ipcMain,
    preference: {
      read() { try { return JSON.parse(fs.readFileSync(preferenceFile, 'utf8')).enabled === true; } catch { return false; } },
      write(enabled) {
        fs.writeFileSync(`${preferenceFile}.tmp`, JSON.stringify({ enabled }));
        fs.renameSync(`${preferenceFile}.tmp`, preferenceFile);
      },
    },
    locale: () => desktopLocale || (app.getLocale().startsWith('ru') ? 'ru' : 'en'),
    messages: () => localeMessages(desktopLocale || (app.getLocale().startsWith('ru') ? 'ru' : 'en')),
    onError(error, source, { activation }) {
      const detail = [currentMessages().notificationFailed, source?.project?.root].filter(Boolean).join('\n');
      logStartup('notification-failed', { root: source?.project?.root, error: error.message });
      // OS notification blocking/failure stays quiet, like the browser fallback.
      // A requested click with an unavailable root/page must fail explicitly.
      if (activation) dialog.showErrorBox(productName, detail);
    },
  });
  const projectEvents = require('./project-events.cjs').createProjectEventReceiver({
    WebSocket: globalThis.WebSocket, token: () => process.env.VOXARIUM_STUDIO_TOKEN,
    onNotification: notifications.notify,
    onAttention: values => projectAttention?.replace(values),
    onError: error => logStartup('desktop-events-failed', { error: error.message }),
  });
  // Electron 45 provides Node's WebSocket. Unit boot VMs have no transport and
  // remain inert; never introduce a renderer/window-lifetime event connection.
  if (typeof globalThis.WebSocket === 'function') projectEvents.start();
  // Tray/second-instance belong to the app, not any individual shell (§5.3.5).
  appTray = registerTray({ app, Tray, Menu,
    activate: () => desktopController.showWindow(),
    hideWindow: () => desktopController.getActiveWindow()?.hide(),
    onError: error => logStartup('window-activation-failed', { error: error.message }),
    icon: nativeImage.createFromPath(path.join(__dirname, 'assets/tray.png')), productName,
    messages: locale => { if (locale) desktopLocale = locale; return currentMessages(); },
  });
  const startup = require('./startup.cjs').registerStartup({ app, ipcMain, authorize: bridge.authorize });
  const broadcast = (channel, value) => {
    for (const contents of bridge.allContents()) {
      if (!contents.isDestroyed()) { try { contents.send(channel, value); } catch {} }
    }
  };
  let updates;
  if (app.isPackaged) {
    updates = require('./updates.cjs').registerUpdates({ ipcMain, authorize: bridge.authorize, broadcast,
      updater: require('electron-updater').autoUpdater, version: app.getVersion(), supported: true,
      configureFeed: () => configureUpdateFeed({ updater: require('electron-updater').autoUpdater,
        accessError: currentMessages().updateAccess }),
      installFailure: () => currentMessages().installFailed,
      install: commit => requestShutdown('update', () => installAndQuit(require('electron-updater').autoUpdater, commit)),
    });
  } else ipcMain.handle('studio:update', event => { bridge.authorize(event); return null; });
  let cleanupPending = false, cleanupComplete = false;
  app.on('will-quit', event => {
    if (cleanupComplete) return;
    // The prewarmed hook process must acknowledge shutdown before Electron exits.
    // This is app cleanup only, never a backend/tab lifecycle action (§5.3.4).
    event?.preventDefault();
    if (cleanupPending) return;
    cleanupPending = true;
    appTray.dispose();
    projectEvents.dispose(); notifications.dispose();
    updates?.dispose(); startup?.dispose(); controls.dispose();
    if (!app.isPackaged) ipcMain.removeHandler('studio:update');
    for (const service of permissionServices.values()) service.dispose();
    Promise.resolve(desktopController.dispose()).catch(error => {
      logStartup('desktop-cleanup-failed', { error: error.message });
    }).finally(() => { cleanupComplete = true; if (event) app.quit(); });
  });
  // Only a genuinely absent manifest uses the legacy selection as a first-run
  // seed. Restore/corruption/empty topology never fall back to that selection.
  const token = process.env.VOXARIUM_STUDIO_TOKEN;
  await desktopController.initializeSession(async native => {
    window = native;
    try {
      const selected = await fetch(`${ORIGIN}/api/project`, { redirect: 'error', cache: 'no-store',
        headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: AbortSignal.timeout(5000) });
      if (selected.ok) {
        const project = await selected.json();
        if (typeof project.root === 'string' && project.root) {
          await desktopController.openProject(project.root, String(window.id));
        }
      } else if (selected.status !== 401 && selected.status !== 403) {
        throw new Error(`Could not read initial project (${selected.status})`);
      }
    } catch (error) {
      // The shell is already usable. A missing/stale seed or failed ensure/view
      // is recoverable via its menu, not a fatal boot/backend shutdown (§5.3.4).
      logStartup('initial-project-failed', { error: error.stack || String(error) });
      bridge.setWindowError(String(window.id), error.message);
    }
  });
  window = desktopController.getActiveWindow();
  // An authenticated backend without a supplied credential remains an empty
  // shell; do not leak a token, bypass auth or fall back to the legacy SPA.
  logStartup('window-loaded');
  window?.show();
  if (splash && !splash.isDestroyed()) splash.close();
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  // electron-updater emits this immediately before its scheduled app.quit,
  // including after a failed NSIS spawn. Only our guarded handoff may exit.
  nativeUpdater?.on('before-quit-for-update', () => { updaterQuitPending = true; });
  // Zero windows is a resident tray/backend state, not an exit request (§5.3.5).
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (updaterQuitPending) { updaterQuitPending = false; event.preventDefault(); return; }
    if (quitting) return; // Only a successfully guarded commit can authorise teardown.
    event.preventDefault();
    void requestShutdown('quit', () => app.quit()).catch(error => {
      dialog.showErrorBox(productName, error.message);
    });
  });
  app.whenReady().then(boot).catch(async error => {
    logStartup('startup-failed', { error: error.stack || String(error) });
    dialog.showErrorBox(productName, `${messages?.startFailed || error.message}\n${error.message}\n${runDir || ''}`);
    try { await stopOwned(); }
    catch (stopError) {
      // Keep ownership/stop file and the app alive for a subsequent Quit retry,
      // rather than orphaning an owned backend or rejecting the boot catch.
      logStartup('startup-cleanup-failed', { error: stopError.stack || String(stopError) });
      dialog.showErrorBox(productName, `${currentMessages().stopFailed}\n${stopError.message}`);
      return;
    }
    app.exit(1);
  });
}
