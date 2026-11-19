const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { ORIGIN } = require('../policy.cjs');
const { TABS_CHANNELS } = require('../tabs-ipc.cjs');

// Exercise the actual boot path without spawning processes, opening windows or
// contacting the fixed server port. The desktop and backend must share a version (§2.7).
async function boot({ existing = null, packaged = true, studioVersion = packaged ? '5.2.0' : '5.0.0',
  startedVersion = studioVersion, releaseVersion = '5.2.0', staleBackendVersion,
  confirmationResponse = 1, project = { root: 'D:\\Games\\CurrentProject' }, projectOk = true,
  recheckedExisting, token, realServices = false, stopFailures = 0, backendSurvivesStop = false,
  projectStatus = 500, ensureOk = true, projectLoadFails = false, manifest, manifestError, missingRoots = [], epoch = 'epoch-1', onEnsure,
  projectEvents = false } = {}) {
  const calls = { launch: [], stop: [], errors: [], pages: [], exits: [], updates: [], diagnostics: [],
    confirmations: [], projects: [], ensured: [], windows: [], views: [], trays: [], permissionInstalls: 0, registrations: [],
    dragConstructors: [], dragWindows: [], dragOperations: [], dragDisposals: 0,
    closeAck: true, closeRequests: [], quits: 0, manifests: [], restoredPages: [], sessionReads: 0, timeline: [] };
  const instance = 'test-desktop-instance';
  const projectIds = new Map();
  calls.notifications = []; calls.sockets = []; calls.attention = new Map();
  class Notification extends EventEmitter {
    static isSupported() { return true; }
    constructor(options) { super(); this.options = options; calls.notifications.push(this); }
    show() {}
    close() { this.emit('close'); }
  }
  class WebSocket extends EventEmitter {
    constructor(url) { super(); this.url = url; calls.sockets.push(this); }
    close() { this.closed = true; }
    frame(value) { this.emit('message', JSON.stringify(value)); }
  }
  let launched = false, stopped = false, completion, healthChecks = 0;
  const app = new EventEmitter();
  Object.assign(app, {
    isPackaged: packaged,
    getPath: () => path.resolve('test-profile'), setPath() {},
    getLocale: () => 'en', getVersion: () => packaged ? releaseVersion : '5.0.0',
    setAppUserModelId() {}, requestSingleInstanceLock: () => true,
    quit() { calls.quits++; }, exit: code => calls.exits.push(code),
    whenReady: () => ({ then: callback => ({
      catch: onError => { completion = Promise.resolve().then(callback).catch(onError); },
    }) }),
  });
  calls.app = app;
  const handlers = new Map();
  const ipcMain = {
    handle(channel, handler) {
      assert.equal(handlers.has(channel), false, `duplicate IPC ${channel}`);
      handlers.set(channel, handler); calls.registrations.push(channel);
    },
    removeHandler: channel => handlers.delete(channel),
  };
  calls.invoke = (channel, contents, ...args) => handlers.get(channel)({ sender: contents, senderFrame: contents.mainFrame }, ...args);
  const session = { setPermissionRequestHandler(fn) { this.request = fn; calls.permissionInstalls++; },
    setPermissionCheckHandler(fn) { this.check = fn; } };
  class Contents extends EventEmitter {
    constructor() { super(); this.destroyed = false; this.mainFrame = { url: 'about:blank' }; this.session = session; this.sent = []; }
    isDestroyed() { return this.destroyed; }
    getURL() { return this.mainFrame.url; }
    setWindowOpenHandler(fn) { this.windowOpen = fn; }
    async loadURL(url) {
      calls.pages.push(url); this.mainFrame.url = url;
      this.emit('did-start-loading');
      if ((calls.projectLoadFails ?? projectLoadFails) && url.includes('/p/')) throw new Error('project renderer load failed');
      this.emit('did-finish-load');
    }
    async executeJavaScript(script) {
      assert.equal(script, 'true');
      if (calls.transferAck) await calls.transferAck();
      return true;
    }
    send(...args) {
      this.sent.push(args);
      if (args[0] === TABS_CHANNELS.stateChanged && args[1].role === 'project' && args[1].tab.state === 'ready') {
        const tab = args[1].tab;
        queueMicrotask(() => {
          if (!this.destroyed && handlers.has(TABS_CHANNELS.reportPage)) {
            try { calls.invoke(TABS_CHANNELS.reportPage, this, { tabId: tab.tabId, tabRevision: tab.revision,
              transferGeneration: tab.transferGeneration, page: this.page || { path: '/' } }); } catch {}
          }
        });
      }
      if (args[0] === TABS_CHANNELS.restorePage) {
        const { page, ...request } = args[1]; this.page = page; calls.restoredPages.push(page);
        queueMicrotask(() => calls.invoke(TABS_CHANNELS.pageRestoreAck, this, { ...request, status: 'restored' }));
      }
      if (args[0] === TABS_CHANNELS.closeRequest) {
        const request = args[1]; calls.closeRequests.push({ contents: this, request });
        if (request.phase !== 'release' && calls.closeAck !== null) queueMicrotask(() => calls.invoke(TABS_CHANNELS.closeAck, this,
          { ...request, prepared: calls.closeAck }));
      }
    }
    focus() { calls.focusedContents = this; }
    close() { if (this.destroyed) return; this.destroyed = true; this.emit('destroyed'); }
  }
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.id = calls.windows.length + 1;
      this.destroyed = false; this.contents = new Contents(); this.focused = false;
      this.children = new Set();
      const children = this.children;
      this.contentView = { get children() { return [...children]; },
        addChildView: view => children.add(view), removeChildView: view => children.delete(view) };
      calls.windows.push(this);
    }
    async loadFile() {}
    get webContents() {
      // Electron's native BrowserWindow is already gone in a 'closed' handler.
      if (this.destroyed) throw new TypeError('Object has been destroyed');
      return this.contents;
    }
    async loadURL(url) {
      if (calls.failNextShellLoad) { calls.failNextShellLoad = false; throw new Error('shell load failed'); }
      await this.webContents.loadURL(url);
    }
    show() { this.showCalls = (this.showCalls || 0) + 1; }
    isDestroyed() { return this.destroyed; }
    isMinimized() { return false; }
    isMaximized() { return false; }
    getNormalBounds() { return { x: this.options.x ?? 0, y: this.options.y ?? 0,
      width: this.options.width, height: this.options.height }; }
    maximize() { this.maximized = true; }
    isFocused() { return this.focused; }
    setOverlayIcon() { assert.equal(this.destroyed, false); }
    flashFrame() { assert.equal(this.destroyed, false); }
    focus() {
      for (const window of calls.windows) window.focused = false;
      this.focused = true; this.focusCalls = (this.focusCalls || 0) + 1; this.emit('focus');
    }
    minimize() { this.minimized = true; }
    hide() {}
    getContentSize() { return this.contentSize || [1600, 1000]; }
    destroy() { if (this.destroyed) return; this.destroyed = true; this.contents.close(); this.emit('closed'); }
    close() {
      let prevented = false;
      this.emit('close', { preventDefault() { prevented = true; } });
      if (!prevented) this.destroy();
    }
  }
  class WebContentsView {
    constructor(options) { this.options = options; this.webContents = new Contents(); calls.views.push(this); }
    setVisible(value) { this.visible = value; }
    setBounds(value) { this.bounds = value; }
  }
  class Tray extends EventEmitter {
    constructor() { super(); calls.trays.push(this); this.destroyed = false; }
    setToolTip() {}
    setContextMenu(menu) { this.menu = menu; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
  }
  const updater = Object.assign(new EventEmitter(), {
    checkForUpdates: async () => {}, downloadUpdate: async () => {}, quitAndInstall() {},
  });
  const image = { resize() { return this; } };
  // Main wiring only: never instantiate the real coordinator/native OS helper.
  const screen = Object.freeze({ screenToDipPoint: point => point,
    getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }] });
  calls.screen = screen;
  const metadata = { productName: 'Voxarium Studio', version: app.getVersion(),
    ...(packaged ? { studioVersion } : {}),
    ...(staleBackendVersion ? { backendVersion: staleBackendVersion } : {}) };
  const mocks = {
    electron: { app, BrowserWindow, WebContentsView, Tray, screen,
      dialog: {
        showErrorBox: (_title, text) => calls.errors.push(text),
        showMessageBox: async (_window, options) => {
          calls.confirmations.push({ ...options, projectReads: calls.projects.length });
          return { response: confirmationResponse };
        },
      },
      Notification, shell: { openExternal: async () => {} }, Menu: { setApplicationMenu() {}, buildFromTemplate: value => value }, ipcMain,
      nativeImage: { createFromPath: () => image },
    },
    'node:fs': { mkdirSync() {}, openSync: () => 7, closeSync() {}, writeFileSync() {}, renameSync() {},
      appendFileSync: (file, text) => {
        assert.equal(path.basename(file), 'desktop.log');
        calls.diagnostics.push(JSON.parse(text));
      },
    },
    'node:crypto': { randomUUID: () => instance },
    'node:child_process': {
      spawn: (...args) => {
        launched = true;
        calls.launch.push(args);
        return Object.assign(new EventEmitter(), { unref() {} });
      },
      execFile: (...args) => {
        calls.timeline.push('stop');
        calls.stop.push(args.slice(0, -1));
        if (stopFailures-- > 0) args.at(-1)(new Error('stop failed'));
        else { stopped = !backendSurvivesStop; args.at(-1)(null); }
      },
    },
    './package.json': metadata,
    './scripts/release.cjs': { sourceReleaseInfo: () => ({ version: studioVersion }) },
    './window-controls.cjs': { registerWindowControls: () => ({ registerWindow() {}, dispose() {} }) },
    './tray.cjs': { registerTray: () => ({ dispose() {} }) },
    './producer-attention.cjs': { ...require('../producer-attention.cjs'), registerProducerAttention(options) {
      const attention = require('../producer-attention.cjs').registerProducerAttention({ ...options, platform: 'win32' });
      const id = String(options.window.id);
      return { ...attention, set(value) { calls.attention.set(id, value); attention.set(value); } };
    } },
    './startup.cjs': { registerStartup() {} },
    './update-feed.cjs': { configureUpdateFeed() {} },
    './updates.cjs': { registerUpdates: options => { calls.updates.push(options); return { dispose() {} }; } },
    './tab-drag.cjs': { createTabDrag(options) {
      calls.dragConstructors.push(options);
      return {
        registerWindow(native) { calls.dragWindows.push(native); return () => {}; },
        ...Object.fromEntries(['begin', 'cancel', 'setGeometry'].map(method => [method,
          (event, request) => {
            calls.dragOperations.push({ method, event, request });
            return `${method}-wired`;
          }])),
        dispose() { calls.dragDisposals++; return Promise.resolve(); },
      };
    } },
    'electron-updater': { autoUpdater: updater },
  };
  const tabs = require('../tabs.cjs');
  mocks['./tabs.cjs'] = { ...tabs, canonicalProjectRoot: async root => root, DesktopTabs: class extends tabs.DesktopTabs {
    constructor(options) { super({ ...options, canonicalizeRoot: async root => root }); }
  } };
  if (realServices) {
    delete mocks['./window-controls.cjs']; delete mocks['./startup.cjs']; delete mocks['./updates.cjs'];
    delete mocks['./tray.cjs'];
  }
  app.getLoginItemSettings = () => ({ openAtLogin: false });
  app.setLoginItemSettings = value => { calls.loginSettings = value; };
  app.getAppPath = () => 'app';
  const desktop = path.resolve(__dirname, '..');
  const context = {
    __dirname: desktop,
    WebSocket: projectEvents ? WebSocket : undefined,
    require: name => {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      if (name.includes('/locales/')) return require('../../frontend/src/locales/en.json');
      return require(name.startsWith('.') ? path.resolve(desktop, name) : name);
    },
    process: { env: token ? { VOXARIUM_STUDIO_TOKEN: token } : {}, pid: 1234, resourcesPath: path.resolve('test-resources') },
    fetch: async (url, options = {}) => {
      if (url === `${ORIGIN}/api/desktop/session`) {
        if (options.method === 'GET') {
          calls.sessionReads++;
          if (manifestError) return { ok: false, status: 409, json: async () => ({ detail: { code: manifestError } }) };
          if (!manifest) return { ok: false, status: 404, json: async () => ({ detail: { code: 'desktop_session_missing' } }) };
          return { ok: true, status: 200, json: async () => manifest };
        }
        if (calls.sessionSaveError) return { ok: false, status: 500,
          json: async () => ({ detail: { code: 'desktop_session_write_failed' } }) };
        const value = JSON.parse(options.body); calls.manifests.push(value); calls.timeline.push('save');
        return { ok: true, status: 200, json: async () => value };
      }
      if (url === `${ORIGIN}/api/project`) {
        calls.projects.push(url);
        return { ok: projectOk, status: projectStatus, json: async () => project };
      }
      if (url === `${ORIGIN}/api/projects/open`) {
        const request = JSON.parse(options.body); calls.ensured.push({ ...request, options });
        await onEnsure?.(request, context.getController(), calls);
        const ok = !missingRoots.includes(request.path) && (calls.ensureOk ?? ensureOk);
        if (ok && !projectIds.has(request.path)) projectIds.set(request.path, 'project-' + (projectIds.size + 1));
        return { ok, status: ok ? 200 : 404, json: async () => ({ root: request.path,
          project_id: projectIds.get(request.path), epoch }) };
      }
      if (url === `${ORIGIN}/api/projects/activity`) return { ok: true, json: async () => ({
        status: 'idle', requires_confirmation: false, errors: [], projects: [],
      }) };
      assert.equal(url, `${ORIGIN}/healthz`);
      healthChecks++;
      const health = stopped ? null : launched
        ? { service: 'voxarium_studio', status: 'ok', version: startedVersion, desktop_instance: instance }
        : healthChecks > 1 && recheckedExisting !== undefined ? recheckedExisting : existing;
      return { ok: !!health, json: async () => health };
    },
    AbortSignal, setTimeout, clearTimeout, console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(desktop, 'main.cjs'), 'utf8') +
    '\nglobalThis.getController = () => desktopController; globalThis.getShellURL = () => SHELL_URL;', context, { filename: 'main.cjs' });
  await completion;
  calls.controller = context.getController(); calls.shellURL = context.getShellURL();
  calls.invoke = (channel, contents, ...args) => handlers.get(channel)({ sender: contents, senderFrame: contents.mainFrame }, ...args);
  calls.handlers = handlers; calls.session = session; calls.app = app; calls.updater = updater;
  return calls;
}

