'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { DesktopTabs } = require('../tabs.cjs');
const { createProjectNotifications, notificationTag, notificationText } = require('../project-notifications.cjs');
const en = require('../../frontend/src/locales/en.json');
const ru = require('../../frontend/src/locales/ru.json');

const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
}
const project = (name = 'A', epoch = 'current') => ({
  root: `/projects/${name}`, project_id: 'same-id', epoch, name,
});
const source = (name = 'A', epoch) => ({ project: project(name, epoch), source_seq: 7,
  target: '/tasks?task=same-task', notification: {
    trigger: 'task.created', values: { task: 'same-task', title: 'Authored title' },
  } });
const meta = { epoch: 'app-epoch', seq: 11 };

// Same injected Electron/ensureProject seams as tabs.test.cjs; real registry
// ownership, pending opens, transfer and activation gates remain under test.
function fixture(t, options = {}) {
  const windows = new Map(), views = [], ensures = [], routes = [], errors = [], natives = [], handlers = new Map();
  let active = null, writes = [], language = 'en';
  class Native extends EventEmitter {
    static isSupported() { return true; }
    constructor(copy) { super(); this.copy = copy; this.closed = deferred(); natives.push(this); }
    show() {}
    close() { this.emit('close'); this.closed.resolve(); }
  }
  const registry = new DesktopTabs({ canonicalizeRoot: async root => root,
    backend: { async ensureProject(request) {
      ensures.push(request); return project(request.path.split('/').at(-1));
    } },
    electron: {
      createProjectView(binding) {
        const contents = new EventEmitter(); contents.isDestroyed = () => false;
        const view = { webContents: contents, project: binding }; views.push(view); return view;
      },
      async loadProjectView() { await options.load?.(); },
      attachView() {}, parkView() {}, setBounds() {}, focusView() {}, destroyView() {},
      async waitForTransferAttach() { await options.transfer?.promise; return true; },
    },
  });
  function addWindow() {
    const window = new EventEmitter();
    Object.assign(window, { id: windows.size + 1, webContents: new EventEmitter(), focused: false,
      isDestroyed: () => false, isMinimized: () => false, isVisible: () => true,
      isFocused: () => window.focused, getContentSize: () => [900, 700],
      show() {}, focus() { active = window; window.focused = true; },
    });
    windows.set(String(window.id), window); registry.registerWindow(window); return window;
  }
  const controller = { registry, sessionReady: () => options.session?.promise,
    resolveRoot: async root => { if (root === '/missing') throw new Error('ENOENT'); return root; },
    getActiveWindow: () => active, ensureWindow: async () => addWindow(),
    getWindow: id => windows.get(String(id)),
    openProject: (root, windowId, extra) => registry.openProject(root, { windowId, ...extra }),
    waitForProjectPage: () => options.page?.promise,
    bridge: {
      authorize(event) { if (event !== authorized) throw new Error('Unauthorized'); },
      publishState() {},
      async restorePage(tabId, page) {
        const tab = registry.getTab(tabId);
        assert.equal(registry.getWindow(tab.ownerWindowId).activeTabId, tabId);
        routes.push({ tab, page }); return { status: 'restored' };
      },
    },
  };
  const authorized = {};
  const notifications = createProjectNotifications({ controller, Notification: Native,
    ipcMain: { handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) },
    preference: { read: () => options.enabled ?? true, write(value) { options.write?.(value); writes.push(value); } },
    messages: () => language === 'en' ? en : ru, locale: () => language,
    onError: error => errors.push(error),
  });
  t.after(() => { notifications.dispose(); registry.dispose(); });
  return { registry, controller, notifications, windows, addWindow, views, ensures, routes, errors, natives,
    handlers, authorized, writes, language: value => { language = value; } };
}

test('zero-window native duplicate clicks open one project and route once', async t => {
  const f = fixture(t);
  f.notifications.notify(source(), meta);
  const native = f.natives[0];
  native.emit('click'); native.emit('click');
  await native.closed.promise;
  assert.equal(f.windows.size, 1);
  assert.deepEqual(f.ensures, [{ path: '/projects/A', create: false }]);
  assert.equal(f.views.length, 1);
  assert.equal(f.routes.length, 1);
  assert.deepEqual(f.routes[0].page, { path: '/tasks', query: { task: 'same-task' } });
  assert.deepEqual(f.errors, []);
});

test('stale notification identity resolves its root, not visible B, and follows transferred owner', async t => {
  const transfer = deferred();
  const f = fixture(t, { transfer }), w1 = f.addWindow(), w2 = f.addWindow();
  const a = await f.controller.openProject('/projects/A', String(w1.id), { create: false });
  const b = await f.controller.openProject('/projects/B', String(w1.id), { create: false });
  // Notification was captured before A acquired its current epoch.
  f.notifications.notify(source('A', 'obsolete'), meta);
  assert.equal(f.natives.length, 1, 'foreground B must not suppress A');
  const sender = w1.webContents;
  const { token } = f.registry.beginTransfer(a.tabId, { sender, windowId: '1',
    windowRevision: f.registry.getWindow('1').revision, tabRevision: f.registry.getTab(a.tabId).revision });
  const moved = f.registry.transferTab(token, { sender, targetWindowId: '2',
    targetWindowRevision: f.registry.getWindow('2').revision, index: 0 });
  await tick();
  w1.focus(); w2.focused = false; // B is visible when the old native notification is clicked.
  f.natives[0].emit('click'); await tick();
  assert.equal(f.routes.length, 0, 'click waits for committed transfer ownership');
  transfer.resolve(); await moved; await f.natives[0].closed.promise;
  assert.equal(f.routes[0].tab.tabId, a.tabId);
  assert.equal(f.routes[0].tab.ownerWindowId, '2');
  assert.equal(f.routes[0].tab.project.epoch, 'current');
  assert.equal(f.registry.getWindow('1').activeTabId, b.tabId);
  assert.equal(f.views.length, 2, 'activation never reloads or duplicates A');
  assert.equal(f.ensures.length, 2);
  assert.deepEqual(f.errors, []);
  // Closing A is UI-only. An older native locator reopens its resident root in
  // the currently available shell while B remains intact, without a task API.
  f.registry.closeTab(a.tabId); w1.focus(); w2.focused = false;
  f.notifications.notify(source('A', 'obsolete'), { ...meta, seq: 12 });
  const reopened = f.natives.at(-1);
  reopened.emit('click'); await reopened.closed.promise;
  assert.equal(f.registry.snapshot().tabs.filter(tab => tab.project.root === '/projects/A').length, 1);
  assert.equal(f.routes.at(-1).tab.ownerWindowId, '1');
  assert.equal(f.registry.getTab(b.tabId).project.root, '/projects/B');
  assert.equal(f.ensures.length, 3);
});

