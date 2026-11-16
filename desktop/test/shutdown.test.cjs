const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { ORIGIN } = require('../policy.cjs');

const tick = () => new Promise(setImmediate);
const idle = () => ({ status: 'idle', requires_confirmation: false, errors: [], projects: [] });
const active = () => ({ status: 'active', requires_confirmation: true, errors: [], projects: [
  { name: 'Background A', root: 'D:/A', status: 'active', errors: [], counts: { tasks: 1 } },
  { name: 'Visible B', root: 'D:/B', status: 'idle', errors: [], counts: { tasks: 0 } },
] });
function fixture(ownsBackend = true, options = {}) {
  const app = new EventEmitter();
  const nativeUpdater = new EventEmitter();
  Object.assign(app, {
    isPackaged: true, setPath() {}, getPath: () => 'profile',
    requestSingleInstanceLock: () => true,
    whenReady: () => ({ then: () => ({ catch() {} }) }),
  });
  let exits = 0;
  app.quit = () => {
    let prevented = false;
    app.emit('before-quit', { preventDefault() { prevented = true; } });
    if (!prevented) { exits++; app.emit('will-quit'); }
  };
  const calls = [], errors = [], confirmations = [], requests = [], phases = [];
  const state = { summary: active(), consent: 1, barrier: true, health: null, ...options };
  const messages = require('../../frontend/src/locales/' + (state.locale || 'en') + '.json').desktop;
  const source = fs.readFileSync(path.join(__dirname, '../main.cjs'), 'utf8');
  const context = {
    require(name) {
      if (name === 'electron') return { app, autoUpdater: nativeUpdater, dialog: {
        showErrorBox: (...args) => errors.push(args),
        showMessageBox: async (...args) => {
          confirmations.push(args.at(-1));
          return { response: state.consent };
        },
      } };
      if (name === 'node:crypto') return { randomUUID: () => 'our-instance' };
      if (name === 'node:child_process') return { execFile: (...args) => calls.push(args) };
      if (name.startsWith('./')) return require(path.join(__dirname, '..', name));
      return require(name);
    },
    __dirname: 'resources/app.asar', process: { resourcesPath: 'resources', env: { VOXARIUM_STUDIO_TOKEN: 'test-secret' } },
    setTimeout, clearTimeout, AbortSignal, messagesForTest: messages,
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/healthz')) return { ok: !!state.health, json: async () => state.health };
      assert.equal(url, ORIGIN + '/api/projects/activity');
      if (state.summary instanceof Error) throw state.summary;
      return { ok: true, json: async () => state.summary };
    },
    controller: {
      getActiveWindow: () => state.window,
      bridge: { withAppCloseBarrier: async callback => {
        phases.push('prepare');
        if (!state.barrier) { phases.push('release'); return false; }
        phases.push('validate');
        try { return await callback(() => { phases.push('revalidate'); if (state.stale) throw new Error('stale'); }); }
        finally { phases.push('release'); }
      } },
    },
  };
  vm.runInNewContext(source + `
    owned = ${ownsBackend}; shutdownFile = ${ownsBackend ? "'profile/run/our-instance.stop'" : 'null'};
    messages = messagesForTest; desktopController = controller;
    globalThis.install = commit => requestShutdown('update', commit);
    globalThis.installHandoff = (updater, commit) => requestShutdown('update', () => installAndQuit(updater, commit));
  `, context);
  return { app, nativeUpdater, calls, errors, confirmations, requests, phases, state, messages,
    install: context.install, installHandoff: context.installHandoff, exits: () => exits };

}

test('quit waits for process cleanup even when quit is requested again', async () => {
  const f = fixture();
  f.app.quit();
  f.app.quit();
  assert.equal(f.exits(), 0);
  await tick();
  assert.equal(f.confirmations.length, 1);
  assert.match(f.confirmations[0].detail, /Background A/);
  assert.doesNotMatch(f.confirmations[0].detail, /Visible B/);
  assert.equal(f.calls.length, 1);
  const [exe, args, options, done] = f.calls[0];
  assert.equal(exe, 'powershell.exe');
  assert.equal(args[args.indexOf('-File') + 1], path.join('resources', 'stop-backend.ps1'));
  assert.equal(options.windowsHide, true);
  done(null);
  await new Promise(setImmediate);
  assert.equal(f.exits(), 1);
});

test('cleanup failure stays visible and can be retried', async () => {
  const f = fixture();
  f.app.quit();
  await tick();
  f.calls[0][3](new Error('failed to stop'));
  await new Promise(setImmediate);
  assert.equal(f.exits(), 0);
  assert.equal(f.errors.length, 1);
  f.app.quit();
  await tick();
  assert.equal(f.calls.length, 2);
  f.calls[1][3](null);
  await new Promise(setImmediate);
  assert.equal(f.exits(), 1);
});

