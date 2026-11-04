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
