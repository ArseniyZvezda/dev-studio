const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerUpdates } = require('../updates.cjs');
const { ORIGIN } = require('../policy.cjs');

function fixture(t, consent = true, configureFeed = async () => {}) {
  const window = new EventEmitter();
  window.isDestroyed = () => false;
  window.webContents = { send() {}, mainFrame: { url: ORIGIN } };
  const updater = new EventEmitter();
  const calls = [];
  updater.checkForUpdates = async () => { calls.push('check'); updater.emit('update-available', { version: '2.0.0' }); };
  updater.downloadUpdate = async () => { calls.push('download'); updater.emit('download-progress', { percent: 42 }); updater.emit('update-downloaded'); };
  updater.quitAndInstall = (...args) => calls.push(['install', ...args]);
  let handler;
  const ipcMain = { handle(_name, fn) { handler = fn; }, removeHandler() {} };
  const controller = registerUpdates({ ipcMain, window, updater, version: '1.0.0', configureFeed,
    prepareInstall: async () => { calls.push('stop'); return consent; } });
  t.after(() => window.emit('closed'));
  return { ...controller, calls, updater, invoke: (event, action) => handler(event, action),
    event: { sender: window.webContents, senderFrame: window.webContents.mainFrame } };
}
test('explicit download and installation stop the backend first', async t => {
  const f = fixture(t);
  assert.equal(f.updater.autoDownload, false);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  assert.equal((await f.action('check')).available, true);
  assert.deepEqual(f.calls, ['check']);
  assert.equal((await f.action('download')).downloaded, true);
  assert.equal((await f.action('install')).state, 'restarting');
  assert.deepEqual(f.calls, ['check', 'download', 'stop', ['install', false, true]]);
});
test('cancelling installation keeps the downloaded update', async t => {
  const f = fixture(t, false);
  await f.action('check'); await f.action('download');
  const state = await f.action('install');
  assert.equal(state.downloaded, true);
  assert.equal(state.state, 'idle');
  assert.equal(f.calls.some(Array.isArray), false);
});
test('network errors are visible and retryable', async t => {
  const f = fixture(t);
  f.updater.checkForUpdates = async () => { throw new Error('offline'); };
  assert.equal((await f.action('check')).error, 'offline');
  f.updater.checkForUpdates = async () => {};
  assert.equal((await f.action('check')).error, null);
});
test('private feed credentials are resolved before contacting GitHub', async t => {
  const f = fixture(t, true, async () => { throw new Error('Sign in to GitHub'); });
  const state = await f.action('check');
  assert.equal(state.state, 'failed');
  assert.equal(state.error, 'Sign in to GitHub');
  assert.deepEqual(f.calls, []);
});
test('untrusted senders and arbitrary actions cannot invoke the updater', async t => {
  const f = fixture(t);
  assert.throws(() => f.invoke({ sender: {} }, 'install'), /Untrusted/);
  assert.throws(() => f.invoke({ ...f.event, senderFrame: { url: ORIGIN } }, 'install'), /Untrusted/);
  await assert.rejects(f.invoke(f.event, 'https://example.com/setup.exe'), /Unknown/);
});
test('concurrent checks are coalesced and installation requires a download', async t => {
  const f = fixture(t);
  assert.equal((await f.action('install')).state, 'failed');
  let finish;
  f.updater.checkForUpdates = () => new Promise(resolve => { finish = resolve; });
  const checking = f.action('check');
  assert.equal((await f.action('check')).state, 'checking');
  finish(); await checking;
});

