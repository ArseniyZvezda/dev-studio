const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerProducerAttention } = require('../producer-attention.cjs');

function fixture(platform = 'win32') {
  const window = new EventEmitter();
  const overlays = [], flashes = [];
  const icon = { native: true };
  let focused = false, label = 'Waiting for your reply';
  Object.assign(window, {
    webContents: new EventEmitter(), isDestroyed: () => false, isFocused: () => focused,
    setOverlayIcon: (...args) => overlays.push(args), flashFrame: value => flashes.push(value),
  });
  const attention = registerProducerAttention({ window, icon, description: () => label, platform });
  return { window, icon, attention, overlays, flashes,
    focus: () => { focused = true; window.emit('focus'); },
    translate: () => { label = 'Ожидает ответа'; attention.refresh(); },
  };
}

test('attention survives focus and locale changes, and clears only when answered', () => {
  const f = fixture();
  f.attention.set(true);
  assert.deepEqual(f.overlays.at(-1), [f.icon, 'Waiting for your reply']);
  assert.deepEqual(f.flashes, [true]);
  f.attention.set(true);
  assert.deepEqual(f.flashes, [true], 'repeated WS snapshots do not flash again');
  f.focus();
  assert.equal(f.flashes.at(-1), false);
  assert.equal(f.overlays.at(-1)[0], f.icon);
  f.translate();
  assert.deepEqual(f.overlays.at(-1), [f.icon, 'Ожидает ответа']);
  f.attention.set(false);
  assert.deepEqual(f.overlays.at(-1), [null, '']);
});

test('reload, project navigation and a crashed renderer cannot retain a stale badge', () => {
  const f = fixture();
  f.attention.set(true);
  f.window.webContents.emit('did-start-navigation', {}, '/tasks', true, true);
  assert.equal(f.overlays.at(-1)[0], f.icon, 'SPA navigation is not a reset');
  f.window.webContents.emit('did-start-navigation', {}, 'about:blank', false, false);
  assert.equal(f.overlays.at(-1)[0], f.icon, 'subframes do not reset attention');
  f.window.webContents.emit('did-start-navigation', {}, '/', false, true);
  assert.deepEqual(f.overlays.at(-1), [null, '']);
  f.attention.set(true);
  f.window.webContents.emit('render-process-gone');
  assert.deepEqual(f.overlays.at(-1), [null, '']);
});

test('foreground attention is passive and other platforms do not call Windows APIs', () => {
  const foreground = fixture();
  foreground.focus();
  foreground.attention.set(true);
  assert.equal(foreground.overlays.at(-1)[0], foreground.icon);
  assert.deepEqual(foreground.flashes, [false]);
  const other = fixture('linux');
  other.attention.set(true);
  other.focus();
  other.attention.set(false);
  assert.deepEqual(other.overlays, []);
  assert.deepEqual(other.flashes, []);
});