test('tagged desktop starts only its matching backend and uses the same version for updates', async () => {
  const calls = await boot();
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.equal(calls.launch.length, 1);
  assert.equal(calls.stop.length, 0);
  assert.deepEqual(calls.exits, []);
  assert.equal(calls.updates[0].version, '5.2.0');
  assert.deepEqual(calls.diagnostics.map(entry => entry.event), ['backend-check', 'backend-started', 'window-loaded']);
  assert.equal(calls.diagnostics[0].expectedVersion, '5.2.0');
  assert.equal(calls.diagnostics[1].actualVersion, '5.2.0');
});

test('tagged desktop attaches to a compatible independent backend without taking ownership', async () => {
  const calls = await boot({ existing: { service: 'voxarium_studio', status: 'ok', version: '5.2.0' } });
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.equal(calls.launch.length, 0);
  assert.equal(calls.stop.length, 0);
  assert.equal(calls.projects.length, 1);
  assert.equal(calls.confirmations.length, 0);
});

test('a genuinely incompatible owned backend still fails startup and is stopped', async () => {
  const calls = await boot({ startedVersion: '5.0.0' });
  assert.equal(calls.errors.length, 1);
  assert.match(calls.errors[0], /The backend version could not be verified/);
  assert.deepEqual(calls.pages, []);
  assert.equal(calls.launch.length, 1);
  assert.equal(calls.stop.length, 1);
  const check = calls.diagnostics.find(entry => entry.event === 'backend-check');
  assert.equal(check.releaseVersion, '5.2.0');
  assert.equal(check.expectedVersion, '5.2.0');
  const started = calls.diagnostics.find(entry => entry.event === 'backend-started');
  assert.equal(started.expectedVersion, '5.2.0');
  assert.equal(started.actualVersion, '5.0.0');
  const failure = calls.diagnostics.find(entry => entry.event === 'startup-failed');
  assert.match(failure.error, /The backend version could not be verified/);
  assert.match(failure.error, /at startBackend/);
  const stopArgs = calls.stop[0][1];
  assert.match(stopArgs[stopArgs.indexOf('-StopFile') + 1], /test-desktop-instance\.stop$/);
  assert.deepEqual(calls.exits, [1]);
});

