const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerProducerAttention, createProjectAttention } = require('../producer-attention.cjs');

function fixture(platform = 'win32', appOwned = false) {
  const window = new EventEmitter();
  const overlays = [], flashes = [];
  const icon = { native: true };
  let focused = false, destroyed = false, label = 'Waiting for your reply';
  const contents = new EventEmitter();
  Object.assign(window, {
    isDestroyed: () => destroyed, isFocused: () => focused,
    setOverlayIcon: (...args) => { assert.equal(destroyed, false); overlays.push(args); },
    flashFrame: value => { assert.equal(destroyed, false); flashes.push(value); },
  });
  Object.defineProperty(window, 'webContents', {
    get() {
      if (destroyed) throw new TypeError('Object has been destroyed');
      return contents;
    },
  });
  const attention = registerProducerAttention({ window, icon, description: () => label, platform, appOwned });
  return { window, contents, icon, attention, overlays, flashes,
    close: () => { destroyed = true; window.emit('closed'); },
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

for (const appOwned of [false, true]) {
  test(`closed-window cleanup avoids destroyed native properties (appOwned=${appOwned})`, () => {
    const f = fixture('win32', appOwned);
    f.attention.set(true);
    const overlays = [...f.overlays], flashes = [...f.flashes];
    assert.doesNotThrow(() => f.close());
    assert.equal(f.window.listenerCount('focus'), 0);
    assert.equal(f.window.listenerCount('closed'), 0);
    assert.equal(f.contents.listenerCount('did-start-navigation'), 0);
    assert.equal(f.contents.listenerCount('render-process-gone'), 0);
    f.attention.dispose();
    f.attention.set(true);
    f.attention.refresh();
    f.window.emit('focus');
    f.contents.emit('render-process-gone');
    assert.deepEqual(f.overlays, overlays);
    assert.deepEqual(f.flashes, flashes);
  });
}

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

test('app-owned attention survives shell reload and crash, with disposable listeners', () => {
  const f = fixture('win32', true);
  f.attention.set(true);
  f.window.webContents.emit('did-start-navigation', {}, '/', false, true);
  f.window.webContents.emit('render-process-gone');
  assert.equal(f.overlays.at(-1)[0], f.icon);
  assert.deepEqual(f.flashes, [true]);
  f.attention.dispose();
  f.attention.dispose();
  assert.deepEqual(f.overlays.at(-1), [null, '']);
  assert.equal(f.window.listenerCount('focus'), 0);
  assert.equal(f.window.listenerCount('closed'), 0);
  f.attention.set(true);
  assert.deepEqual(f.overlays.at(-1), [null, '']);
  const legacy = fixture();
  legacy.window.emit('closed');
  assert.equal(legacy.window.webContents.listenerCount('did-start-navigation'), 0);
  assert.equal(legacy.window.webContents.listenerCount('render-process-gone'), 0);
});

test('project attention follows transferred tabs and full snapshots without reflashing', () => {
  const source = fixture('win32', true), target = fixture('win32', true);
  const project = { root: 'D:/a', project_id: 'shared', epoch: 'one' };
  const windows = [{ windowId: 'source', tabIds: ['waiting', 'other'], activeTabId: 'other' },
    { windowId: 'target', tabIds: [] }];
  const tabs = [{ tabId: 'waiting', project },
    { tabId: 'other', project: { ...project, root: 'D:/b' } }];
  const handles = new Map([['source', source.attention], ['target', target.attention]]);
  const attention = createProjectAttention({ registry: { snapshot: () => ({ windows, tabs }) },
    getWindowAttention: id => handles.get(id) });
  const values = [{ project, waiting: [{ chat_id: 'same-chat', target: { chat_id: 'same-chat' } }] }];
  attention.replace(values);
  attention.replace(values);
  assert.equal(source.overlays.at(-1)[0], source.icon, 'inactive waiting tab contributes');
  assert.deepEqual(source.flashes, [true]);
  assert.equal(target.overlays.at(-1)[0], null);
  windows[0].tabIds = ['other'];
  windows[1].tabIds = ['waiting'];
  attention.refresh();
  assert.equal(source.overlays.at(-1)[0], null);
  assert.equal(target.overlays.at(-1)[0], target.icon);
  attention.refresh();
  assert.equal(target.flashes.filter(Boolean).length, 1);
  // A new window can be absent from the handle map until main finishes setup.
  windows.push({ windowId: 'new', tabIds: [] });
  attention.refresh();
  attention.replace([]);
  assert.equal(target.overlays.at(-1)[0], null, 'omitted projects clear the prior full snapshot');
});

test('equal project/chat IDs never substitute for strict root and epoch identity', () => {
  const f = fixture('win32', true);
  const project = { root: 'D:/a', project_id: 'same-id', epoch: 'current' };
  const attention = createProjectAttention({ registry: { snapshot: () => ({
    windows: [{ windowId: 'one', tabIds: ['tab'] }], tabs: [{ tabId: 'tab', project }],
  }) }, getWindowAttention: () => f.attention });
  const waiting = [{ chat_id: 'same-chat', target: { chat_id: 'same-chat' } }];
  for (const mismatch of [{ ...project, root: 'D:/b' }, { ...project, epoch: 'old' },
    { ...project, project_id: 'different-id' }]) {
    attention.replace([{ project: mismatch, waiting }]);
    assert.equal(f.overlays.at(-1)[0], null);
  }
  attention.replace([{ project: { ...project }, waiting }]);
  assert.equal(f.overlays.at(-1)[0], f.icon);
  attention.replace([{ project, waiting: [] }]);
  assert.equal(f.overlays.at(-1)[0], null);
});