function appFixture(t, overrides = {}) {
  const windows = [new EventEmitter(), new EventEmitter()];
  const owners = new Map();
  for (const [index, window] of windows.entries()) {
    window.isDestroyed = () => !!window.destroyed;
    const contents = { mainFrame: { url: index ? `${ORIGIN}/p/b` : 'data:text/html,shell' },
      isDestroyed: () => !!window.destroyed, received: [], send(...args) { this.received.push(args); } };
    window.webContents = contents;
    owners.set(contents, { role: index ? 'project' : 'shell', contents, window });
  }
  const updater = new EventEmitter(), calls = [];
  updater.checkForUpdates = async () => { calls.push('check'); updater.emit('update-available', { version: '2' }); };
  updater.downloadUpdate = async () => { calls.push('download'); updater.emit('update-downloaded'); };
  updater.quitAndInstall = () => calls.push('install');
  let handler, registrations = 0, removals = 0;
  const ipcMain = { handle: (_name, fn) => { registrations++; handler = fn; }, removeHandler: () => { removals++; } };
  const options = { ipcMain, updater, version: '1', prepareInstall: async () => { calls.push('prepare'); return true; },
    authorize(event) {
      const owner = owners.get(event.sender);
      if (!owner) throw new Error('Untrusted sender');
      return owner;
    }, broadcast(channel, state) {
      for (const contents of owners.keys()) if (!contents.isDestroyed()) contents.send(channel, state);
    }, ...overrides };
  const api = registerUpdates(options);
  t.after(() => api.dispose());
  const event = window => ({ sender: window.webContents, senderFrame: window.webContents.mainFrame });
  return { api, options, windows, owners, calls, updater, event,
    invoke: (event, name) => handler(event, name), counts: () => ({ registrations, removals }) };
}

test('one app updater broadcasts to live contents and survives all shell windows closing', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const f = appFixture(t);
  assert.equal(registerUpdates(f.options), f.api);
  assert.deepEqual(f.counts(), { registrations: 1, removals: 0 });
  assert.equal(f.updater.listenerCount('update-available'), 1);
  const [a, b] = f.windows;
  await f.invoke(f.event(a), 'check'); // An exact authorized data shell is trusted.
  for (const window of f.windows) assert.equal(window.webContents.received.at(-1)[1].latest, '2');
  a.emit('closed'); a.destroyed = true; f.owners.delete(a.webContents);
  const before = a.webContents.received.length;
  f.updater.emit('download-progress', { percent: 44 });
  assert.equal(a.webContents.received.length, before);
  assert.equal(b.webContents.received.at(-1)[1].progress, 44);
  assert.throws(() => f.invoke(f.event(a), 'download'), /Untrusted/);
  assert.throws(() => f.invoke({ ...f.event(b), senderFrame: { url: ORIGIN } }, 'check'), /Untrusted/);
  b.webContents.mainFrame.url = 'https://example.com';
  assert.throws(() => f.invoke(f.event(b), 'check'), /Untrusted/);
  b.webContents.mainFrame.url = `${ORIGIN}/p/b`;
  b.emit('closed'); b.destroyed = true; f.owners.delete(b.webContents);
  assert.equal(f.updater.listenerCount('update-available'), 1);
  t.mock.timers.tick(15_000);
  await new Promise(setImmediate);
  assert.deepEqual(f.calls, ['check', 'check']);
  t.mock.timers.tick(180 * 60_000);
  await new Promise(setImmediate);
  assert.deepEqual(f.calls, ['check', 'check', 'check']);
  f.api.dispose(); f.api.dispose();
  assert.deepEqual(f.counts(), { registrations: 1, removals: 1 });
  assert.equal(f.updater.listenerCount('update-available'), 0);
  t.mock.timers.tick(180 * 60_000);
  await new Promise(setImmediate);
  assert.equal(f.calls.length, 3);
});

test('controller updater guards all native side effects when unsupported', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const f = appFixture(t, { supported: false, reason: 'unpackaged' });
  const event = f.event(f.windows[1]);
  for (const action of ['check', 'download', 'install', 'unknown']) {
    const state = await f.invoke(event, action);
    assert.equal(state.supported, false); assert.equal(state.reason, 'unpackaged');
  }
  t.mock.timers.tick(180 * 60_000);
  assert.deepEqual(f.calls, []);
  assert.throws(() => f.invoke({ sender: {} }, 'install'), /Untrusted/);
});

test('disposing during pending feed/installation prevents late native work', async t => {
  let finishFeed;
  const f = appFixture(t, { configureFeed: () => new Promise(resolve => { finishFeed = resolve; }) });
  const checking = f.api.action('check');
  f.api.dispose(); finishFeed(); await checking;
  assert.deepEqual(f.calls, []);
  let finishConsent;
  const other = appFixture(t, { prepareInstall: () => new Promise(resolve => { finishConsent = resolve; }) });
  await other.api.action('check'); await other.api.action('download');
  const installing = other.api.action('install');
  other.api.dispose(); finishConsent(true); await installing;
  assert.deepEqual(other.calls, ['check', 'download']);
});