test('closing a desktop attached to an independent server does not stop it', async () => {
  const f = fixture(false);
  f.app.quit();
  await tick();
  assert.equal(f.exits(), 1);
  assert.equal(f.calls.length, 0);
  assert.match(f.confirmations[0].detail, /independently/);
});

test('zero windows does not request quit or stop an owned or independent backend', () => {
  for (const owned of [true, false]) {
    const f = fixture(owned);
    f.app.emit('window-all-closed');
    assert.equal(f.exits(), 0);
    assert.equal(f.calls.length, 0);
  }
});

test('cancel preserves background work with no windows; EN/RU use app-wide wording', async () => {
  for (const locale of ['en', 'ru']) {
    const f = fixture(true, { consent: 0, locale });
    f.app.emit('window-all-closed'); f.app.quit();
    await tick();
    assert.equal(f.calls.length, 0); assert.equal(f.exits(), 0);
    assert.equal(f.confirmations[0].message, f.messages.close);
    assert.match(f.confirmations[0].detail, /Background A/);
    assert.ok(f.confirmations[0].detail.includes(f.messages.ownedShutdown));
    assert.deepEqual(f.phases, ['prepare', 'validate', 'release']);
    const request = f.requests[0];
    assert.equal(request.options.cache, 'no-store'); assert.equal(request.options.redirect, 'error');
    assert.equal(request.options.headers.Authorization, 'Bearer test-secret');
  }
});

test('summary unavailable or malformed blocks stop/install and allows retry; unknown requires consent', async () => {
  const f = fixture(false, { summary: new Error('offline') });
  f.app.quit(); await tick();
  assert.equal(f.exits(), 0); assert.equal(f.errors[0][1], f.messages.activityFailed);
  f.state.summary = { status: 'idle' };
  let installed = 0;
  await assert.rejects(f.install(() => installed++), /could not check activity/);
  assert.equal(f.calls.length, 0); assert.equal(installed, 0);
  f.state.summary = { ...active(), status: 'unknown', requires_confirmation: false, errors: ['jobs'] };
  f.state.consent = 0;
  assert.equal(await f.install(() => installed++), false);
  assert.ok(f.confirmations[0].detail.includes(f.messages.activityUnknown));
  f.state.summary = idle(); f.state.consent = 1;
  assert.equal(await f.install(() => installed++), true);
  assert.equal(installed, 1); assert.equal(f.calls.length, 0);
});

test('failed save barrier or stale renderer never reaches backend stop or installer', async () => {
  const f = fixture(true, { barrier: false });
  assert.equal(await f.install(() => assert.fail('installer')), false);
  assert.equal(f.requests.length, 0); assert.equal(f.calls.length, 0);
  f.state.barrier = true; f.state.stale = true;
  await assert.rejects(f.install(() => assert.fail('installer')), /stale/);
  assert.equal(f.calls.length, 0); assert.equal(f.phases.at(-1), 'release');
});

test('a successful stop command with a surviving owned listener is not success', async () => {
  const f = fixture(true, { health: { desktop_instance: 'our-instance' } });
  const first = f.install(() => assert.fail('installer'));
  await tick(); f.calls[0][3](null);
  await assert.rejects(first, /could not be stopped/);
  assert.equal(f.phases.at(-1), 'release');
  f.state.health = null;
  const retry = f.install(() => true);
  await tick(); assert.equal(f.calls.length, 2); f.calls[1][3](null);
  assert.equal(await retry, true);
});

test('update handoff holds fences, rejects late failure and permits retry without stopping twice', async () => {
  const f = fixture(), updater = new EventEmitter();
  let installs = 0;
  let launch;
  const installing = f.installHandoff(updater, () => { installs++; return new Promise(resolve => { launch = resolve; }); });
  await tick();
  f.app.quit(); // A competing Quit cannot inherit consent while stopping.
  f.calls[0][3](null); await tick();
  assert.equal(installs, 1); assert.equal(f.calls.length, 1);
  assert.notEqual(f.phases.at(-1), 'release');
  updater.emit('error', new Error('native install failed'));
  await assert.rejects(installing, /native install failed/);
  assert.equal(f.phases.at(-1), 'release'); assert.equal(f.exits(), 0);
  // BaseUpdater's setImmediate still fires even after NSIS reported failure.
  f.nativeUpdater.emit('before-quit-for-update'); f.app.quit(); await tick();
  assert.equal(f.exits(), 0); assert.equal(f.confirmations.length, 1);
  launch(); // A late completion cannot revive the failed handoff.
  const retried = f.installHandoff(updater, () => { installs++; });
  assert.equal(await retried, true);
  assert.equal(f.calls.length, 1); assert.equal(installs, 2);
  assert.equal(f.exits(), 1); assert.equal(f.calls.length, 1);
});