test('stale backend compatibility metadata cannot bypass strict desktop version equality', async () => {
  const calls = await boot({ startedVersion: '5.0.0', staleBackendVersion: '5.0.0' });
  assert.equal(calls.errors.length, 1);
  assert.match(calls.errors[0], /The backend version could not be verified/);
  assert.deepEqual(calls.pages, []);
  assert.equal(calls.stop.length, 1);
  assert.deepEqual(calls.exits, [1]);
});

test('checkout startup still requires the matching package version', async () => {
  const calls = await boot({ packaged: false });
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.equal(calls.launch.length, 1);
  assert.equal(calls.stop.length, 0);
});

test('untagged desktop starts its exact commit hash while updater retains a numeric package version', async () => {
  const calls = await boot({ studioVersion: 'abcdef0', releaseVersion: '5.0.0' });
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.equal(calls.diagnostics[0].expectedVersion, 'abcdef0');
  assert.equal(calls.diagnostics[1].actualVersion, 'abcdef0');
  assert.equal(calls.updates[0].version, '5.0.0');
});

test('untagged desktop rejects a backend from another commit even with equal numeric package versions', async () => {
  const calls = await boot({ studioVersion: 'abcdef0', startedVersion: '1234567', releaseVersion: '5.0.0' });
  assert.equal(calls.errors.length, 1);
  assert.match(calls.errors[0], /The backend version could not be verified/);
  assert.deepEqual(calls.pages, []);
  assert.equal(calls.stop.length, 1);
  assert.deepEqual(calls.exits, [1]);
});

test('checkout startup uses the same untagged source commit as the backend', async () => {
  const calls = await boot({ packaged: false, studioVersion: 'abcdef0' });
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.equal(calls.diagnostics[0].expectedVersion, 'abcdef0');
});

const independentCommit = { service: 'voxarium_studio', status: 'ok', version: '0ebc2cf', desktop_instance: null };

test('installed commit switches an independent backend only after confirmation and preserves its current project', async () => {
  const calls = await boot({ existing: independentCommit, studioVersion: '3d9da6e', releaseVersion: '5.0.0' });
  assert.deepEqual(calls.errors, []);
  assert.equal(calls.confirmations.length, 1);
  const confirmation = calls.confirmations[0];
  assert.match(confirmation.message, /0ebc2cf/);
  assert.match(confirmation.message, /3d9da6e/);
  assert.match(confirmation.message, /tasks and pipelines will be stopped/);
  assert.equal(confirmation.defaultId, 0);
  assert.equal(confirmation.cancelId, 0);
  assert.equal(confirmation.projectReads, 0, 'read the current project after the user confirms');
  assert.equal(calls.projects.length, 2);
  assert.equal(calls.launch.length, 1);
  const args = calls.launch[0][1];
  assert.equal(args[args.indexOf('-Project') + 1], 'D:\\Games\\CurrentProject');
  assert.equal(args.includes('-Desktop'), false);
  assert.ok(args.includes(path.join(path.resolve('test-resources'), 'runtime', 'run.ps1')));
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
  assert.deepEqual(calls.exits, []);
});

test('cancelling a commit switch leaves the independent backend untouched and exits without an error', async () => {
  const calls = await boot({ existing: independentCommit, studioVersion: '3d9da6e', confirmationResponse: 0 });
  assert.equal(calls.confirmations.length, 1);
  assert.deepEqual(calls.projects, []);
  assert.deepEqual(calls.launch, []);
  assert.deepEqual(calls.stop, []);
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, []);
  assert.deepEqual(calls.exits, [0]);
  assert.equal(calls.diagnostics.at(-1).event, 'backend-switch-cancelled');
});

test('matching commits attach without switch prompt; only shell seed reads the project', async () => {
  const calls = await boot({ existing: independentCommit, studioVersion: independentCommit.version });
  assert.deepEqual(calls.confirmations, []);
  assert.equal(calls.projects.length, 1);
  assert.deepEqual(calls.launch, []);
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [calls.shellURL, `${ORIGIN}/p/project-1/`]);
});

test('a backend changed during switch confirmation is never replaced', async () => {
  for (const changed of [{ ...independentCommit, version: 'abcdef0' },
    { ...independentCommit, desktop_instance: 'other-desktop' }, { ...independentCommit, service: 'other' }]) {
    const calls = await boot({ existing: independentCommit, studioVersion: '3d9da6e', recheckedExisting: changed });
    assert.equal(calls.confirmations.length, 1);
    assert.deepEqual(calls.launch, []);
    assert.deepEqual(calls.stop, []);
    assert.match(calls.errors[0], /backend changed/i);
    assert.deepEqual(calls.exits, [1]);
  }
});

test('a confirmed commit switch still requires access to the current absolute project path', async () => {
  for (const options of [{ projectOk: false }, { project: { root: 'relative' } }]) {
    const calls = await boot({ existing: independentCommit, studioVersion: '3d9da6e', ...options });
    assert.equal(calls.confirmations.length, 1);
    assert.deepEqual(calls.launch, []);
    assert.deepEqual(calls.stop, []);
    assert.match(calls.errors[0], /current project/i);
    assert.deepEqual(calls.exits, [1]);
  }
});

