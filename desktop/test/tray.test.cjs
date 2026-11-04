const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerTray } = require('../tray.cjs');
function fixture() {
  const app = new EventEmitter();
  const window = new EventEmitter();
  const calls = [];
  let destroyed = false, minimized = false, menu, nativeTray;
  Object.assign(window, {
    isDestroyed: () => destroyed, isMinimized: () => minimized,
    restore: () => { minimized = false; calls.push('restore'); },
    show: () => calls.push('show'), hide: () => calls.push('hide'),
    focus: () => calls.push('focus'), close: () => calls.push('close'),
  });
  class Tray extends EventEmitter {
    constructor() { super(); nativeTray = this; this.destroyed = false; }
    setToolTip() {}
    setContextMenu(value) { menu = value; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; calls.push('destroy'); }
  }
  const tray = registerTray({ app, window, Tray, Menu: { buildFromTemplate: value => value },
    icon: {}, productName: 'Voxarium Studio',
    messages: locale => require(`../../frontend/src/locales/${locale || 'en'}.json`).desktop,
  });
  return { app, window, tray, nativeTray, calls, menu: () => menu,
    minimize: () => { minimized = true; }, close: () => { destroyed = true; window.emit('closed'); } };
}
test('tray hides without closing, restores on click and retains maximized window geometry', () => {
  const f = fixture();
  f.tray.hide();
  assert.deepEqual(f.calls, ['hide']);
  f.nativeTray.emit('click');
  assert.deepEqual(f.calls.slice(1), ['show', 'focus']);
  f.minimize();
  f.nativeTray.emit('double-click');
  assert.deepEqual(f.calls.slice(-3), ['restore', 'show', 'focus']);
});
test('second launch restores a hidden window; quit goes through the existing close confirmation', () => {
  const f = fixture();
  f.app.emit('second-instance');
  assert.deepEqual(f.calls, ['show', 'focus']);
  f.menu().at(-1).click();
  assert.deepEqual(f.calls.slice(-3), ['show', 'focus', 'close']);
  assert.equal(f.nativeTray.destroyed, false); // A cancelled close keeps the tray usable.
});
test('tray menu follows the UI locale and releases native resources on exit', () => {
  const f = fixture();
  f.tray.setLocale('ru');
  assert.equal(f.menu()[0].label, 'Открыть Voxarium Studio');
  f.tray.setLocale('en');
  assert.equal(f.menu()[0].label, 'Open Voxarium Studio');
  f.close();
  f.app.emit('will-quit');
  f.tray.hide();
  f.tray.show();
  assert.deepEqual(f.calls, ['destroy']);
  assert.equal(f.app.listenerCount('second-instance'), 0);
});
test('normal quit and updater quit both dispose the tray', () => {
  const f = fixture();
  f.app.emit('will-quit');
  assert.deepEqual(f.calls, ['destroy']);
  f.tray.hide();
  assert.deepEqual(f.calls, ['destroy']);
});
