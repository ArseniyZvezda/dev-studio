const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

function fixture(ownsBackend = true) {
  const app = new EventEmitter();
  Object.assign(app, {
    isPackaged: true, setPath() {}, getPath: () => 'profile',
    requestSingleInstanceLock: () => true,
    whenReady: () => ({ then: () => ({ catch() {} }) }),
  });
  let exits = 0;
  app.quit = () => {
    let prevented = false;
    app.emit('before-quit', { preventDefault() { prevented = true; } });
    if (!prevented) exits++;
  };
  const calls = [], errors = [];
  const source = fs.readFileSync(path.join(__dirname, '../main.cjs'), 'utf8');
  vm.runInNewContext(source + (ownsBackend ? `
    owned = true; shutdownFile = 'profile/run/our-instance.stop';
    messages = { stopFailed: 'Could not stop backend' };
  ` : ''), {
    require(name) {
      if (name === 'electron') return { app, dialog: { showErrorBox: (...args) => errors.push(args) } };
      if (name === 'node:child_process') return { execFile: (...args) => calls.push(args) };
      if (name.startsWith('./')) return require(path.join(__dirname, '..', name));
      return require(name);
    },
    __dirname: 'resources/app.asar', process: { resourcesPath: 'resources' },
    setTimeout,
  });
  return { app, calls, errors, exits: () => exits };
}

test('quit waits for process cleanup even when quit is requested again', async () => {
  const f = fixture();
  f.app.quit();
  f.app.quit();
  assert.equal(f.exits(), 0);
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
  f.calls[0][3](new Error('failed to stop'));
  await new Promise(setImmediate);
  assert.equal(f.exits(), 0);
  assert.equal(f.errors.length, 1);
  f.app.quit();
  assert.equal(f.calls.length, 2);
  f.calls[1][3](null);
  await new Promise(setImmediate);
  assert.equal(f.exits(), 1);
});

test('closing a desktop attached to an independent server does not stop it', () => {
  const f = fixture(false);
  f.app.quit();
  assert.equal(f.exits(), 1);
  assert.equal(f.calls.length, 0);
});