test('real app services survive one of two windows; open/focus never launch a backend', async t => {
  const f = await boot({ realServices: true, token: 'private-test-token' });
  t.after(() => f.app.emit('will-quit'));
  assert.deepEqual(f.errors, []);
  assert.equal(f.launch.length, 1);
  assert.equal(f.trays.length, 1);
  assert.equal(f.permissionInstalls, 1);
  const controller = f.controller, registry = controller.registry;
  const first = controller.getActiveWindow(), a = f.views[0].webContents;
  const second = await controller.createWindow();
  const shell = second.webContents;
  const event = contents => ({ sender: contents, senderFrame: contents.mainFrame });
  assert.equal(f.invoke('studio:tabs-bootstrap', shell).role, 'shell');
  let w2 = registry.getWindow(String(second.id));
  const bTab = await f.invoke('studio:tabs-open', shell, {
    windowId: w2.windowId, windowRevision: w2.revision, root: 'D:\\Games\\SecondProject', create: false,
  });
  const b = f.views[1].webContents;
  assert.equal(f.views.length, 2);
  assert.equal(f.permissionInstalls, 1, 'shared session permissions installed once');
  assert.equal(f.trays.length, 1, 'one app tray, not one tray per window');
  assert.equal(f.invoke('studio:tabs-bootstrap', b).binding.project_id, bTab.project.project_id);
  assert.equal(JSON.stringify(f.invoke('studio:tabs-bootstrap', b)).includes('private-test-token'), false);
  for (const request of f.ensured) {
    assert.equal(request.options.headers.Authorization, 'Bearer private-test-token');
    assert.equal(request.options.redirect, 'error');
    assert.equal(request.create, false);
  }
  // Reopening the known project from W2 focuses W1, never duplicates/reloads it.
  w2 = registry.getWindow(String(second.id));
  await f.invoke('studio:tabs-open', shell, {
    windowId: w2.windowId, windowRevision: w2.revision, root: 'D:\\Games\\CurrentProject',
  });
  assert.equal(f.views.length, 2);
  assert.equal(f.ensured.length, 2);
  w2 = registry.getWindow(String(second.id));
  f.invoke('studio:tabs-activate', shell, {
    windowId: w2.windowId, windowRevision: w2.revision, tabId: bTab.tabId, tabRevision: bTab.revision,
  });
  const permission = (contents, isMainFrame = true) => f.session.check(contents,
    'clipboard-sanitized-write', ORIGIN, { isMainFrame });
  assert.equal(permission(a), true);
  assert.equal(permission(b), true);
  assert.equal(permission(b, false), false);
  assert.equal(permission({ getURL: () => ORIGIN, mainFrame: { url: ORIGIN }, isDestroyed: () => false }), false);
  assert.throws(() => controller.bridge.authorize({ ...event(b), senderFrame: { url: b.getURL() } }), /Untrusted/);
  const foreignProjectURL = b.mainFrame.url;
  b.mainFrame.url = `${ORIGIN}/p/project-1/`;
  assert.throws(() => f.invoke('studio:startup', b), /Untrusted/);
  b.mainFrame.url = foreignProjectURL;
  f.updater.emit('update-available', { version: '6.0.0' });
  assert.equal(b.sent.some(([channel]) => channel === 'studio:update-state'), true);
  f.invoke('studio:window', first.webContents, 'close');
  await controller.requestCloseWindow(String(first.id));
  assert.equal(first.isDestroyed(), true);
  assert.equal(a.isDestroyed(), true);
  assert.equal(permission(a), false);
  assert.equal(permission(b), true);
  assert.throws(() => f.invoke('studio:tabs-bootstrap', a), /Untrusted/);
  f.invoke('studio:window', b, 'minimize');
  assert.equal(second.minimized, true);
  assert.equal(f.invoke('studio:startup', shell).supported, true);
  f.invoke('studio:startup', b, true);
  assert.equal(f.loginSettings.openAtLogin, true);
  assert.equal((await f.invoke('studio:update', b, 'state')).available, true);
  assert.equal(f.updater.listenerCount('update-available'), 1);
  assert.equal(f.trays[0].isDestroyed(), false);
  f.app.emit('second-instance');
  await controller.showWindow();
  assert.equal(second.isFocused(), true);
  assert.equal(f.launch.length, 1);
  assert.equal(f.stop.length, 0);
  assert.equal(f.projects.length, 1, 'legacy selection read only once as seed');
  assert.equal(f.pages.includes(ORIGIN), false, 'shell never loads the legacy SPA');
  assert.equal(new Set(f.registrations).size, f.registrations.length);
  for (const native of [first, second]) {
    assert.equal(native.options.webPreferences.contextIsolation, true);
    assert.equal(native.options.webPreferences.sandbox, true);
    assert.equal(native.options.webPreferences.nodeIntegration, false);
  }
  f.app.emit('will-quit');
  assert.equal(f.handlers.size, 0);
  assert.equal(f.updater.listenerCount('update-available'), 0);
  assert.equal(f.trays[0].isDestroyed(), true);
  assert.equal(b.isDestroyed(), true);
});

test('shell and project native navigation remain role-scoped', async () => {
  const f = await boot();
  const shell = f.controller.getActiveWindow().webContents;
  const project = f.views[0].webContents;
  for (const [contents, urls] of [[shell, [ORIGIN, 'https://example.com', `${f.shellURL}evil`]],
    [project, [ORIGIN, `${ORIGIN}/p/foreign/`, 'https://example.com']]]) {
    for (const channel of ['will-navigate', 'will-redirect']) {
      for (const url of urls) {
        let prevented = false;
        contents.emit(channel, { preventDefault() { prevented = true; } }, url);
        assert.equal(prevented, true, `${channel} ${url}`);
      }
    }
    assert.equal(contents.windowOpen({ url: 'javascript:alert(1)' }).action, 'deny');
  }
  f.app.emit('will-quit');
});

test('updater verifies owned stop completion and keeps failed shutdown retryable', async () => {
  const f = await boot({ backendSurvivesStop: true });
  for (let attempt = 1; attempt <= 2; attempt++) {
    await assert.rejects(f.updates[0].install(() => f.app.emit('will-quit')), /could not be stopped/i);
    assert.equal(f.stop.length, attempt);
    assert.deepEqual(f.exits, []);
  }
  f.app.emit('will-quit');
  const independent = await boot({ existing: independentCommit, studioVersion: independentCommit.version });
  assert.equal(await independent.updates[0].install(() => independent.app.emit('will-quit')), true);
  assert.equal(independent.stop.length, 0, 'updating never stops an independent backend');
  independent.app.emit('will-quit');
});

test('owned updater stops its backend once before permitting installation', async () => {
  const f = await boot();
  assert.equal(await f.updates[0].install(() => f.app.emit('will-quit')), true);
  assert.equal(f.stop.length, 1);
  assert.equal(await f.updates[0].install(() => assert.fail('duplicate install')), false);
  assert.equal(f.stop.length, 1);
  f.app.emit('will-quit');
});

test('fatal startup cleanup failure is handled and ownership remains retryable through Quit', async () => {
  const f = await boot({ startedVersion: 'wrong-version', stopFailures: 1 });
  assert.equal(f.errors.length, 2);
  assert.match(f.errors[1], /stop failed/);
  assert.deepEqual(f.exits, []);
  assert.equal(f.stop.length, 1);
  assert.equal(f.diagnostics.at(-1).event, 'startup-cleanup-failed');
  let prevented = false;
  f.app.emit('before-quit', { preventDefault() { prevented = true; } });
  await new Promise(setImmediate);
  assert.equal(prevented, true);
  assert.equal(f.stop.length, 2);
  f.app.emit('will-quit');
});

test('unauthorized initial seed fails closed without project views or legacy fallback', async () => {
  for (const status of [401, 403]) {
    const f = await boot({ projectOk: false, projectStatus: status });
    assert.deepEqual(f.errors, []);
    assert.deepEqual(f.ensured, []);
    assert.equal(f.views.length, 0);
    assert.deepEqual(f.pages, [f.shellURL]);
    assert.equal(f.controller.registry.snapshot().tabs.length, 0);
    f.app.emit('will-quit');
  }
});

test('failed second shell cleans only its registry and native listeners, leaving app services alive', async t => {
  const f = await boot({ realServices: true });
  t.after(() => f.app.emit('will-quit'));
  const incumbent = f.controller.getActiveWindow();
  const before = f.controller.registry.snapshot();
  f.failNextShellLoad = true;
  await assert.rejects(f.controller.createWindow(), /shell load failed/);
  const failed = f.windows.at(-1);
  const failedContents = failed.contents; // Fixture-only access after native destruction.
  assert.equal(failed.isDestroyed(), true);
  assert.equal(failed.listenerCount('resize'), 0);
  assert.equal(failedContents.listenerCount('will-navigate'), 0);
  assert.equal(failedContents.listenerCount('will-redirect'), 0);
  assert.deepEqual(f.controller.registry.snapshot(), before);
  assert.equal(f.controller.getWindow(failed.id), undefined);
  assert.throws(() => f.invoke('studio:tabs-bootstrap', failedContents), /Untrusted/);
  assert.equal(f.invoke('studio:tabs-bootstrap', incumbent.webContents).role, 'shell');
  assert.equal(f.invoke('studio:startup', f.views[0].webContents).supported, true);
  assert.equal((await f.invoke('studio:update', f.views[0].webContents, 'state')).supported, true);
  assert.equal(f.permissionInstalls, 1);
  assert.equal(f.launch.length, 1);
});