for (const step of ['summary', 'stop']) {
  test(`guarded install ${step} failure retains download and permits retry`, async t => {
    let guarded = false, failStep = true;
    const f = appFixture(t, { install: async commit => {
      assert.equal(guarded, false);
      guarded = true;
      try {
        if (failStep) throw new Error(`${step} failed`);
        await commit();
        return true;
      } finally { guarded = false; }
    } });
    await f.api.action('check'); await f.api.action('download');
    const failed = await f.api.action('install');
    assert.equal(failed.state, 'failed');
    assert.equal(failed.error, `${step} failed`);
    assert.equal(failed.downloaded, true);
    assert.equal(guarded, false);
    assert.equal(f.updater.autoInstallOnAppQuit, false);
    assert.deepEqual(f.calls, ['check', 'download']);
    failStep = false;
    assert.equal((await f.api.action('install')).state, 'restarting');
    assert.deepEqual(f.calls, ['check', 'download', 'install']);
  });
}

for (const asynchronous of [false, true]) {
  test(`native install ${asynchronous ? 'rejection' : 'throw'} reaches guard and permits retry`, async t => {
    let guarded = false, caught;
    const f = appFixture(t, { install: async commit => {
      guarded = true;
      try { await commit(); return true; }
      catch (error) { caught = error; throw error; }
      finally { guarded = false; }
    } });
    await f.api.action('check'); await f.api.action('download');
    f.updater.quitAndInstall = () => {
      if (asynchronous) return Promise.reject(new Error('native failed'));
      throw new Error('native failed');
    };
    const failed = await f.api.action('install');
    assert.equal(failed.state, 'failed');
    assert.equal(failed.error, 'native failed');
    assert.equal(failed.downloaded, true);
    assert.equal(caught.message, 'native failed');
    assert.equal(guarded, false);
    assert.equal(f.updater.autoInstallOnAppQuit, false);
    f.updater.quitAndInstall = (...args) => f.calls.push(['install', ...args]);
    assert.equal((await f.api.action('install')).state, 'restarting');
    assert.deepEqual(f.calls, ['check', 'download', ['install', false, true]]);
  });
}

test('late updater error clears restarting without enabling install on ordinary quit', async t => {
  const f = appFixture(t, { install: async commit => { await commit(); return true; } });
  await f.api.action('check'); await f.api.action('download');
  assert.equal((await f.api.action('install')).state, 'restarting');
  await new Promise(resolve => setImmediate(() => {
    f.updater.emit('error', new Error('installer launch failed'));
    resolve();
  }));
  const failed = await f.api.action('state');
  assert.equal(failed.state, 'failed');
  assert.equal(failed.error, 'installer launch failed');
  assert.equal(failed.downloaded, true);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  assert.equal((await f.api.action('install')).state, 'restarting');
  assert.deepEqual(f.calls, ['check', 'download', 'install', 'install']);
});

test('guard cancellation and disposal before commit never invoke native install', async t => {
  const cancelled = appFixture(t, { install: async () => false });
  await cancelled.api.action('check'); await cancelled.api.action('download');
  assert.equal((await cancelled.api.action('install')).state, 'idle');
  assert.deepEqual(cancelled.calls, ['check', 'download']);
  let commit;
  const disposed = appFixture(t, { install: async next => { commit = next; return false; } });
  await disposed.api.action('check'); await disposed.api.action('download');
  await disposed.api.action('install');
  disposed.api.dispose(); await commit();
  assert.deepEqual(disposed.calls, ['check', 'download']);
});

