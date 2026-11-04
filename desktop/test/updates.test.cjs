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