test('shell owns title/strip hit zones and menu reserves the entire surface without reloading A/B', async t => {
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  assert.equal(f.shellURL, `${ORIGIN}/?desktopShell=1`);
  const native = f.controller.getActiveWindow(), shell = native.webContents;
  const registry = f.controller.registry, id = String(native.id);
  const a = registry.snapshot().tabs[0], aView = f.views[0];
  const b = await f.controller.openProject('D:\\Games\\B', id), bView = f.views[1];
  const pages = [...f.pages], ensured = f.ensured.length, aBinding = a.project, bBinding = b.project;
  const windowRequest = () => { const w = registry.getWindow(id); return { windowId: id, windowRevision: w.revision }; };
  const activate = tabId => f.invoke('studio:tabs-activate', shell, { ...windowRequest(),
    tabId, tabRevision: registry.getTab(tabId).revision });
  assert.deepEqual(bView.bounds, { x: 0, y: 44, width: 1600, height: 956 });
  assert.equal(bView.visible, true); assert.equal(aView.visible, false);
  // Native hit rectangle starts below the shared 44px title bar.
  for (const y of [0, 22, 43]) assert.equal(y >= bView.bounds.y, false);
  activate(a.tabId);
  assert.equal(aView.visible, true); assert.equal(bView.visible, false);
  f.invoke('studio:tabs-set-menu-open', shell, { ...windowRequest(), open: true });
  assert.deepEqual(aView.bounds, { x: 0, y: 1000, width: 1600, height: 0 });
  assert.equal(f.focusedContents, shell, 'menu has keyboard focus, not the zero-height native view');
  native.contentSize = [1200, 700]; native.emit('resize');
  assert.deepEqual(aView.bounds, { x: 0, y: 700, width: 1200, height: 0 });
  activate(b.tabId);
  assert.deepEqual(bView.bounds, { x: 0, y: 700, width: 1200, height: 0 });
  assert.equal(f.focusedContents, shell, 'activating behind an open menu cannot steal input');
  f.invoke('studio:tabs-set-menu-open', shell, { ...windowRequest(), open: false });
  assert.deepEqual(bView.bounds, { x: 0, y: 44, width: 1200, height: 656 });
  assert.equal(f.focusedContents, bView.webContents);
  for (const tab of [a, b, a, b]) {
    activate(tab.tabId);
    assert.equal(f.focusedContents, shell, 'repeated strip activation retains the native shell input target');
  }
  assert.deepEqual(f.pages, pages, 'activation/menu never call loadURL or reload');
  assert.equal(f.views.length, 2); assert.equal(f.ensured.length, ensured);
  assert.equal(registry.getTab(a.tabId).project, aBinding); assert.equal(registry.getTab(b.tabId).project, bBinding);
  assert.equal(f.invoke('studio:tabs-bootstrap', aView.webContents).binding.project_id, aBinding.project_id);
  assert.equal(f.invoke('studio:tabs-bootstrap', bView.webContents).binding.project_id, bBinding.project_id);
  assert.equal(f.launch.length, 1); assert.equal(f.projects.length, 1); assert.equal(f.stop.length, 0);
});

test('cross-window deduplicated open closes source menu without stealing target focus', async t => {
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  const source = f.controller.getActiveWindow(), shell = source.webContents;
  const registry = f.controller.registry, sourceId = String(source.id);
  const a = registry.snapshot().tabs[0], aView = f.views[0];
  const target = await f.controller.createWindow(), targetId = String(target.id);
  const root = 'D:\\Games\\Target';
  const b = await f.controller.openProject(root, targetId), bView = f.views[1];
  source.focus(); shell.focus();
  const request = () => ({ windowId: sourceId, windowRevision: registry.getWindow(sourceId).revision });
  f.invoke('studio:tabs-set-menu-open', shell, { ...request(), open: true });
  assert.equal(aView.bounds.height, 0);
  const pages = [...f.pages], ensured = f.ensured.length;
  const opened = await f.invoke('studio:tabs-open', shell, { ...request(), root });
  assert.equal(opened.tabId, b.tabId); assert.equal(opened.ownerWindowId, targetId);
  assert.equal(target.isFocused(), true); assert.equal(source.isFocused(), false);
  assert.equal(f.focusedContents, bView.webContents);
  const sourceShows = source.showCalls, sourceFocuses = source.focusCalls;
  // The source UI automatically closes its menu after the target was focused.
  f.invoke('studio:tabs-set-menu-open', shell, { ...request(), open: false });
  assert.deepEqual(aView.bounds, { x: 0, y: 44, width: 1600, height: 956 });
  assert.equal(source.showCalls, sourceShows, 'automatic menu close must not show the source window');
  assert.equal(source.focusCalls, sourceFocuses, 'automatic menu close must not focus the source window');
  assert.equal(f.focusedContents, bView.webContents, 'target project keeps the native input focus');
  assert.equal(f.controller.getActiveWindow(), target);
  assert.equal(registry.getWindow(sourceId).activeTabId, a.tabId);
  assert.equal(registry.getWindow(targetId).activeTabId, b.tabId);
  assert.equal(aView.visible, true); assert.equal(bView.visible, true);
  assert.deepEqual(f.pages, pages); assert.equal(f.ensured.length, ensured); assert.equal(f.views.length, 2);
  assert.equal(f.launch.length, 1); assert.equal(f.projects.length, 1); assert.equal(f.stop.length, 0);
});

test('native loading/crash/destruction events publish shell state after registry transitions', async t => {
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  const native = f.controller.getActiveWindow(), shell = native.webContents;
  const tab = f.controller.registry.snapshot().tabs[0], view = f.views[0];
  const states = () => shell.sent.filter(([channel]) => channel === 'studio:tabs-state-changed').map(([, state]) => state);
  assert.equal(states().some(state => state.tabs.some(item => item.state === 'loading')), true);
  assert.equal(states().at(-1).tabs[0].state, 'ready');
  view.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  await new Promise(setImmediate);
  assert.equal(states().at(-1).tabs[0].state, 'crashed');
  assert.equal(states().at(-1).tabs[0].revision, tab.revision + 1);
  assert.equal(view.bounds.height, 0, 'crashed native surface does not cover shell recovery UI');
  assert.equal(f.focusedContents, shell);
  view.webContents.close(); await new Promise(setImmediate);
  assert.deepEqual(states().at(-1).tabs, []);
  assert.equal(states().at(-1).window.activeTabId, null);
  assert.equal(f.launch.length, 1); assert.equal(f.stop.length, 0);
});

test('empty and failed initial seeds keep a recoverable shell and owned backend alive', async t => {
  for (const options of [{ project: {} }, { projectOk: false, projectStatus: 500 },
    { ensureOk: false }, { projectLoadFails: true }]) {
    const f = await boot(options); t.after(() => f.app.emit('will-quit'));
    const shell = f.controller.getActiveWindow().webContents;
    const state = f.invoke('studio:tabs-bootstrap', shell);
    assert.equal(state.role, 'shell'); assert.deepEqual(state.tabs, []);
    if (options.project) assert.equal(state.error, undefined);
    else assert.equal(typeof state.error, 'string');
    assert.deepEqual(f.errors, []); assert.deepEqual(f.exits, []); assert.deepEqual(f.stop, []);
    assert.equal(f.launch.length, 1); assert.equal(f.windows[0].isDestroyed(), true, 'splash is dismissed');
    f.ensureOk = true; f.projectLoadFails = false;
    await f.invoke('studio:tabs-open', shell, { windowId: state.window.windowId,
      windowRevision: state.window.revision, root: 'D:\\Games\\Recovery' });
    const recovered = f.invoke('studio:tabs-state', shell);
    assert.equal(recovered.tabs.length, 1); assert.equal(recovered.error, undefined);
    assert.equal(f.launch.length, 1); assert.equal(f.projects.length, 1);
  }
});