test('NSIS handoff awaits native spawn and resets its failed-install latch for retry', async t => {
  const f = appFixture(t, { install: async commit => { await commit(); return true; } });
  await f.api.action('check'); await f.api.action('download');
  let rejectLaunch;
  const originalSpawn = () => new Promise((_resolve, reject) => { rejectLaunch = reject; });
  f.updater.spawnLog = originalSpawn;
  f.updater.quitAndInstallCalled = false;
  let launches = 0;
  f.updater.quitAndInstall = () => {
    if (f.updater.quitAndInstallCalled) return; // Real BaseUpdater guard.
    f.updater.quitAndInstallCalled = true; launches++;
    f.updater.spawnLog('installer.exe').catch(error => f.updater.emit('error', error));
    // Real NSIS returns synchronously, before spawn has completed.
  };
  const pending = f.api.action('install');
  await new Promise(setImmediate);
  let completed = false; pending.then(() => { completed = true; });
  await Promise.resolve(); assert.equal(completed, false);
  rejectLaunch(new Error('NSIS spawn failed'));
  assert.equal((await pending).state, 'failed');
  assert.equal(f.updater.quitAndInstallCalled, false);
  assert.equal(f.updater.spawnLog, originalSpawn);
  f.updater.spawnLog = async () => true;
  assert.equal((await f.api.action('install')).state, 'restarting');
  assert.equal(launches, 2); assert.equal(f.updater.autoInstallOnAppQuit, false);
});

test('NSIS ENOENT openPath handoff awaits success; string errors and rejection retain retry', async t => {
  let guarded = false, settleOpen, rejectOpen, spawns = 0, opens = 0;
  const shell = { openPath(file) {
    assert.equal(this, shell); assert.equal(file, 'installer.exe'); opens++;
    return new Promise((resolve, reject) => { settleOpen = resolve; rejectOpen = reject; });
  } };
  const originalOpen = shell.openPath;
  // Only Electron is stubbed; exercise the production observer without native work.
  const module = { exports: {} };
  const updaterRequire = require('node:module').createRequire(require.resolve('../updates.cjs'));
  require('node:vm').runInNewContext(require('node:fs').readFileSync(require.resolve('../updates.cjs'), 'utf8'), {
    module, require: name => name === 'electron' ? { shell } : updaterRequire(name),
    setTimeout, setInterval, clearTimeout, clearInterval,
  });
  const f = appFixture(t, { install: async commit => {
    guarded = true;
    try { await commit(); return true; } finally { guarded = false; }
  } });
  f.api.dispose();
  const api = module.exports.registerUpdates(f.options);
  t.after(() => api.dispose());
  await api.action('check'); await api.action('download');
  f.updater.installerPath = 'installer.exe';
  const originalSpawn = f.updater.spawnLog = async () => {
    spawns++; throw Object.assign(new Error('missing'), { code: 'ENOENT' });
  };
  f.updater.quitAndInstall = () => {
    if (f.updater.quitAndInstallCalled) return;
    f.updater.quitAndInstallCalled = true;
    // NsisUpdater.doInstall's existing fallback: its promise is not returned.
    f.updater.spawnLog(f.updater.installerPath).catch(error => {
      if (error.code === 'ENOENT') shell.openPath(f.updater.installerPath).catch(error => f.updater.emit('error', error));
      else f.updater.emit('error', error);
    });
  };
  const assertPending = async pending => {
    let completed = false; pending.then(() => { completed = true; });
    await new Promise(setImmediate);
    assert.equal(completed, false, completed ? JSON.stringify(await pending) : ''); assert.equal(guarded, true);
  };
  const stringFailure = api.action('install');
  await assertPending(stringFailure); settleOpen('Failed to open installer');
  assert.equal((await stringFailure).state, 'failed');
  assert.equal(f.updater.quitAndInstallCalled, false);
  const rejection = api.action('install');
  await assertPending(rejection); rejectOpen(new Error('shell rejected'));
  const failed = await rejection;
  assert.equal(failed.state, 'failed'); assert.equal(failed.downloaded, true);
  assert.equal(f.updater.quitAndInstallCalled, false);
  assert.equal(shell.openPath, originalOpen); assert.equal(f.updater.spawnLog, originalSpawn);
  const success = api.action('install');
  await assertPending(success); settleOpen('');
  assert.equal((await success).state, 'restarting');
  assert.equal(f.updater.quitAndInstallCalled, true);
  await api.action('install'); // No repeated installer after a confirmed handoff.
  assert.equal(spawns, 3); assert.equal(opens, 3);
  assert.equal(shell.openPath, originalOpen); assert.equal(f.updater.spawnLog, originalSpawn);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
});
