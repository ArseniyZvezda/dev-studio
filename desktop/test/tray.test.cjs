const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerTray } = require('../tray.cjs');
function fixture() {
  const app = new EventEmitter();
  app.quit = () => calls.push('quit');
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
  const tray = registerTray({ app, Tray, Menu: { buildFromTemplate: value => value },
    activate: () => {
      if (window.isDestroyed()) { destroyed = false; calls.push('create'); }
      if (window.isMinimized()) window.restore();
      window.show(); window.focus();
    },
    hideWindow: () => { if (!window.isDestroyed()) window.hide(); },
    icon: {}, productName: 'Voxarium Studio',
    messages: locale => require(`../../frontend/src/locales/${locale || 'en'}.json`).desktop,
  });
  return { app, window, tray, nativeTray, calls, menu: () => menu,
    minimize: () => { minimized = true; }, close: () => { destroyed = true; window.emit('closed'); } };
}
test('tray hides without closing, restores on click and retains maximized window geometry', async () => {
  const f = fixture();
  f.tray.hide();
  assert.deepEqual(f.calls, ['hide']);
  f.nativeTray.emit('click');
  await f.tray.show();
  assert.deepEqual(f.calls.slice(1), ['show', 'focus']);
  f.minimize();
  f.nativeTray.emit('double-click');
  await f.tray.show();
  assert.deepEqual(f.calls.slice(-3), ['restore', 'show', 'focus']);
});
test('second launch restores a hidden window; explicit quit is not native close', async () => {
  const f = fixture();
  f.app.emit('second-instance');
  await f.tray.show();
  assert.deepEqual(f.calls, ['show', 'focus']);
  f.menu().at(-1).click();
  assert.deepEqual(f.calls, ['show', 'focus', 'quit']);
  assert.equal(f.nativeTray.destroyed, false); // Disposal waits for app teardown.
});
test('tray survives zero windows; menu follows locale and exit releases native resources', async () => {
  const f = fixture();
  f.tray.setLocale('ru');
  assert.equal(f.menu()[0].label, 'Открыть Voxarium Studio');
  f.tray.setLocale('en');
  assert.equal(f.menu()[0].label, 'Open Voxarium Studio');
  f.close();
  assert.equal(f.nativeTray.destroyed, false);
  assert.equal(f.app.listenerCount('second-instance'), 1);
  f.app.emit('second-instance');
  await f.tray.show();
  assert.deepEqual(f.calls, ['create', 'show', 'focus']);
  f.app.emit('will-quit');
  f.tray.hide();
  await f.tray.show();
  assert.deepEqual(f.calls, ['create', 'show', 'focus', 'destroy']);
  assert.equal(f.app.listenerCount('second-instance'), 0);
});

test('repeated click/double-click/second-instance share an in-flight activation', async () => {
  const f = fixture();
  f.close();
  f.nativeTray.emit('click'); f.nativeTray.emit('double-click'); f.app.emit('second-instance');
  await f.tray.show();
  assert.deepEqual(f.calls, ['create', 'show', 'focus']);
  f.tray.dispose(); f.tray.dispose();
  assert.equal(f.calls.filter(call => call === 'destroy').length, 1);
});
test('normal quit and updater quit both dispose the tray', () => {
  const f = fixture();
  f.app.emit('will-quit');
  assert.deepEqual(f.calls, ['destroy']);
  f.tray.hide();
  assert.deepEqual(f.calls, ['destroy']);
});