test('production controller detach/return uses its hidden shell factory without reopening a project or backend', async t => {
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  const registry = f.controller.registry, source = f.controller.getActiveWindow();
  const original = registry.snapshot().tabs[0], view = f.views[0], contents = view.webContents;
  const sourceShell = source.webContents;
  const request = () => {
    const tab = registry.getTab(original.tabId), window = registry.getWindow(tab.ownerWindowId);
    return { windowId: window.windowId, windowRevision: window.revision, tabId: tab.tabId, tabRevision: tab.revision };
  };
  const initialWindows = f.windows.length, projectPages = f.pages.filter(url => url.includes('/p/'));
  const detached = await f.invoke('studio:tabs-detach', sourceShell, request());
  const target = f.controller.getWindow(detached.ownerWindowId);
  const targetShell = target.webContents;
  assert.equal(f.windows.length, initialWindows + 1);
  assert.equal(target.showCalls, 1, 'prepared hidden shell is shown only by successful commit');
  assert.equal(target.children.has(view), true); assert.equal(source.children.has(view), false);
  assert.equal(f.focusedContents, contents);
  assert.equal(registry.getWindow(String(source.id)), null);
  assert.equal(source.isDestroyed(), true, 'last-tab transfer uses empty-shell cleanup');
  assert.throws(() => source.webContents, /Object has been destroyed/);
  for (const event of ['will-navigate', 'will-redirect']) assert.equal(sourceShell.listenerCount(event), 0);
  for (const event of ['resize', 'move', 'focus', 'maximize', 'unmaximize', 'close']) {
    assert.equal(source.listenerCount(event), 0);
  }
  const returnTarget = await f.controller.createWindow();
  const { token } = f.invoke('studio:tabs-transfer-begin', target.webContents, request());
  const returned = await f.invoke('studio:tabs-transfer', target.webContents, { ...request(), token,
    targetWindowId: String(returnTarget.id), targetWindowRevision: registry.getWindow(String(returnTarget.id)).revision });
  assert.equal(returned.ownerWindowId, String(returnTarget.id)); assert.equal(returnTarget.children.has(view), true);
  assert.equal(target.isDestroyed(), true, 'return also closes only the emptied source');
  for (const event of ['will-navigate', 'will-redirect']) assert.equal(targetShell.listenerCount(event), 0);
  assert.equal(f.views[0], view); assert.equal(view.webContents, contents); assert.equal(contents.isDestroyed(), false);
  assert.deepEqual(f.pages.filter(url => url.includes('/p/')), projectPages);
  assert.equal(f.views.length, 1); assert.equal(f.ensured.length, 1); assert.equal(f.projects.length, 1);
  assert.equal(f.launch.length, 1); assert.equal(f.stop.length, 0);
});

test('main wires one native drag coordinator to resident registry, IPC and initial/detached shells', async t => {
  // Protocol/wiring proof only, not native OS validation. The fixture replaces
  // createTabDrag so no helper process, server or native gesture can run.
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  assert.deepEqual(f.errors, []);
  assert.equal(f.dragConstructors.length, 1);
  const options = f.dragConstructors[0], controller = f.controller, registry = controller.registry;
  const source = controller.getActiveWindow(), shell = source.webContents;
  const original = registry.snapshot().tabs[0], view = f.views[0];
  const event = { sender: shell, senderFrame: shell.mainFrame };
  assert.equal(options.registry, registry);
  assert.equal(options.screen, f.screen);
  assert.equal(typeof options.log, 'function');
  assert.deepEqual([...options.getWindows()], [source], 'splash is not a drag shell');
  assert.deepEqual(f.dragWindows, [source]);
  const windowRequest = () => ({ windowId: String(source.id),
    windowRevision: registry.getWindow(String(source.id)).revision });
  const tabRequest = () => ({ ...windowRequest(), tabId: original.tabId,
    tabRevision: registry.getTab(original.tabId).revision });
  const request = tabRequest();
  const authorized = options.authorizeSource(event, request);
  assert.equal(authorized.window, source); assert.equal(authorized.contents, shell);
  assert.throws(() => options.authorizeSource({ sender: shell, senderFrame: { url: shell.getURL() } }, request));
  assert.throws(() => options.authorizeSource(event, { ...request, windowRevision: request.windowRevision + 1 }));
  const project = view.webContents;
  assert.throws(() => options.authorizeSource({ sender: project, senderFrame: project.mainFrame }, request));

  assert.equal(options.isMenuOpen(source.id), false);
  f.invoke('studio:tabs-set-menu-open', shell, { ...windowRequest(), open: true });
  assert.equal(options.isMenuOpen(source.id), true);
  assert.equal(options.isMenuOpen(String(source.id)), true);
  f.invoke('studio:tabs-set-menu-open', shell, { ...windowRequest(), open: false });
  assert.equal(options.isMenuOpen(source.id), false);
  const stateCount = shell.sent.filter(([channel]) => channel === 'studio:tabs-state-changed').length;
  options.publishState();
  assert.equal(shell.sent.filter(([channel]) => channel === 'studio:tabs-state-changed').length, stateCount + 1);

  const pages = [...f.pages], launches = f.launch.length, ensured = f.ensured.length;
  const projects = f.projects.length, views = f.views.length, windows = f.windows.length;
  for (const [channel, method, payload] of [
    ['studio:tabs-drag-begin', 'begin', { ...tabRequest(), point: { x: 30, y: 20 } }],
    ['studio:tabs-drag-cancel', 'cancel', { ...windowRequest(), token: 'native-token' }],
    ['studio:tabs-drag-geometry', 'setGeometry', { ...windowRequest(),
      tabs: [{ tabId: original.tabId, x: 0, y: 0, width: 120, height: 44 }] }],
  ]) {
    assert.equal(await f.invoke(channel, shell, payload), `${method}-wired`);
    const call = f.dragOperations.at(-1);
    assert.equal(call.method, method); assert.equal(call.request, payload);
    assert.equal(call.event.sender, shell); assert.equal(call.event.senderFrame, event.senderFrame);
  }
  assert.equal(f.dragOperations.length, 3);
  assert.deepEqual(f.pages, pages, 'gesture dispatch never navigates a project');
  assert.equal(f.windows.length, windows, 'renderer begin/cancel/geometry cannot create a shell');
  assert.equal(f.launch.length, launches); assert.equal(f.ensured.length, ensured);
  assert.equal(f.projects.length, projects); assert.equal(f.views.length, views);

  const detached = await f.invoke('studio:tabs-detach', shell, tabRequest());
  const target = controller.getWindow(detached.ownerWindowId);
  assert.deepEqual(f.dragWindows, [source, target]);
  assert.equal(new Set(f.dragWindows).size, 2, 'register each initial/detached shell exactly once');
  assert.deepEqual([...options.getWindows()], [target]);
  assert.equal(target.children.has(view), true); assert.equal(view.webContents, project);
  assert.equal(f.launch.length, launches); assert.equal(f.ensured.length, ensured);
  assert.equal(f.projects.length, projects); assert.equal(f.views.length, views);
  assert.deepEqual(f.pages.filter(url => url.includes('/p/')), pages.filter(url => url.includes('/p/')));
  assert.equal(f.stop.length, 0);
  f.app.emit('will-quit');
  assert.equal(f.dragDisposals, 1);
  assert.equal(f.handlers.size, 0);
  assert.deepEqual([...options.getWindows()], []);
  controller.dispose(); assert.equal(f.dragDisposals, 1, 'helper disposal is idempotent through main');
});

test('production detach shell failure rolls back and window loss creates a recovery shell for the same contents', async t => {
  const f = await boot(); t.after(() => f.app.emit('will-quit'));
  const registry = f.controller.registry, source = f.controller.getActiveWindow(), view = f.views[0];
  const tab = registry.snapshot().tabs[0];
  const request = () => ({ windowId: String(source.id), windowRevision: registry.getWindow(String(source.id)).revision,
    tabId: tab.tabId, tabRevision: registry.getTab(tab.tabId).revision });
  f.failNextShellLoad = true;
  await assert.rejects(f.invoke('studio:tabs-detach', source.webContents, request()), /shell load failed/);
  assert.equal(registry.getTab(tab.tabId).ownerWindowId, String(source.id));
  assert.equal(source.children.has(view), true); assert.equal(view.webContents.isDestroyed(), false);
  const target = await f.controller.createWindow();
  const { token } = f.invoke('studio:tabs-transfer-begin', source.webContents, request());
  f.transferAck = async () => { f.transferAck = null; source.destroy(); target.destroy(); };
  await assert.rejects(f.invoke('studio:tabs-transfer', source.webContents, { ...request(), token,
    targetWindowId: String(target.id), targetWindowRevision: registry.getWindow(String(target.id)).revision }));
  const recovered = registry.getTab(tab.tabId), recovery = f.controller.getWindow(recovered.ownerWindowId);
  assert.equal(recovered.state, 'ready'); assert.ok(recovery); assert.equal(recovery.children.has(view), true);
  assert.equal(f.focusedContents, view.webContents); assert.equal(view.webContents.isDestroyed(), false);
  assert.equal(f.views.length, 1); assert.equal(f.ensured.length, 1); assert.equal(f.projects.length, 1);
  assert.equal(f.pages.filter(url => url.includes('/p/')).length, 1);
  assert.equal(f.launch.length, 1); assert.equal(f.stop.length, 0);
});

