const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerWindowControls } = require('../window-controls.cjs');
function fixture() {
  const window = new EventEmitter();
  let maximized = false;
  const calls = [];
  const ipc = { handle: (_channel, fn) => { ipc.request = fn; }, removeHandler: () => calls.push('removed') };
  Object.assign(window, {
    webContents: { mainFrame: { url: 'http://127.0.0.1:8765/explorer' }, send: (...args) => calls.push(args) },
    isMaximized: () => maximized, isFocused: () => true, isDestroyed: () => false,
    minimize: () => calls.push('minimize'), close: () => calls.push('close'),
    maximize: () => { maximized = true; window.emit('maximize'); },
    unmaximize: () => { maximized = false; window.emit('unmaximize'); },
  });
  const tray = { hide: () => calls.push('tray'), setLocale: locale => calls.push(locale) };
  registerWindowControls(ipc, window, tray);
  const event = { sender: window.webContents, senderFrame: window.webContents.mainFrame };
  return { window, ipc, event, calls };
}
test('title bar actions preserve native state and the existing close handler', () => {
  const { ipc, event, calls, window } = fixture();
  assert.equal(ipc.request(event, 'toggle-maximize').maximized, true);
  assert.equal(ipc.request(event, 'toggle-maximize').maximized, false);
  ipc.request(event, 'minimize');
  ipc.request(event, 'close');
  assert.deepEqual(calls.slice(-2), ['minimize', 'close']);
  assert.equal(calls[0][0], 'studio:window-state');
  window.emit('closed');
  assert.equal(calls.at(-1), 'removed');
});
test('other windows, iframes, origins and unknown commands cannot control the window', () => {
  const { ipc, event, calls } = fixture();
  assert.throws(() => ipc.request({ ...event, sender: {} }, 'close'));
  assert.throws(() => ipc.request({ ...event, senderFrame: { url: event.senderFrame.url } }, 'close'));
  assert.throws(() => ipc.request(event, 'execute'));
  assert.throws(() => ipc.request(event, 'set-locale', 'invalid'));
  assert.throws(() => ipc.request(event, 'producer-attention', 'true'));
  assert.throws(() => ipc.request({ ...event, sender: {} }, 'producer-attention', true));
  assert.throws(() => ipc.request({ ...event, sender: {} }, 'minimize-to-tray'));
  event.senderFrame.url = 'https://example.com';
  assert.throws(() => ipc.request(event, 'close'));
  assert.deepEqual(calls, []);
});
test('tray and locale controls use the same trusted main-frame boundary', () => {
  const { ipc, event, calls } = fixture();
  ipc.request(event, 'minimize-to-tray');
  ipc.request(event, 'set-locale', 'ru');
  ipc.request(event, 'set-locale', 'en');
  assert.deepEqual(calls, ['tray', 'ru', 'en']);
});

function controllerFixture() {
  const requests = new Map();
  const removed = [];
  const ipc = { handle: (channel, fn) => { assert.equal(requests.has(channel), false); requests.set(channel, fn); },
    removeHandler: channel => { removed.push(channel); requests.delete(channel); } };
  const owners = new Map();
  const makeContents = () => ({ mainFrame: { url: 'http://127.0.0.1:8765/p/a/explorer' },
    sent: [], send(...args) { this.sent.push(args); }, isDestroyed() { return !!this.destroyed; } });
  const makeWindow = id => {
    const window = new EventEmitter();
    Object.assign(window, { id, webContents: makeContents(), maximized: false, minimized: 0,
      isDestroyed() { return !!this.destroyed; }, isMaximized() { return this.maximized; }, isFocused: () => true,
      minimize() { this.minimized++; }, close() { this.destroyed = true; this.emit('closed'); },
      maximize() { this.maximized = true; this.emit('maximize'); }, unmaximize() { this.maximized = false; this.emit('unmaximize'); } });
    owners.set(window.webContents, { role: 'shell', contents: window.webContents, window });
    return window;
  };
  const first = makeWindow(1), second = makeWindow(2), project = makeContents();
  owners.set(project, { role: 'project', contents: project, window: first });
  const controller = { authorize(event) {
    const owner = owners.get(event.sender);
    if (!owner) throw new Error('Untrusted sender');
    return owner;
  }, contentsForWindow: id => [...owners.values()].filter(x => String(x.window.id) === id).map(x => x.contents) };
  const api = registerWindowControls(ipc, controller);
  api.registerWindow(first); api.registerWindow(second);
  const event = contents => ({ sender: contents, senderFrame: contents.mainFrame });
  const invoke = (contents, action, value) => requests.get('studio:window')(event(contents), action, value);
  return { api, ipc, controller, requests, removed, owners, first, second, project, event, invoke, makeContents };
}