test('restore/open race coalesces one renderer and routes only after project page ready', async t => {
  const session = deferred(), load = deferred(), page = deferred();
  const f = fixture(t, { session, load: () => load.promise, page });
  const activation = f.notifications.activate(source());
  assert.strictEqual(f.notifications.activate(source()), activation, 'concurrent activation is coalesced');
  await tick(); assert.equal(f.windows.size, 0); assert.equal(f.ensures.length, 0);
  const window = f.addWindow();
  const restored = f.controller.openProject('/projects/A', String(window.id), { create: false });
  window.focus(); session.resolve(); await tick();
  assert.equal(f.ensures.length, 1); assert.equal(f.views.length, 1);
  assert.equal(f.routes.length, 0);
  load.resolve(); const tab = await restored; await tick();
  assert.equal(f.routes.length, 0, 'loaded view alone does not imply page IPC readiness');
  page.resolve(); const result = await activation;
  assert.equal(result.tabId, tab.tabId); assert.equal(f.routes.length, 1);
  assert.equal(f.views.length, 1); assert.equal(f.ensures.length, 1);
});

test('native IDs and dedup scope include root and epoch despite equal source/entity IDs', t => {
  const f = fixture(t), a = source(), b = source('B');
  assert.notEqual(notificationTag(a, meta), notificationTag(b, meta));
  f.notifications.notify(a, meta); f.notifications.notify(a, meta); f.notifications.notify(b, meta);
  assert.equal(f.natives.length, 2);
  assert.equal(new Set(f.natives.map(item => item.copy.id)).size, 2);
  assert.match(f.natives[0].copy.title, /^\[A\] /);
  assert.match(f.natives[1].copy.title, /^\[B\] /);
  f.notifications.notify(source('A', 'new-epoch'), meta);
  assert.equal(f.natives.length, 3);
  assert.equal(new Set(f.natives.map(item => item.copy.id)).size, 3);
});

test('missing project root never creates a shell or falls back to visible B', async t => {
  const f = fixture(t), window = f.addWindow();
  const b = await f.controller.openProject('/projects/B', String(window.id), { create: false });
  for (const root of [undefined, '', 'relative', 'D:relative', '/missing']) {
    await assert.rejects(f.notifications.activate({ ...source(), project: { ...project(), root } }));
  }
  assert.equal(f.windows.size, 1); assert.equal(f.views.length, 1); assert.equal(f.ensures.length, 1);
  assert.equal(f.registry.getWindow('1').activeTabId, b.tabId);
  assert.deepEqual(f.routes, []);
});

test('native copy reuses EN/RU notification vocabulary and preserves authored text', t => {
  const f = fixture(t);
  f.notifications.notify(source(), meta);
  assert.equal(f.natives[0].copy.title, `[A] ${en.automations.notification.createdTitle.replace('{task}', 'same-task')}`);
  f.language('ru'); f.notifications.notify(source('B'), meta);
  assert.equal(f.natives[1].copy.title, `[B] ${ru.automations.notification.createdTitle.replace('{task}', 'same-task')}`);
  assert.equal(f.natives[1].copy.body, 'Authored title');
  const changed = { trigger: 'path.changed', values: { count: '2', folder: './art' } };
  assert.equal(notificationText(changed, en, 'en').body, '2 files');
  assert.equal(notificationText(changed, ru, 'ru').body, '2 файла');
  const status = { trigger: 'task.status_changed', values: { task: 'VS-62', status: 'done' } };
  assert.equal(notificationText(status, ru, 'ru').title, `VS-62 → ${ru.status.done}`);
  assert.deepEqual(notificationText({ ...changed, title: 'Custom', message: 'Untranslated' }, ru, 'ru'),
    { title: 'Custom', body: 'Untranslated' });
});

test('preference IPC authorizes before read/write and persists before changing state', t => {
  let fail = false;
  const f = fixture(t, { enabled: false, write() { if (fail) throw new Error('Write failed'); } });
  const ipc = f.handlers.get('studio:notifications');
  assert.throws(() => ipc({}, 'set-enabled', true), /Unauthorized/);
  assert.deepEqual(f.writes, []);
  assert.deepEqual(ipc(f.authorized, 'state'), { supported: true, enabled: false });
  assert.throws(() => ipc(f.authorized, 'set-enabled', 'true'), /Invalid/);
  assert.throws(() => ipc(f.authorized, 'state', true), /Invalid/);
  assert.deepEqual(ipc(f.authorized, 'set-enabled', true), { supported: true, enabled: true });
  fail = true;
  assert.throws(() => ipc(f.authorized, 'set-enabled', false), /Write failed/);
  assert.equal(f.notifications.state().enabled, true);
  assert.deepEqual(f.writes, [true]);
});