test('native close coalesces prepare-close, respects cancellation and leaves zero-window app services resident', async t => {
  const f = await boot({ realServices: true }); t.after(() => f.app.emit('will-quit'));
  const controller = f.controller, registry = controller.registry, native = controller.getActiveWindow();
  const view = f.views[0], handlers = [...f.handlers.keys()], initialWindows = f.windows.length;
  f.closeAck = null;
  native.close(); native.close();
  const pending = controller.requestCloseWindow(String(native.id));
  await new Promise(setImmediate);
  assert.equal(f.closeRequests.length, 1, 'repeated native close shares one renderer preparation');
  assert.equal(native.isDestroyed(), false); assert.equal(view.webContents.isDestroyed(), false);
  const ack = prepared => {
    const { contents, request } = f.closeRequests.at(-1);
    return f.invoke(TABS_CHANNELS.closeAck, contents, { ...request, prepared });
  };
  ack(false);
  assert.equal(await pending, false);
  assert.equal(native.isDestroyed(), false); assert.equal(registry.snapshot().tabs.length, 1);
  native.close();
  const retry = controller.requestCloseWindow(String(native.id));
  await new Promise(setImmediate);
  assert.deepEqual(f.closeRequests.map(({ request }) => request.phase), ['prepare', 'release', 'prepare']);
  assert.equal(f.closeRequests.at(-1).request.phase, 'prepare');
  ack(true);
  await new Promise(setImmediate);
  assert.deepEqual(f.closeRequests.map(({ request }) => request.phase), ['prepare', 'release', 'prepare', 'validate']);
  assert.equal(f.closeRequests.at(-1).request.phase, 'validate');
  assert.equal(native.isDestroyed(), false); assert.equal(view.webContents.isDestroyed(), false);
  ack(true);
  assert.equal(await retry, true);
  assert.equal(native.isDestroyed(), true); assert.equal(view.webContents.isDestroyed(), true);
  assert.deepEqual(registry.snapshot().windows, []);
  f.app.emit('window-all-closed');
  assert.equal(f.quits, 0); assert.equal(f.trays[0].isDestroyed(), false);
  assert.deepEqual([...f.handlers.keys()], handlers);
  assert.equal(f.confirmations.length, 0, 'native close never displays owned-backend Quit confirmation');
  assert.equal(f.stop.length, 0);

  f.trays[0].emit('click'); f.trays[0].emit('double-click'); f.app.emit('second-instance');
  const restored = await controller.showWindow();
  assert.ok(restored); assert.equal(restored.isFocused(), true);
  assert.equal(f.windows.length, initialWindows + 1, 'activation burst creates exactly one shell');
  assert.equal(registry.snapshot().windows.length, 1); assert.equal(registry.snapshot().tabs.length, 0);
  assert.equal(f.launch.length, 1); assert.equal(f.projects.length, 1); assert.equal(f.ensured.length, 1);
  assert.equal(f.permissionInstalls, 1); assert.equal(f.trays.length, 1);
});

test('last-tab close uses empty-shell cleanup; second launch focuses the existing unique project view', async t => {
  const f = await boot({ realServices: true,
    existing: { service: 'voxarium_studio', status: 'ok', version: '5.2.0' } });
  t.after(() => f.app.emit('will-quit'));
  const controller = f.controller, registry = controller.registry, native = controller.getActiveWindow();
  const tab = registry.snapshot().tabs[0], owner = registry.getWindow(String(native.id));
  assert.deepEqual(await f.invoke(TABS_CHANNELS.close, native.webContents, {
    windowId: owner.windowId, windowRevision: owner.revision, tabId: tab.tabId, tabRevision: tab.revision }), { closed: true });
  assert.equal(native.isDestroyed(), true); assert.equal(f.trays[0].isDestroyed(), false);
  assert.equal(f.stop.length, 0); assert.equal(f.launch.length, 0);
  f.app.emit('second-instance');
  const restored = await controller.showWindow();
  const reopened = await controller.openProject(tab.project.root, String(restored.id)), view = f.views[1];
  const pages = [...f.pages], ensured = f.ensured.length, windows = f.windows.length;
  f.app.emit('second-instance'); f.trays[0].emit('click');
  await controller.showWindow();
  assert.equal(registry.snapshot().tabs.length, 1); assert.equal(registry.getTab(reopened.tabId).ownerWindowId, String(restored.id));
  assert.equal(f.focusedContents, view.webContents);
  assert.equal(f.windows.length, windows); assert.equal(f.ensured.length, ensured); assert.deepEqual(f.pages, pages);
  assert.equal(f.projects.length, 1); assert.equal(f.launch.length, 0); assert.equal(f.stop.length, 0);
});

const restoredSession = () => ({ version: 1, active_window: 0, windows: [
  { active_tab: 0, geometry: { x: 9000, y: -8000, width: 1400, height: 900, maximized: true }, tabs: [
    { root: 'D:/A', page: { path: '/tasks', query: { task: 'same-task' } } },
    { root: 'D:/B', page: { path: '/explorer', query: { path: 'assets/100%#mesh.vox' } } },
  ] },
  { active_tab: 0, tabs: [{ root: 'D:/C', page: { path: '/documents', query: { path: 'notes.md' } } }] },
] });

test('manifest restart resolves new epoch and restores windows, order, active tabs, pages and available bounds', async t => {
  const manifest = restoredSession();
  const f = await boot({ manifest, epoch: 'new-epoch', existing: { service: 'voxarium_studio', status: 'ok', version: '5.2.0' } });
  t.after(() => f.app.emit('will-quit'));
  assert.deepEqual(f.errors, []);
  assert.equal(f.projects.length, 0, 'restore never reads legacy selection');
  assert.equal(f.launch.length, 0); assert.equal(f.sessionReads, 1);
  const snapshot = f.controller.registry.snapshot();
  assert.equal(snapshot.windows.length, 2);
  for (let index = 0; index < snapshot.windows.length; index++) {
    const window = snapshot.windows[index], saved = manifest.windows[index];
    assert.deepEqual(window.tabIds.map(id => f.controller.registry.getTab(id).project.root), saved.tabs.map(tab => tab.root));
    assert.equal(f.controller.registry.getTab(window.activeTabId).project.root, saved.tabs[saved.active_tab].root);
  }
  assert.ok(snapshot.tabs.every(tab => tab.project.epoch === 'new-epoch'));
  assert.deepEqual(f.ensured.map(request => [request.path, request.create]), [['D:/A', false], ['D:/B', false], ['D:/C', false]]);
  assert.deepEqual(f.restoredPages, manifest.windows.flatMap(window => window.tabs.map(tab => tab.page)));
  const active = f.controller.getActiveWindow();
  assert.equal(String(active.id), snapshot.windows[0].windowId);
  assert.deepEqual([active.options.x, active.options.y, active.options.width, active.options.height], [520, 0, 1400, 900]);
  assert.equal(active.maximized, true);
  await f.controller.flushSession();
  assert.deepEqual(f.manifests.at(-1).windows.map(window => window.tabs), manifest.windows.map(window => window.tabs));
});