test('one controls handler follows the transferred contents and broadcasts only to current owners', () => {
  const f = controllerFixture();
  assert.equal(registerWindowControls(f.ipc, f.controller), f.api);
  f.invoke(f.project, 'minimize');
  assert.equal(f.first.minimized, 1);
  f.owners.get(f.project).window = f.second;
  f.invoke(f.project, 'minimize');
  assert.equal(f.second.minimized, 1);
  f.first.emit('focus');
  assert.equal(f.first.webContents.sent.length, 1);
  assert.equal(f.project.sent.length, 0);
  f.second.emit('focus');
  assert.equal(f.project.sent.length, 1);
  f.first.close();
  assert.deepEqual(f.removed, []);
  assert.equal(f.first.listenerCount('focus'), 0);
  f.invoke(f.project, 'toggle-maximize');
  assert.equal(f.second.maximized, true);
  f.second.close();
  assert.deepEqual(f.removed, []); // Zero windows is not app shutdown.
  f.api.dispose(); f.api.dispose();
  assert.deepEqual(f.removed, ['studio:window']);
});

test('controller controls reject foreign, stale, destroyed, nested frames and external URLs', () => {
  const f = controllerFixture();
  const request = f.requests.get('studio:window');
  assert.throws(() => f.invoke(f.makeContents(), 'close'), /Untrusted/);
  assert.throws(() => request({ ...f.event(f.project), senderFrame: { url: f.project.mainFrame.url } }, 'close'), /Untrusted/);
  f.project.mainFrame.url = 'https://example.com';
  assert.throws(() => f.invoke(f.project, 'close'), /Untrusted/);
  f.project.mainFrame.url = 'http://user:pass@127.0.0.1:8765/p/a';
  assert.throws(() => f.invoke(f.project, 'close'), /Untrusted/);
  f.project.mainFrame.url = 'http://127.0.0.1:8765/p/a';
  f.project.destroyed = true;
  assert.throws(() => f.invoke(f.project, 'close'), /Untrusted/);
  f.project.destroyed = false;
  f.owners.get(f.project).role = 'foreign';
  assert.throws(() => f.invoke(f.project, 'close'), /Untrusted/);
  f.owners.get(f.project).role = 'project';
  f.first.close();
  assert.throws(() => f.invoke(f.project, 'close'), /Untrusted/);
  f.api.dispose();
  assert.equal(f.second.listenerCount('focus'), 0);
  assert.throws(() => request(f.event(f.second.webContents), 'close'), /Untrusted/);
});

test('authorized exact non-HTTP shell documents can use native controls', () => {
  const f = controllerFixture();
  f.second.webContents.mainFrame.url = 'data:text/html,<title>Studio shell</title>';
  assert.equal(f.invoke(f.second.webContents, 'state').focused, true);
  f.api.dispose();
});

test('tray, locale and Producer attention follow the project current owner', () => {
  const f = controllerFixture(), calls = [];
  f.api.dispose();
  const api = registerWindowControls(f.ipc, f.controller);
  for (const window of [f.first, f.second]) api.registerWindow(window, {
    tray: { hide: () => calls.push([window.id, 'hide']), setLocale: value => calls.push([window.id, value]) },
    producerAttention: { set: value => calls.push([window.id, value]), refresh: () => calls.push([window.id, 'refresh']) },
  });
  f.invoke(f.project, 'producer-attention', true);
  f.owners.get(f.project).window = f.second;
  f.invoke(f.project, 'producer-attention', false);
  f.invoke(f.project, 'set-locale', 'ru');
  f.invoke(f.project, 'minimize-to-tray');
  assert.deepEqual(calls, [[1, true], [2, false], [2, 'ru'], [2, 'refresh'], [2, 'hide']]);
  api.dispose();
});
