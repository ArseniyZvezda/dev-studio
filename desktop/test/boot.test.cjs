const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { ORIGIN } = require('../policy.cjs');

// Exercise the actual boot path without spawning processes, opening windows or
// contacting the fixed server port. The desktop and backend must share a version (§2.7).
async function boot({ existing = null, packaged = true, studioVersion = packaged ? '5.2.0' : '5.0.0',
  startedVersion = studioVersion, releaseVersion = '5.2.0', staleBackendVersion,
  confirmationResponse = 1, project = { root: 'D:\\Games\\CurrentProject' }, projectOk = true,
  recheckedExisting } = {}) {
  const calls = { launch: [], stop: [], errors: [], pages: [], exits: [], updates: [], diagnostics: [],
    confirmations: [], projects: [] };
  const instance = 'test-desktop-instance';
  let launched = false, completion, healthChecks = 0;
  const app = new EventEmitter();
  Object.assign(app, {
    isPackaged: packaged,
    getPath: () => path.resolve('test-profile'), setPath() {},
    getLocale: () => 'en', getVersion: () => packaged ? releaseVersion : '5.0.0',
    setAppUserModelId() {}, requestSingleInstanceLock: () => true,
    quit() {}, exit: code => calls.exits.push(code),
    whenReady: () => ({ then: callback => ({
      catch: onError => { completion = Promise.resolve().then(callback).catch(onError); },
    }) }),
  });
  class BrowserWindow extends EventEmitter {
    constructor() {
      super();
      this.destroyed = false;
      this.webContents = Object.assign(new EventEmitter(), {
        session: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {} },
        setWindowOpenHandler() {},
      });
    }
    async loadFile() {}
    async loadURL(url) { calls.pages.push(url); }
    show() {}
    isDestroyed() { return this.destroyed; }
    close() { this.destroyed = true; }
  }
  const image = { resize() { return this; } };
  const metadata = { productName: 'Voxarium Studio', version: app.getVersion(),
    ...(packaged ? { studioVersion } : {}),
    ...(staleBackendVersion ? { backendVersion: staleBackendVersion } : {}) };
  const mocks = {
    electron: { app, BrowserWindow,
      dialog: {
        showErrorBox: (_title, text) => calls.errors.push(text),
        showMessageBox: async (_window, options) => {
          calls.confirmations.push({ ...options, projectReads: calls.projects.length });
          return { response: confirmationResponse };
        },
      },
      shell: {}, Menu: { setApplicationMenu() {} }, ipcMain: { handle() {} },
      nativeImage: { createFromPath: () => image },
    },
    'node:fs': { mkdirSync() {}, openSync: () => 7, closeSync() {}, writeFileSync() {},
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
      execFile: (...args) => { calls.stop.push(args.slice(0, -1)); args.at(-1)(null); },
    },
    './package.json': metadata,
    './scripts/release.cjs': { sourceReleaseInfo: () => ({ version: studioVersion }) },
    './window-controls.cjs': { registerWindowControls() {} },
    './tray.cjs': { registerTray: () => ({}) },
    './producer-attention.cjs': { registerProducerAttention: () => ({}) },
    './startup.cjs': { registerStartup() {} },
    './update-feed.cjs': { configureUpdateFeed() {} },
    './updates.cjs': { registerUpdates: options => calls.updates.push(options) },
    'electron-updater': { autoUpdater: {} },
  };
  const desktop = path.resolve(__dirname, '..');
  vm.runInNewContext(fs.readFileSync(path.join(desktop, 'main.cjs'), 'utf8'), {
    __dirname: desktop,
    require: name => {
      if (Object.hasOwn(mocks, name)) return mocks[name];
      if (name.includes('/locales/')) return require('../../frontend/src/locales/en.json');
      return require(name.startsWith('.') ? path.resolve(desktop, name) : name);
    },
    process: { env: {}, pid: 1234, resourcesPath: path.resolve('test-resources') },
    fetch: async url => {
      if (url === `${ORIGIN}/api/project`) {
        calls.projects.push(url);
        return { ok: projectOk, json: async () => project };
      }
      assert.equal(url, `${ORIGIN}/healthz`);
      healthChecks++;
      const health = launched
        ? { service: 'voxarium_studio', status: 'ok', version: startedVersion, desktop_instance: instance }
        : healthChecks > 1 && recheckedExisting !== undefined ? recheckedExisting : existing;
      return { ok: !!health, json: async () => health };
    },
    AbortSignal, setTimeout, console,
  }, { filename: 'main.cjs' });
  await completion;
  return calls;
}

test('tagged desktop starts only its matching backend and uses the same version for updates', async () => {
  const calls = await boot();
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [ORIGIN]);
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
  assert.deepEqual(calls.pages, [ORIGIN]);
  assert.equal(calls.launch.length, 0);
  assert.equal(calls.stop.length, 0);
  assert.equal(calls.projects.length, 0);
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
  assert.deepEqual(calls.pages, [ORIGIN]);
  assert.equal(calls.launch.length, 1);
  assert.equal(calls.stop.length, 0);
});

test('untagged desktop starts its exact commit hash while updater retains a numeric package version', async () => {
  const calls = await boot({ studioVersion: 'abcdef0', releaseVersion: '5.0.0' });
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [ORIGIN]);
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
  assert.deepEqual(calls.pages, [ORIGIN]);
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
  assert.equal(calls.projects.length, 1);
  assert.equal(calls.launch.length, 1);
  const args = calls.launch[0][1];
  assert.equal(args[args.indexOf('-Project') + 1], 'D:\\Games\\CurrentProject');
  assert.equal(args.includes('-Desktop'), false);
  assert.ok(args.includes(path.join(path.resolve('test-resources'), 'runtime', 'run.ps1')));
  assert.deepEqual(calls.pages, [ORIGIN]);
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

test('matching commits attach without a switch prompt or project read', async () => {
  const calls = await boot({ existing: independentCommit, studioVersion: independentCommit.version });
  assert.deepEqual(calls.confirmations, []);
  assert.deepEqual(calls.projects, []);
  assert.deepEqual(calls.launch, []);
  assert.deepEqual(calls.errors, []);
  assert.deepEqual(calls.pages, [ORIGIN]);
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