test('the unsettled default page is a fallback, not a restore target, and round-trips without error', async t => {
  const manifest = { version: 1, active_window: 0, windows: [
    { active_tab: 1, tabs: [{ root: 'D:/A', page: { path: '/' } },
      { root: 'D:/B', page: { path: '/tasks', query: { task: 'same-task' } } }] }] };
  const f = await boot({ manifest });
  t.after(() => f.app.emit('will-quit'));
  assert.deepEqual(f.errors, []);
  assert.deepEqual(f.restoredPages, [{ path: '/tasks', query: { task: 'same-task' } }],
    "'/' carries no saved target: the fresh renderer lands on the default section itself");
  assert.deepEqual(f.controller.registry.snapshot().tabs.map(tab => tab.project.root), ['D:/A', 'D:/B']);
  await f.controller.flushSession();
  assert.deepEqual(f.manifests.at(-1).windows[0].tabs.map(tab => tab.page), manifest.windows[0].tabs.map(tab => tab.page));
});

test('missing/uninitialised roots and corrupt manifest report their own errors without legacy fallback or create', async t => {
  const manifest = restoredSession();
  const f = await boot({ manifest, missingRoots: ['D:/A', 'D:/B'] });
  t.after(() => f.app.emit('will-quit'));
  assert.equal(f.errors.length, 2);
  assert.match(f.errors[0], /D:\/A/); assert.match(f.errors[1], /D:\/B/);
  assert.deepEqual(f.controller.registry.snapshot().tabs.map(tab => tab.project.root), ['D:/C']);
  assert.ok(f.ensured.every(request => request.create === false));
  assert.equal(f.projects.length, 0); assert.equal(f.launch.length, 1);
  const corrupt = await boot({ manifestError: 'desktop_session_corrupt' });
  t.after(() => corrupt.app.emit('will-quit'));
  assert.match(corrupt.errors[0], /desktop_session_corrupt/);
  assert.equal(corrupt.projects.length, 0); assert.equal(corrupt.ensured.length, 0);
  await corrupt.controller.flushSession();
  assert.equal(corrupt.manifests.length, 0, 'corruption is not silently replaced with an empty snapshot');
  const empty = await boot({ manifest: { version: 1, windows: [], active_window: null } });
  t.after(() => empty.app.emit('will-quit'));
  assert.equal(empty.projects.length, 0); assert.equal(empty.ensured.length, 0);
  assert.equal(empty.controller.registry.snapshot().tabs.length, 0, 'empty topology is not a first-run seed');
});

test('restore coalesces concurrent project open and second launch without extra views, windows or launcher', async t => {
  let raced;
  const f = await boot({ realServices: true, manifest: restoredSession(), onEnsure(request, controller, calls) {
    if (request.path !== 'D:/A' || raced) return;
    raced = Promise.resolve().then(() => controller.openProject(request.path, String(controller.getActiveWindow().id)));
    calls.appDuringRestore = true;
    // Same resident activation path as tray/second-instance; waits for restore.
    calls.activationDuringRestore = controller.showWindow();
  } });
  t.after(() => f.app.emit('will-quit'));
  await raced; await f.activationDuringRestore;
  f.app.emit('second-instance'); await f.controller.showWindow();
  assert.equal(f.controller.registry.snapshot().windows.length, 2);
  assert.equal(f.views.length, 3); assert.equal(f.ensured.length, 3);
  assert.equal(f.launch.length, 1); assert.equal(f.projects.length, 0);
  assert.deepEqual(f.errors, []);
});

test('cancelled detach is not saved; intentional close persists and quit saves before stop without teardown overwrite', async t => {
  const f = await boot();
  t.after(() => f.app.emit('will-quit'));
  const registry = f.controller.registry, source = f.controller.getActiveWindow();
  const second = await f.controller.openProject('D:/B', String(source.id));
  await f.controller.flushSession();
  const before = JSON.stringify(f.manifests.at(-1));
  const request = { windowId: String(source.id), windowRevision: registry.getWindow(String(source.id)).revision,
    tabId: second.tabId, tabRevision: registry.getTab(second.tabId).revision };
  f.failNextShellLoad = true;
  await assert.rejects(f.invoke(TABS_CHANNELS.detach, source.webContents, request), /shell load failed/);
  await f.controller.flushSession();
  assert.equal(JSON.stringify(f.manifests.at(-1)), before);
  registry.closeTab(second.tabId);
  await f.controller.flushSession();
  assert.equal(f.manifests.at(-1).windows[0].tabs.length, 1);
  f.app.emit('before-quit', { preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(f.stop.length, 1);
  assert.ok(f.timeline.lastIndexOf('save') < f.timeline.indexOf('stop'));
  const final = JSON.stringify(f.manifests.at(-1)), writes = f.manifests.length;
  f.app.emit('will-quit'); await new Promise(setImmediate);
  assert.equal(f.controller.registry.snapshot().tabs.length, 0);
  assert.equal(f.manifests.length, writes);
  assert.equal(JSON.stringify(f.manifests.at(-1)), final);
  const restart = await boot({ manifest: JSON.parse(final), epoch: 'restart-epoch' });
  t.after(() => restart.app.emit('will-quit'));
  assert.equal(restart.controller.registry.snapshot().tabs.length, 1, 'closed B does not resurrect');
  assert.equal(restart.ensured.some(request => request.path === 'D:/B'), false);
});

test('quit waits outside restore gate and a failed manifest write prevents backend stop until retry', async t => {
  let requested = false;
  const f = await boot({ manifest: restoredSession(), onEnsure(request, _controller, calls) {
    if (requested || request.path !== 'D:/A') return;
    requested = true; calls.sessionSaveError = true;
    calls.app.emit('before-quit', { preventDefault() {} });
  } });
  t.after(() => f.app.emit('will-quit'));
  await new Promise(setImmediate);
  assert.equal(f.controller.registry.snapshot().tabs.length, 3);
  assert.equal(f.diagnostics.some(entry => entry.event === 'startup-failed'), false);
  assert.ok(f.errors.some(message => message.includes('desktop_session_write_failed')));
  assert.equal(f.stop.length, 0); assert.equal(f.quits, 0);
  f.sessionSaveError = false;
  f.app.emit('before-quit', { preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(f.stop.length, 1);
  assert.equal(f.manifests.at(-1).windows.flatMap(window => window.tabs).length, 3);
});

test('main app events survive zero windows and route a native click without relaunching', async t => {
  const f = await boot({ projectEvents: true });
  t.after(() => f.app.emit('will-quit'));
  const tab = f.controller.registry.snapshot().tabs[0], native = f.controller.getWindow(tab.ownerWindowId);
  f.invoke('studio:notifications', native.webContents, 'set-enabled', true);
  native.destroy();
  const socket = f.sockets[0], project = { ...tab.project, name: 'A' };
  socket.frame({ type: 'system.hello', epoch: project.epoch, seq: 0,
    data: { epoch: project.epoch, resumed_from: null, attention: [] } });
  socket.frame({ type: 'desktop.producer_attention', epoch: project.epoch, seq: 1,
    data: { project, source_seq: 7, target: '/producer', waiting: [{ chat_id: 'chat', target: '/producer?chat=chat' }] } });
  socket.frame({ type: 'desktop.notification', epoch: project.epoch, seq: 2,
    data: { project, source_seq: 8, target: '/tasks?task=same', notification: {
      automation_id: 'same', automation_name: 'Review', title: 'Ready', message: 'Review', trigger: 'manual', values: {},
    } } });
  assert.equal(f.controller.registry.snapshot().windows.length, 0);
  assert.equal(f.notifications.length, 1);
  const launchCount = f.launch.length;
  f.notifications[0].emit('click'); f.notifications[0].emit('click');
  await new Promise(setImmediate);
  assert.equal(f.controller.registry.snapshot().tabs.length, 1);
  assert.equal(f.restoredPages.at(-1).query.task, 'same');
  assert.equal(f.ensured.length, 2, 'initial open plus one UI reopen only');
  assert.equal(f.launch.length, launchCount);
  assert.equal(f.sockets.length, 1, 'not a connection per window/renderer');
  const reopened = f.controller.registry.snapshot().tabs[0];
  assert.equal(f.attention.get(reopened.ownerWindowId), true, 'resident waiting state applies to reopened UI');
  assert.deepEqual(f.errors, []);
});
