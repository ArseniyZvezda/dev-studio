'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { registerTabsIpc, createShellServices, TABS_CHANNELS: C } = require('../tabs-ipc.cjs');
const { DesktopTabs, createElectronAdapter } = require('../tabs.cjs');

const SHELL = pathToFileURL(path.join(__dirname, '..', 'shell.html')).href;
const binding = name => ({ root: `/projects/${name}`, project_id: `project-${name}`, epoch: 'epoch-1' });
class Contents extends EventEmitter {
  constructor(url) { super(); this.mainFrame = { url }; this.destroyed = false; this.sent = []; this.id = 42; }
  isDestroyed() { return this.destroyed; }
  getURL() { return this.mainFrame.url; }
  navigate(url) { this.mainFrame = { url }; }
  send(...message) { this.sent.push(message); }
  close() { if (!this.destroyed) { this.destroyed = true; this.emit('destroyed'); } }
  async loadURL(url) { this.navigate(url); }
  async executeJavaScript() { return true; }
  setWindowOpenHandler() {}
  focus() {}
}
class Window extends EventEmitter {
  constructor(id) {
    super(); this.id = id; this.webContents = new Contents(SHELL); this.destroyed = false;
    this.contentView = { children: [],
      addChildView(view) { if (!this.children.includes(view)) this.children.push(view); },
      removeChildView(view) { this.children = this.children.filter(child => child !== view); } };
  }
  isDestroyed() { return this.destroyed; }
  close() { this.destroyed = true; this.emit('closed'); }
  getContentSize() { return [800, 600]; }
  isMinimized() { return false; }
  show() {}
  focus() {}
}
const event = contents => ({ sender: contents, senderFrame: contents.mainFrame });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
function ipcFixture() {
  const handlers = new Map(), removed = [];
  return { handlers, removed,
    handle(channel, handler) { if (handlers.has(channel)) throw new Error('Handler already installed'); handlers.set(channel, handler); },
    removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    request(channel, contents, ...args) { return handlers.get(channel)(event(contents), ...args); },
  };
}
function fixture(options = {}) {
  const ipc = ipcFixture(), windows = new Map(), tabs = new Map(), calls = [];
  const registry = {
    getWindow: id => windows.get(String(id)) ?? null,
    getTab: id => tabs.get(id) ?? null,
    snapshot: () => ({ windows: [...windows.values()], tabs: [...tabs.values()] }),
    async openProject(root, request) {
      calls.push(['open', root, request]);
      const known = [...tabs.values()].find(tab => tab.project.root === root);
      if (known) return this.activateTab(known.tabId);
      return addTab(root.split('/').at(-1), request.windowId).tab;
    },
    activateTab(id) {
      calls.push(['activate', id]);
      const tab = tabs.get(id), window = windows.get(tab.ownerWindowId);
      if (window.activeTabId !== id) { window.activeTabId = id; window.revision++; }
      return tab;
    },
  };
  const controller = registerTabsIpc({ ipcMain: ipc, registry, ...options });
  function addWindow(id) {
    const window = new Window(id);
    windows.set(String(id), { windowId: String(id), revision: 0, tabIds: [], activeTabId: null,
      insets: { top: 48, right: 0, bottom: 0, left: 0 }, token: 'SECRET', native: window });
    const release = controller.registerWindow(window);
    return { window, release };
  }
  const { window: w1, release: release1 } = addWindow(1), { window: w2 } = addWindow(2);
  function addTab(name, windowId = '1', insert = true) {
    const project = binding(name), view = { webContents: new Contents(`http://127.0.0.1:8765/p/${project.project_id}/`) };
    const release = controller.registerProjectView(view, project);
    const tab = { tabId: `tab-${name}`, project, ownerWindowId: windowId, revision: 1,
      transferGeneration: 0, state: 'ready', token: 'SECRET', view };
    function commit() {
      tabs.set(tab.tabId, tab); const window = windows.get(windowId);
      window.tabIds.push(tab.tabId); window.revision++;
    }
    if (insert) commit();
    return { tab, view, release, commit };
  }
  const windowRequest = (id = '1') => ({ windowId: id, windowRevision: windows.get(id).revision });
  const tabRequest = tab => ({ ...windowRequest(tab.ownerWindowId), tabId: tab.tabId, tabRevision: tab.revision });
  return { ipc, registry, controller, windows, tabs, calls, w1, w2, release1, addTab, addWindow, windowRequest, tabRequest };
}

async function liveFixture({ coreOptions = {}, ...options } = {}) {
  const ipc = ipcFixture(), views = [], nativeCalls = [], created = [], releases = new Map();
  let controller;
  class View {
    constructor() { this.webContents = new Contents('about:blank'); views.push(this); }
    setVisible() {}
    setBounds() {}
  }
  const electron = createElectronAdapter({ WebContentsView: View, configureProjectView(view, project) {
    return controller.registerProjectView(view, project);
  } });
  const registry = new DesktopTabs({ backend: { ensureProject: async ({ path: root }) => {
    nativeCalls.push(['ensure', root]); return binding(root.split('/').at(-1));
  } }, electron, canonicalizeRoot: async root => root, createShell: async () => {
    const window = new Window(3 + created.length); created.push(window);
    registry.registerWindow(window); releases.set(String(window.id), controller.registerWindow(window)); return window;
  }, ...coreOptions });
  controller = registerTabsIpc({ ipcMain: ipc, registry, ...options });
  const w1 = new Window(1), w2 = new Window(2);
  for (const window of [w1, w2]) {
    registry.registerWindow(window); releases.set(String(window.id), controller.registerWindow(window));
  }
  const open = (name, windowId = '1') => registry.openProject(binding(name).root, { windowId });
  const windowRequest = (id = '1') => ({ windowId: id, windowRevision: registry.getWindow(id).revision });
  const tabRequest = id => {
    const tab = registry.getTab(typeof id === 'string' ? id : id.tabId);
    return { ...windowRequest(tab.ownerWindowId), tabId: tab.tabId, tabRevision: tab.revision };
  };
  const transferRequest = (tab, token, targetWindowId = '2') => ({ ...tabRequest(tab), token,
    targetWindowId, targetWindowRevision: registry.getWindow(targetWindowId).revision });
  const dispose = () => { controller.dispose(); registry.dispose(); };
  return { ipc, registry, controller, w1, w2, views, nativeCalls, created, releases, open, windowRequest, tabRequest, transferRequest, dispose };
}

test('transfer, detach and reorder reject non-shell, foreign, stale and malformed requests before core', async () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2'), calls = [];
  for (const name of ['beginTransfer', 'transferTab', 'detachTab', 'reorderTab']) {
    f.registry[name] = (...args) => { calls.push([name, args]); throw new Error('Unexpected core call'); };
  }
  for (const channel of [C.transferBegin, C.transfer, C.detach, C.reorder]) {
    const good = { ...f.tabRequest(a.tab), ...(channel === C.transfer
      ? { token: 'one-shot', targetWindowId: '2', targetWindowRevision: 1 } : {}),
    ...(channel === C.reorder ? { index: 0 } : {}) };
    const bad = [null, [], {}, Object.create(good), { ...good, extra: true },
      { ...good, windowId: '2' }, { ...good, windowRevision: 0 }, { ...good, tabRevision: 0 },
      { ...good, tabId: b.tab.tabId }, { ...good, tabRevision: '1' }, { ...good, tabId: '' },
      { ...good, windowRevision: Number.MAX_SAFE_INTEGER + 1 }];
    if ([C.reorder, C.transfer].includes(channel)) bad.push(...[-1, 0.5, '0', undefined, NaN, Infinity,
      Number.MAX_SAFE_INTEGER + 1].map(index => ({ ...good, index })));
    if (channel === C.transfer) bad.push(...[
      { token: '' }, { token: undefined }, { token: 'x'.repeat(129) },
      { targetWindowId: 'missing' }, { targetWindowRevision: 0 }, { targetWindowRevision: '1' },
      { targetWindowId: 2 }, { targetWindowRevision: -1 },
    ].map(patch => ({ ...good, ...patch })));
    for (const request of bad) await assert.rejects(async () => f.ipc.request(channel, f.w1.webContents, request));
    await assert.rejects(async () => f.ipc.request(channel, a.view.webContents, good), /Shell/);
    await assert.rejects(async () => f.ipc.request(channel, f.w2.webContents, good), /Foreign window/);
    await assert.rejects(async () => f.ipc.request(channel, f.w1.webContents, good, 'extra'));
  }
  assert.deepEqual(calls, []); f.controller.dispose();
});

test('transfer IPC pins native sender, revalidates at commit, and projects only final public state', async () => {
  const gate = deferred(), f = fixture(), a = f.addTab('A'), shell = f.w1.webContents;
  const source = f.tabRequest(a.tab), request = { ...source, token: 'one-shot',
    targetWindowId: '2', targetWindowRevision: 0, index: 0 };
  // The core API does not receive tabId inside the options object.
  f.registry.beginTransfer = (id, input) => {
    assert.equal(id, a.tab.tabId);
    const { tabId, ...expected } = source;
    assert.deepEqual(input, { ...expected, sender: shell, senderFrame: shell.mainFrame });
    return { token: 'one-shot', expiresAt: 1234, native: shell, secret: 'SECRET' };
  };
  assert.deepEqual(f.ipc.request(C.transferBegin, shell, source), { token: 'one-shot', expiresAt: 1234 });
  assert.equal(shell.sent.length, 0, 'issuing a token is not a state mutation');
  f.registry.transferTab = async (token, input) => {
    assert.equal(token, 'one-shot'); assert.equal(input.sender, shell);
    assert.equal(input.senderFrame, shell.mainFrame);
    assert.equal(input.targetWindowId, '2'); assert.equal(input.targetWindowRevision, 0); assert.equal(input.index, 0);
    a.tab.state = 'transferring'; f.controller.publishState();
    await gate.promise; input.validateSender();
    f.windows.get('1').tabIds = []; f.windows.get('1').revision++;
    f.windows.get('2').tabIds.push(a.tab.tabId); f.windows.get('2').revision++;
    a.tab.ownerWindowId = '2'; a.tab.revision++; a.tab.transferGeneration++; a.tab.state = 'ready';
    return a.tab;
  };
  const pending = f.ipc.request(C.transfer, shell, request);
  assert.equal(shell.sent.length, 0, 'no tentative transferring state is published');
  request.tabRevision = 999; // IPC captures assertions before awaiting core.
  gate.resolve(); const result = await pending;
  assert.equal(result.ownerWindowId, '2'); assert.equal(result.token, undefined); assert.equal(result.view, undefined);
  assert.equal(shell.sent.length, 1); assert.deepEqual(shell.sent[0][1].tabs, []);
  assert.equal(f.ipc.request(C.state, a.view.webContents).windowId, '2');
  assert.throws(() => f.ipc.request(C.activate, shell, { ...f.windowRequest(), tabId: a.tab.tabId,
    tabRevision: a.tab.revision }), /Foreign tab/);
  f.controller.dispose();
});

test('late transfer/detach authorization rejects stale frame, URL, role registration and owner before commit', async () => {
  for (const channel of [C.transfer, C.detach]) {
    for (const change of [f => f.w1.webContents.navigate(SHELL),
      f => f.w1.webContents.navigate('http://127.0.0.1:8765/'), f => f.release1(),
      f => f.w1.webContents.close(), (f, a) => { a.tab.ownerWindowId = '2'; },
      f => { f.windows.get('1').revision++; }, (_f, a) => { a.tab.revision++; }]) {
      const gate = deferred(), f = fixture(), a = f.addTab('A');
      let committed = false;
      f.registry[channel === C.transfer ? 'transferTab' : 'detachTab'] = async (...args) => {
        await gate.promise; args[1].validateSender(); committed = true; return a.tab;
      };
      const pending = f.ipc.request(channel, f.w1.webContents, { ...f.tabRequest(a.tab),
        ...(channel === C.transfer ? { token: 'one-shot', targetWindowId: '2', targetWindowRevision: 0 } : {}) });
      change(f, a); gate.resolve(); await assert.rejects(pending);
      assert.equal(committed, false); f.controller.dispose();
    }
  }
});

test('detach passes source assertions and publishes rollback only after the core promise settles', async () => {
  const gate = deferred(), f = fixture(), a = f.addTab('A'), source = f.tabRequest(a.tab);
  f.registry.detachTab = async (id, input) => {
    assert.equal(id, a.tab.tabId); const { tabId, ...expected } = source;
    assert.deepEqual({ ...input, validateSender: undefined },
      { ...expected, sender: f.w1.webContents, senderFrame: f.w1.webContents.mainFrame, validateSender: undefined });
    input.validateSender(); a.tab.state = 'transferring';
    f.controller.publishState(); await gate.promise;
    a.tab.state = 'ready'; a.tab.revision++; throw new Error('native attach failed; rolled back');
  };
  const pending = f.ipc.request(C.detach, f.w1.webContents, source);
  a.view.webContents.emit('did-stop-loading'); await Promise.resolve();
  assert.equal(f.w1.webContents.sent.length, 0); gate.resolve(); await assert.rejects(pending, /rolled back/);
  assert.equal(f.w1.webContents.sent.length, 1);
  assert.equal(f.w1.webContents.sent[0][1].tabs[0].state, 'ready');
  assert.equal(f.w1.webContents.sent[0][1].tabs[0].ownerWindowId, '1'); f.controller.dispose();
});

test('reorder passes only source revisions/index, returns public snapshot and publishes even after rollback', () => {
  const f = fixture(), a = f.addTab('A'), source = { ...f.tabRequest(a.tab), index: 0 };
  f.registry.reorderTab = (id, input) => {
    assert.equal(id, a.tab.tabId); const { tabId, ...expected } = source;
    assert.deepEqual(input, expected); return a.tab;
  };
  const result = f.ipc.request(C.reorder, f.w1.webContents, source);
  assert.equal(result.tabId, a.tab.tabId); assert.equal(result.view, undefined); assert.equal(result.token, undefined);
  f.registry.reorderTab = () => { throw new Error('rolled back'); };
  assert.throws(() => f.ipc.request(C.reorder, f.w1.webContents, source), /rolled back/);
  assert.equal(f.w1.webContents.sent.length, 2); f.controller.dispose();
});

test('close seam holds the registry barrier during callback and final authorization, then releases on error', async () => {
  let barrier = false, validations = 0;
  const gate = deferred(), f = fixture({ prepareClose: async () => {
    assert.equal(barrier, true); await gate.promise; return { prepared: true };
  } }), a = f.addTab('A');
  f.registry.withCloseBarrier = async callback => {
    assert.equal(barrier, false); barrier = true;
    try { return await callback(); } finally { barrier = false; }
  };
  const getWindow = f.registry.getWindow;
  f.registry.getWindow = id => { if (barrier) validations++; return getWindow(id); };
  const pending = f.ipc.request(C.prepareClose, f.w1.webContents, { ...f.tabRequest(a.tab), target: 'tab' });
  assert.equal(barrier, true); const before = validations;
  f.w1.webContents.navigate(SHELL); gate.resolve();
  await assert.rejects(pending, /Untrusted/); assert.equal(barrier, false);
  assert.ok(before > 0, 'validation immediately before callback is inside the barrier');
  f.controller.dispose();
});

test('real registry live transfer consumes token once and changes project authority to the new owner', async () => {
  const f = await liveFixture(), a = await f.open('A'), b = await f.open('B', '2');
  try {
    const source = f.tabRequest(a), { token } = f.ipc.request(C.transferBegin, f.w1.webContents, source);
    const request = f.transferRequest(a, token), contents = f.views[0].webContents;
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w2.webContents, request), /Foreign window/);
    const result = await f.ipc.request(C.transfer, f.w1.webContents, request);
    assert.equal(result.ownerWindowId, '2'); assert.equal(result.transferGeneration, a.transferGeneration + 1);
    assert.equal(f.views.length, 2); assert.equal(f.nativeCalls.length, 2, 'no reopen/backend ensure on transfer');
    assert.equal(f.controller.authorize(event(contents)).window, f.w2);
    assert.ok(!f.controller.contentsForWindow('1').includes(contents));
    assert.ok(f.controller.contentsForWindow('2').includes(contents));
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents, request));
    // Even with fresh revisions/new owning shell, a consumed capability cannot replay.
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w2.webContents,
      f.transferRequest(a, token, '1')));
    assert.throws(() => f.ipc.request(C.activate, f.w1.webContents,
      { ...f.windowRequest(), tabId: a.tabId, tabRevision: result.revision }), /Foreign tab/);
    assert.equal(f.ipc.request(C.activate, f.w2.webContents, f.tabRequest(a)).tabId, a.tabId);
    assert.equal(f.ipc.request(C.state, contents).windowId, '2');
    assert.equal(f.registry.getTab(b.tabId).ownerWindowId, '2');
  } finally { f.dispose(); }
});

test('real registry rejects stale tokens/revisions and reorder performs no project navigation', async () => {
  const f = await liveFixture(), a = await f.open('A'), b = await f.open('B');
  try {
    const source = f.tabRequest(a), { token } = f.ipc.request(C.transferBegin, f.w1.webContents, source);
    const request = f.transferRequest(a, token), view = f.views[0], frame = view.webContents.mainFrame;
    const reordered = f.ipc.request(C.reorder, f.w1.webContents, { ...source, index: 1 });
    assert.equal(reordered.ownerWindowId, '1');
    assert.deepEqual(f.registry.getWindow('1').tabIds, [b.tabId, a.tabId]);
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents, request), /Stale/);
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, token)));
    assert.equal(view.webContents.mainFrame, frame); assert.equal(f.views.length, 2); assert.equal(f.nativeCalls.length, 2);
  } finally { f.dispose(); }
});

test('real registry close barrier excludes IPC transfer/detach until the dirty callback settles', async () => {
  const gate = deferred(), f = await liveFixture({ prepareClose: () => gate.promise }), a = await f.open('A');
  try {
    const { token } = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const request = f.transferRequest(a, token);
    const closing = f.ipc.request(C.prepareClose, f.w1.webContents, { ...f.tabRequest(a), target: 'tab' });
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents, request));
    await assert.rejects(async () => f.ipc.request(C.detach, f.w1.webContents, f.tabRequest(a)));
    assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
    gate.resolve({ prepared: false }); assert.deepEqual(await closing, { prepared: false });
    // A rejected transfer may consume its token; start a fresh gesture after cancellation.
    const next = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    assert.equal((await f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, next.token))).ownerWindowId, '2');
  } finally { gate.resolve({ prepared: false }); f.dispose(); }
});

test('real registry command detach creates exactly one shell and keeps the same live contents', async () => {
  const f = await liveFixture(), a = await f.open('A'), view = f.views[0], frame = view.webContents.mainFrame;
  try {
    const result = await f.ipc.request(C.detach, f.w1.webContents, f.tabRequest(a));
    assert.equal(f.created.length, 1); assert.equal(result.ownerWindowId, '3');
    assert.equal(f.views.length, 1); assert.equal(view.webContents.mainFrame, frame);
    assert.equal(f.nativeCalls.length, 1); assert.equal(view.webContents.isDestroyed(), false);
    assert.equal(f.controller.authorize(event(view.webContents)).window, f.created[0]);
    assert.deepEqual(f.created[0].contentView.children, [view]);
  } finally { f.dispose(); }
});

test('real registry pins tokens to the initiating native shell, not a sender id or source claims', async () => {
  const f = await liveFixture(), a = await f.open('A'), b = await f.open('B', '2');
  try {
    const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    // Both fake contents deliberately share the same numeric id, like a forged claim.
    assert.equal(f.w1.webContents.id, f.w2.webContents.id);
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w2.webContents,
      f.transferRequest(b, lease.token, '1')), /token/);
    assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents,
      f.transferRequest(a, lease.token)), /token/);
  } finally { f.dispose(); }
});

test('real registry rejects an old begin token from a new main frame of the same shell contents', async () => {
  const f = await liveFixture(), a = await f.open('A');
  try {
    const contents = f.w1.webContents, initialFrame = contents.mainFrame, initial = f.tabRequest(a);
    const lease = f.ipc.request(C.transferBegin, contents, initial);
    contents.navigate(SHELL);
    assert.notEqual(contents.mainFrame, initialFrame);
    assert.deepEqual(f.tabRequest(a), initial, 'reload does not change registry revisions');
    await assert.rejects(async () => f.ipc.request(C.transfer, contents, f.transferRequest(a, lease.token)), /token|frame/i);
    assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
    assert.deepEqual(f.w1.contentView.children, [f.views[0]]); assert.deepEqual(f.w2.contentView.children, []);
    const fresh = f.ipc.request(C.transferBegin, contents, f.tabRequest(a));
    assert.equal((await f.ipc.request(C.transfer, contents, f.transferRequest(a, fresh.token))).ownerWindowId, '2');
  } finally { f.dispose(); }
});

test('unregistering the source shell revokes both unconsumed and in-flight transfer capabilities', async () => {
  for (const inFlight of [false, true]) {
    const f = await liveFixture(), a = await f.open('A'), gate = deferred(), entered = deferred();
    try {
      const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
      let moving;
      if (inFlight) {
        f.views[0].webContents.executeJavaScript = async () => { entered.resolve(); return gate.promise; };
        moving = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
        await entered.promise;
      }
      f.releases.get('1')(); f.controller.registerWindow(f.w1);
      if (!inFlight) moving = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
      gate.resolve(true); await assert.rejects(moving, /token|revoked/);
      assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
      assert.deepEqual(f.w1.contentView.children, [f.views[0]]); assert.deepEqual(f.w2.contentView.children, []);
    } finally { gate.resolve(true); f.dispose(); }
  }
});

test('transfer rejects an unregistered, navigated or destroyed target shell before consuming a token', async () => {
  for (const change of [f => f.w2.webContents.navigate('http://127.0.0.1:8765/'),
    f => f.w2.webContents.navigate(`${SHELL}?foreign=1`),
    f => f.w2.webContents.close(), f => { f.w2.webContents.mainFrame.isDestroyed = () => true; },
    f => f.w2.close()]) {
    const f = fixture(), a = f.addTab('A'); let calls = 0;
    f.registry.transferTab = () => { calls++; throw new Error('Must not consume'); };
    change(f);
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents,
      { ...f.tabRequest(a.tab), token: 'one-shot', targetWindowId: '2', targetWindowRevision: 0 }), /Untrusted|available/);
    assert.equal(calls, 0); f.controller.dispose();
  }
});

test('real registry target reload/navigation/unregistration during attach rolls back before commit', async () => {
  for (const change of [f => f.w2.webContents.navigate(SHELL),
    f => f.w2.webContents.navigate('http://127.0.0.1:8765/'), f => f.releases.get('2')(),
    f => { f.releases.get('2')(); f.controller.registerWindow(f.w2); }]) {
    const gate = deferred(), started = deferred(), f = await liveFixture(), a = await f.open('A');
    try {
      const view = f.views[0];
      view.webContents.executeJavaScript = async () => { started.resolve(); return gate.promise; };
      const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
      const pending = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
      await started.promise;
      assert.equal(f.w1.webContents.sent.length, 0); assert.equal(f.w2.webContents.sent.length, 0);
      change(f); gate.resolve(true); await assert.rejects(pending, /Untrusted/);
      assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
      assert.deepEqual(f.w1.contentView.children, [view]); assert.deepEqual(f.w2.contentView.children, []);
      assert.equal(f.w1.webContents.sent.length, 1);
      assert.equal(f.w1.webContents.sent[0][1].tabs[0].ownerWindowId, '1');
    } finally { gate.resolve(true); f.dispose(); }
  }
});

test('real registry token expiry and stale target are denied without a new view or navigation', async () => {
  let now = 100;
  const f = await liveFixture({ coreOptions: { now: () => now, transferTTL: 10 } }), a = await f.open('A');
  try {
    const expired = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    now = 111;
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents,
      f.transferRequest(a, expired.token)), /expired/);
    const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const stale = f.transferRequest(a, lease.token); await f.open('B', '2');
    await assert.rejects(async () => f.ipc.request(C.transfer, f.w1.webContents, stale), /target/);
    // Target validation happens before token consumption, so a corrected target
    // assertion can still use this capability if source identity stayed current.
    assert.equal((await f.ipc.request(C.transfer, f.w1.webContents,
      f.transferRequest(a, lease.token))).ownerWindowId, '2');
    assert.equal(f.views.length, 2); assert.equal(f.nativeCalls.length, 2);
  } finally { f.dispose(); }
});

test('real registry rejects a shell frame replaced during native attach and publishes only rollback', async () => {
  const gate = deferred(), started = deferred(), f = await liveFixture(), a = await f.open('A');
  try {
    const view = f.views[0];
    view.webContents.executeJavaScript = async () => { started.resolve(); return gate.promise; };
    const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const pending = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
    await started.promise;
    assert.equal(f.w1.webContents.sent.length, 0); assert.equal(f.w2.webContents.sent.length, 0);
    f.w1.webContents.navigate(SHELL); gate.resolve(true);
    await assert.rejects(pending, /Untrusted/);
    assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '1');
    assert.deepEqual(f.w1.contentView.children, [view]); assert.deepEqual(f.w2.contentView.children, []);
    assert.equal(f.w1.webContents.sent.length, 1);
    assert.equal(f.w1.webContents.sent[0][1].tabs[0].ownerWindowId, '1');
    assert.deepEqual(f.w2.webContents.sent[0][1].tabs, []);
  } finally { gate.resolve(true); f.dispose(); }
});

test('real registry denies close preparation during an in-flight transfer without entering dirty callback', async () => {
  const gate = deferred(), started = deferred(); let callbacks = 0;
  const f = await liveFixture({ prepareClose: () => { callbacks++; return { prepared: true }; } }), a = await f.open('A');
  try {
    f.views[0].webContents.executeJavaScript = async () => { started.resolve(); return gate.promise; };
    const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const moving = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
    await started.promise;
    await assert.rejects(async () => f.ipc.request(C.prepareClose, f.w1.webContents,
      { ...f.tabRequest(a), target: 'tab' }), /busy/);
    assert.equal(callbacks, 0); gate.resolve(true); await moving;
    assert.deepEqual(await f.ipc.request(C.prepareClose, f.w2.webContents,
      { ...f.tabRequest(a), target: 'tab' }), { prepared: true });
    assert.equal(callbacks, 1);
  } finally { gate.resolve(true); f.dispose(); }
});

test('lazy project registration uses immutable binding and only public own bootstrap snapshots', () => {
  const f = fixture(); const a = f.addTab('A', '1', false), b = f.addTab('B', '2');
  assert.throws(() => f.controller.authorize(event(a.view.webContents)), /no longer available/);
  a.commit();
  const shell = f.ipc.request(C.bootstrap, f.w1.webContents);
  assert.equal(shell.role, 'shell'); assert.deepEqual(shell.window.tabIds, [a.tab.tabId]);
  assert.deepEqual(shell.tabs.map(tab => tab.tabId), [a.tab.tabId]);
  const project = f.ipc.request(C.bootstrap, a.view.webContents);
  assert.equal(project.role, 'project'); assert.deepEqual(project.binding, binding('A'));
  assert.equal(project.tab.tabId, a.tab.tabId); assert.equal(project.windowId, '1');
  assert.equal('tabs' in project, false);
  for (const state of [shell, project]) {
    const serialized = JSON.stringify(state);
    assert.ok(!serialized.includes('SECRET')); assert.ok(!serialized.includes('webContents'));
    assert.ok(!serialized.includes(b.tab.tabId));
  }
  project.binding.epoch = 'spoofed'; shell.window.tabIds.push('spoofed');
  assert.equal(f.ipc.request(C.state, a.view.webContents).binding.epoch, 'epoch-1');
  assert.deepEqual(f.windows.get('1').tabIds, [a.tab.tabId]); f.controller.dispose();
});

test('only existing registry windows and unique native contents/bindings can register', () => {
  const f = fixture();
  assert.throws(() => f.controller.registerWindow(new Window(3)), /registry window/);
  assert.throws(() => f.controller.registerWindow(f.w1), /already registered/);
  assert.throws(() => f.controller.registerProjectView({ webContents: f.w1.webContents }, binding('A')));
  const a = f.addTab('A');
  assert.throws(() => f.controller.registerProjectView(a.view, binding('A')));
  assert.throws(() => f.controller.registerProjectView({ webContents: new Contents('about:blank') }, binding('A')));
  for (const invalid of [null, {}, { ...binding('B'), epoch: '' }, { ...binding('B'), project_id: 'a/b' }]) {
    assert.throws(() => f.controller.registerProjectView({ webContents: new Contents('about:blank') }, invalid));
  }
  f.controller.dispose();
});

test('all IPC requires exact live native identity and main frame, not ids or matching URLs', async () => {
  const f = fixture(), a = f.addTab('A');
  const foreign = new Contents(SHELL); foreign.id = f.w1.webContents.id;
  for (const channel of Object.values(C).filter(channel => ![C.stateChanged, C.closeRequest, C.restorePage].includes(channel))) {
    const handler = f.ipc.handlers.get(channel);
    for (const badEvent of [event(foreign), { sender: f.w1.webContents },
      { ...event(f.w1.webContents), senderFrame: { url: SHELL } },
      { ...event(a.view.webContents), senderFrame: { url: a.view.webContents.getURL() } }]) {
      await assert.rejects(async () => handler(badEvent, {}), /Untrusted tabs sender/);
    }
  }
  assert.deepEqual(f.calls, []);
  f.w1.webContents.mainFrame.isDestroyed = () => true;
  assert.throws(() => f.controller.authorize(event(f.w1.webContents)));
  delete f.w1.webContents.mainFrame.isDestroyed;
  const old = event(f.w1.webContents); f.w1.webContents.navigate(SHELL);
  assert.throws(() => f.controller.authorize(old));
  f.w1.webContents.destroyed = true;
  assert.throws(() => f.controller.authorize(event(f.w1.webContents)));
  f.w1.webContents.destroyed = false; f.w1.webContents = new Contents(SHELL);
  assert.throws(() => f.controller.authorize(old)); f.controller.dispose();
});

test('shell allowlist is exact file document or an explicitly configured data document', () => {
  const f = fixture(), contents = f.w1.webContents;
  for (const bad of ['about:blank', `${SHELL}.evil`, SHELL.replace('shell.html', 'splash.html'),
    'http://127.0.0.1:8765/', 'file:///arbitrary.html']) {
    contents.navigate(bad); assert.throws(() => f.controller.authorize(event(contents)), /shell document/);
  }
  contents.navigate(`${SHELL}#section`);
  assert.equal(f.controller.authorize(event(contents)).role, 'shell'); f.controller.dispose();
  const dataURL = 'data:text/html;charset=utf-8,%3Ctitle%3EShell%3C%2Ftitle%3E';
  const data = fixture({ shellURL: dataURL }); data.w1.webContents.navigate(dataURL);
  assert.equal(data.controller.authorize(event(data.w1.webContents)).role, 'shell');
  data.w1.webContents.navigate(`${dataURL}%20`);
  assert.throws(() => data.controller.authorize(event(data.w1.webContents)));
  data.w1.webContents.navigate(`${dataURL}?%3Cscript%3Ealert(1)%3C%2Fscript%3E`);
  assert.throws(() => data.controller.authorize(event(data.w1.webContents)));
  data.controller.dispose();
});

test('project allowlist requires fixed origin, no credentials and exact /p/id boundary in both URLs', () => {
  const f = fixture(), a = f.addTab('A'), contents = a.view.webContents;
  for (const good of ['/p/project-A', '/p/project-A/', '/p/project-A/tasks?selected=1#x']) {
    contents.navigate(`http://127.0.0.1:8765${good}`); assert.equal(f.controller.authorize(event(contents)).tab.tabId, a.tab.tabId);
  }
  for (const bad of ['http://localhost:8765/p/project-A/', 'http://127.0.0.1:8766/p/project-A/',
    'https://127.0.0.1:8765/p/project-A/', 'http://x:y@127.0.0.1:8765/p/project-A/',
    'http://127.0.0.1:8765/p/project-AB/', 'http://127.0.0.1:8765/p/project-B/',
    'http://127.0.0.1:8765/explorer', 'http://127.0.0.1:8765/p/project-A/../../explorer',
    'http://127.0.0.1:8765/p/project%2dA/']) {
    contents.navigate(bad); assert.throws(() => f.controller.authorize(event(contents)), /project document/);
  }
  contents.navigate('http://127.0.0.1:8765/p/project-A/');
  const getURL = contents.getURL; contents.getURL = () => 'https://evil.example';
  assert.throws(() => f.controller.authorize(event(contents))); contents.getURL = getURL;
  f.controller.dispose();
});

test('same-origin shell query is an exact checked identity, never the legacy SPA', () => {
  const address = 'http://127.0.0.1:8765/?desktopShell=1';
  const f = fixture({ shellURL: address }), contents = f.w1.webContents;
  for (const bad of ['http://127.0.0.1:8765/', 'http://127.0.0.1:8765/?desktopShell=0',
    `${address}&extra=1`, `${address}&desktopShell=1`, address.replace('127.0.0.1', 'localhost'),
    address.replace('http://', 'http://user:password@')]) {
    contents.navigate(bad);
    assert.throws(() => f.ipc.request(C.bootstrap, contents), /shell document/);
    // Both the contents URL and the sender frame must have the exact selector.
    contents.getURL = () => address;
    assert.throws(() => f.ipc.request(C.bootstrap, contents), /shell document/);
    delete contents.getURL;
  }
  contents.navigate(`${address}#navigation`);
  assert.equal(f.ipc.request(C.bootstrap, contents).role, 'shell');
  f.controller.dispose();
});

test('shell menu services validate fixed arguments and role before touching backend', async () => {
  const calls = [];
  const names = ['recentProjects', 'inspectPath', 'browseDirectory', 'pickerStatus', 'pickDirectory', 'forgetRecent'];
  const services = Object.fromEntries(names.map(name => [name, async (...args) => { calls.push([name, ...args]); return name; }]));
  const f = fixture({ shellServices: services }), a = f.addTab('A'), contents = f.w1.webContents;
  for (const name of names) {
    await assert.rejects(async () => f.ipc.request(C[name], a.view.webContents, '/A'), /Shell request required/);
    await assert.rejects(async () => f.ipc.request(C[name], new Contents(SHELL), '/A'), /Untrusted/);
    await assert.rejects(async () => f.ipc.request(C[name], contents, '/A', 'extra'));
  }
  for (const name of ['inspectPath', 'browseDirectory', 'pickDirectory', 'forgetRecent']) {
    for (const bad of [null, {}, ['A'], 4, 'a\0b', 'x'.repeat(32769)]) {
      await assert.rejects(async () => f.ipc.request(C[name], contents, bad));
    }
  }
  for (const name of ['inspectPath', 'forgetRecent']) {
    await assert.rejects(async () => f.ipc.request(C[name], contents));
    await assert.rejects(async () => f.ipc.request(C[name], contents, ''));
  }
  assert.deepEqual(calls, []);
  assert.equal(await f.ipc.request(C.recentProjects, contents), 'recentProjects');
  assert.equal(await f.ipc.request(C.inspectPath, contents, '/A'), 'inspectPath');
  assert.equal(await f.ipc.request(C.browseDirectory, contents, ''), 'browseDirectory');
  assert.equal(await f.ipc.request(C.pickerStatus, contents), 'pickerStatus');
  assert.equal(await f.ipc.request(C.pickDirectory, contents), 'pickDirectory');
  assert.equal(await f.ipc.request(C.pickDirectory, contents, '/A'), 'pickDirectory');
  assert.equal(await f.ipc.request(C.forgetRecent, contents, '/A'), 'forgetRecent');
  assert.deepEqual(calls, [['recentProjects'], ['inspectPath', '/A'], ['browseDirectory', ''],
    ['pickerStatus'], ['pickDirectory', undefined], ['pickDirectory', '/A'], ['forgetRecent', '/A']]);
  f.controller.dispose();
});

test('shell services reject late results after navigation and propagate recoverable errors', async () => {
  const pending = deferred();
  const f = fixture({ shellServices: { recentProjects: () => pending.promise,
    inspectPath: async () => { throw new Error('Shell service failed (403)'); } } });
  await assert.rejects(f.ipc.request(C.inspectPath, f.w1.webContents, '/A'), /403/);
  assert.equal(f.ipc.request(C.state, f.w1.webContents).role, 'shell');
  const result = f.ipc.request(C.recentProjects, f.w1.webContents);
  f.w1.webContents.navigate('http://127.0.0.1:8765/'); pending.resolve([]);
  await assert.rejects(result, /Untrusted/);
  f.controller.dispose();
});

test('menu visibility requires exact shell request and current own window revision', () => {
  const changes = [], f = fixture({ setMenuOpen: (...args) => changes.push(args) });
  const a = f.addTab('A'), contents = f.w1.webContents;
  const request = { ...f.windowRequest(), open: true };
  for (const bad of [{ ...request, open: 1 }, { ...request, open: undefined },
    { ...request, windowId: '2' }, { ...request, windowRevision: 0 }, { ...request, root: '/A' }]) {
    assert.throws(() => f.ipc.request(C.setMenuOpen, contents, bad));
  }
  assert.throws(() => f.ipc.request(C.setMenuOpen, a.view.webContents, request));
  assert.throws(() => f.ipc.request(C.setMenuOpen, contents, request, 'extra'));
  assert.deepEqual(changes, []);
  assert.equal(f.ipc.request(C.setMenuOpen, contents, request), undefined);
  assert.deepEqual(changes, [['1', true]]); f.controller.dispose();
});

test('backend shell services use fixed authenticated global URLs, not legacy open or selection', async () => {
  const calls = [], services = createShellServices({ token: () => 'private-token', fetch: async (url, options) => {
    calls.push({ url, options }); return { ok: true, json: async () => [] };
  } });
  const root = 'D:\\Game & sibling?#';
  await services.recentProjects(); await services.inspectPath(root); await services.browseDirectory('');
  await services.pickerStatus(); await services.pickDirectory(root);
  assert.equal(await services.forgetRecent(root), undefined);
  assert.deepEqual(calls.map(item => item.url), [
    'http://127.0.0.1:8765/api/project/recent',
    `http://127.0.0.1:8765/api/fs/inspect?path=${encodeURIComponent(root)}`,
    'http://127.0.0.1:8765/api/fs/browse?path=', 'http://127.0.0.1:8765/api/fs/picker',
    'http://127.0.0.1:8765/api/fs/pick', `http://127.0.0.1:8765/api/project/recent?path=${encodeURIComponent(root)}`,
  ]);
  for (const { options } of calls) {
    assert.equal(options.headers.Authorization, 'Bearer private-token');
    assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
  }
  assert.equal(calls[4].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[4].options.body), { start_path: root });
  assert.equal('signal' in calls[4].options, false, 'human picker has no frontend timeout');
  assert.equal(calls[5].options.method, 'DELETE');
  const unauthorized = createShellServices({ fetch: async () => ({ ok: false, status: 401 }) });
  await assert.rejects(unauthorized.recentProjects(), /401/);
});

test('binding conflicts, deleted tabs and replacements never grant stale project rights', () => {
  const f = fixture(), a = f.addTab('A'), contents = a.view.webContents;
  // Copy the binding at registration rather than trusting the caller object.
  a.tab.project = { ...binding('A'), epoch: 'epoch-2' };
  assert.throws(() => f.controller.authorize(event(contents)));
  a.tab.project = binding('A');
  assert.equal(f.controller.authorize(event(contents)).tab.tabId, a.tab.tabId);
  a.tab.project.root = '/different'; assert.throws(() => f.controller.authorize(event(contents)));
  a.tab.project = binding('A'); f.tabs.delete(a.tab.tabId);
  assert.throws(() => f.controller.authorize(event(contents)));
  f.tabs.set('replacement', { ...a.tab, tabId: 'replacement' });
  assert.throws(() => f.controller.authorize(event(contents)));
  f.controller.dispose();
});

test('open validates exact payload, own window and revision before calling the registry', async () => {
  const f = fixture(), contents = f.w1.webContents;
  const good = { ...f.windowRequest(), root: '/projects/A' };
  for (const request of [null, [], {}, { ...good, windowId: '2' }, { ...good, windowRevision: 1 },
    { ...good, windowRevision: -1 }, { ...good, windowRevision: '0' }, { ...good, root: '' },
    { ...good, root: 123 }, { ...good, root: 'a\0b' }, { ...good, root: 'x'.repeat(32769) },
    { ...good, create: 'yes' }, { ...good, create: undefined }, { ...good, tabId: 'spoofed' },
    { ...good, token: 'spoofed' }, { ...good, url: 'https://evil.example' }, Object.create(good)]) {
    await assert.rejects(async () => f.ipc.request(C.open, contents, request));
  }
  await assert.rejects(async () => f.ipc.request(C.open, contents, good, 'extra'));
  assert.deepEqual(f.calls, []);
  const result = await f.ipc.request(C.open, contents, good);
  assert.equal(result.tabId, 'tab-A');
  assert.deepEqual(f.calls[0], ['open', '/projects/A', { windowId: '1', create: false }]);
  assert.throws(() => f.ipc.request(C.bootstrap, contents, {}));
  assert.throws(() => f.ipc.request(C.state, contents, undefined)); f.controller.dispose();
});

test('activate is shell-only and requires current window revision, tab revision and owner', async () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2'), contents = f.w1.webContents;
  for (const request of [{ ...f.tabRequest(a.tab), windowId: '2' },
    { ...f.tabRequest(a.tab), windowRevision: 0 }, { ...f.tabRequest(a.tab), tabRevision: 0 },
    { ...f.windowRequest(), tabId: b.tab.tabId, tabRevision: b.tab.revision },
    { ...f.tabRequest(a.tab), ownerWindowId: '1' }, { ...f.tabRequest(a.tab), tabRevision: '1' }]) {
    assert.throws(() => f.ipc.request(C.activate, contents, request));
  }
  assert.throws(() => f.ipc.request(C.activate, a.view.webContents, f.tabRequest(a.tab)));
  await assert.rejects(async () => f.ipc.request(C.open, a.view.webContents, { ...f.windowRequest(), root: '/projects/C' }));
  assert.deepEqual(f.calls, []);
  assert.equal(f.ipc.request(C.activate, contents, f.tabRequest(a.tab)).tabId, a.tab.tabId);
  f.controller.dispose();
});

test('repeated shell activation restores native shell focus after each registry activation', () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B'), shell = f.w1.webContents;
  let focused, shellFocuses = 0;
  shell.focus = () => { focused = shell; shellFocuses++; };
  const activate = f.registry.activateTab;
  f.registry.activateTab = id => {
    focused = id === a.tab.tabId ? a.view.webContents : b.view.webContents;
    return activate.call(f.registry, id);
  };
  for (const tab of [a.tab, b.tab, a.tab, b.tab, b.tab]) {
    const result = f.ipc.request(C.activate, shell, f.tabRequest(tab));
    assert.equal(result.tabId, tab.tabId);
    assert.equal(focused, shell, 'Arrow/Home/End activation keeps shell as the native keyboard target');
  }
  assert.equal(shellFocuses, 5);
  assert.equal(f.calls.filter(([kind]) => kind === 'activate').length, 5);
  assert.equal(f.calls.some(([kind]) => kind === 'open'), false);
  f.controller.dispose();
});

test('only open may focus a global existing tab, never move or recreate it', async () => {
  const f = fixture(), a = f.addTab('A', '1');
  const result = await f.ipc.request(C.open, f.w2.webContents, { ...f.windowRequest('2'), root: a.tab.project.root });
  assert.equal(result.tabId, a.tab.tabId); assert.equal(result.ownerWindowId, '1');
  assert.equal(f.tabs.size, 1); assert.deepEqual(f.windows.get('2').tabIds, []);
  assert.equal(f.windows.get('1').activeTabId, a.tab.tabId);
  assert.throws(() => f.ipc.request(C.activate, f.w2.webContents, { ...f.windowRequest('2'), tabId: a.tab.tabId, tabRevision: a.tab.revision }));
  f.controller.dispose();
});

test('project authorization and contents delivery read the current owner/revisions every call', async () => {
  const f = fixture(), a = f.addTab('A'), contents = a.view.webContents;
  assert.equal(f.controller.authorize(event(contents)).window, f.w1);
  const old = { ...f.tabRequest(a.tab), target: 'tab' };
  f.windows.get('1').tabIds = []; f.windows.get('1').revision++;
  f.windows.get('2').tabIds.push(a.tab.tabId); f.windows.get('2').revision++;
  a.tab.ownerWindowId = '2'; a.tab.revision++; a.tab.transferGeneration++;
  const current = f.controller.authorize(event(contents));
  assert.equal(current.window, f.w2); assert.equal(current.tab.revision, 2);
  assert.deepEqual(f.controller.contentsForWindow('1'), [f.w1.webContents]);
  assert.deepEqual(f.controller.contentsForWindow('2'), [f.w2.webContents, contents]);
  assert.deepEqual(new Set(f.controller.allContents()), new Set([f.w1.webContents, f.w2.webContents, contents]));
  await assert.rejects(async () => f.ipc.request(C.prepareClose, contents, old));
  assert.throws(() => f.ipc.request(C.activate, f.w1.webContents, { ...old, windowRevision: f.windows.get('1').revision }));
  f.w1.close();
  assert.equal(f.controller.authorize(event(contents)).window, f.w2);
  assert.equal(f.ipc.request(C.bootstrap, f.w2.webContents).role, 'shell'); f.controller.dispose();
});

test('prepare-close defaults fail closed and never invokes surface disposal or quit', async () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2');
  assert.deepEqual(await f.ipc.request(C.prepareClose, f.w1.webContents, { ...f.windowRequest(), target: 'window' }), { prepared: false });
  assert.deepEqual(await f.ipc.request(C.prepareClose, a.view.webContents, { ...f.tabRequest(a.tab), target: 'tab' }), { prepared: false });
  for (const request of [{ ...f.windowRequest(), target: 'quit' }, { ...f.windowRequest(), target: 'tab' },
    { ...f.windowRequest(), target: 'window', tabId: a.tab.tabId },
    { ...f.windowRequest(), target: 'tab', tabId: b.tab.tabId, tabRevision: 1 },
    { ...f.tabRequest(a.tab), target: 'tab', force: true }]) {
    await assert.rejects(async () => f.ipc.request(C.prepareClose, f.w1.webContents, request));
  }
  await assert.rejects(async () => f.ipc.request(C.prepareClose, a.view.webContents, { ...f.windowRequest(), target: 'window' }));
  assert.deepEqual(f.calls, []); assert.equal(f.tabs.size, 2);
  assert.equal(a.view.webContents.isDestroyed(), false); assert.equal(f.w1.isDestroyed(), false); f.controller.dispose();
});

test('injected close seam gets validated identity and returns only explicit prepared boolean', async () => {
  let received, result = { prepared: true, token: 'SECRET' };
  const f = fixture({ prepareClose: input => { received = input; return result; } }), a = f.addTab('A');
  const request = { ...f.tabRequest(a.tab), target: 'tab' };
  assert.deepEqual(await f.ipc.request(C.prepareClose, a.view.webContents, request), { prepared: true });
  assert.equal(received.authorization.window, f.w1); assert.equal(received.authorization.contents, a.view.webContents);
  assert.ok(Object.isFrozen(received.request));
  for (result of [true, undefined, {}, { prepared: 'true' }, { prepared: false }]) {
    assert.deepEqual(await f.ipc.request(C.prepareClose, a.view.webContents, request), { prepared: false });
  }
  assert.equal(f.tabs.size, 1); f.controller.dispose();
});

test('awaited close preparation rejects changed revisions, ownership or frame after callback', async () => {
  for (const change of [f => { f.windows.get('1').revision++; },
    (_f, a) => { a.tab.revision++; }, (_f, a) => { a.tab.ownerWindowId = '2'; },
    (_f, a) => { a.view.webContents.navigate(a.view.webContents.getURL()); },
    (_f, a) => { a.view.webContents.close(); }]) {
    const gate = deferred(), f = fixture({ prepareClose: () => gate.promise }), a = f.addTab('A');
    const pending = f.ipc.request(C.prepareClose, a.view.webContents, { ...f.tabRequest(a.tab), target: 'tab' });
    change(f, a); gate.resolve({ prepared: true }); await assert.rejects(pending); f.controller.dispose();
  }
});

test('state events are scoped, exclude navigated/dead contents and isolate send failures', () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2');
  f.controller.publishState();
  assert.deepEqual(f.w1.webContents.sent[0][1].tabs.map(tab => tab.tabId), [a.tab.tabId]);
  assert.deepEqual(f.w2.webContents.sent[0][1].tabs.map(tab => tab.tabId), [b.tab.tabId]);
  assert.equal(a.view.webContents.sent[0][1].tab.tabId, a.tab.tabId);
  assert.equal('tabs' in a.view.webContents.sent[0][1], false);
  a.view.webContents.navigate('https://evil.example'); f.w1.webContents.send = () => { throw new Error('closing'); };
  f.controller.publishState(); assert.equal(f.w2.webContents.sent.length, 2); assert.equal(a.view.webContents.sent.length, 1);
  b.view.webContents.close(); assert.ok(!f.controller.allContents().includes(b.view.webContents));
  f.controller.dispose();
});

test('native destruction and per-view release do not remove app IPC or other window identities', () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2');
  a.release(); a.release(); assert.throws(() => f.controller.authorize(event(a.view.webContents)));
  assert.equal(a.view.webContents.listenerCount('destroyed'), 0);
  f.w1.close(); assert.equal(f.ipc.handlers.size, Object.keys(C).length - 3); assert.equal(f.ipc.removed.length, 0);
  assert.equal(f.ipc.request(C.state, b.view.webContents).windowId, '2');
  f.w2.webContents.close(); assert.throws(() => f.controller.authorize(event(b.view.webContents)));
  f.controller.dispose(); f.controller.dispose();
  assert.equal(f.ipc.handlers.size, 0); assert.equal(f.ipc.removed.length, Object.keys(C).length - 3);
  assert.equal(b.view.webContents.listenerCount('destroyed'), 0);
  assert.equal(f.w1.listenerCount('closed'), 0); assert.equal(f.w2.listenerCount('closed'), 0);
  assert.equal(f.w2.isDestroyed(), false); assert.equal(b.view.webContents.isDestroyed(), false);
  assert.equal(f.tabs.size, 2); assert.deepEqual(f.controller.allContents(), []);
  assert.throws(() => f.controller.registerWindow(f.w2));
});

test('registration failure rolls back only handlers this controller installed', () => {
  const ipc = ipcFixture(), existing = () => 'other owner'; ipc.handle(C.open, existing);
  assert.throws(() => registerTabsIpc({ ipcMain: ipc, registry: {} }), /already installed/);
  assert.deepEqual([...ipc.handlers.keys()], [C.open]); assert.equal(ipc.handlers.get(C.open), existing);
});

test('actual VS-46 adapter configures IPC before registry insertion without native registry lookups', async () => {
  const ipc = ipcFixture(), views = [], early = [];
  let controller;
  class View {
    constructor() { this.webContents = new Contents('about:blank'); views.push(this); }
    setVisible() {}
    setBounds() {}
  }
  const electron = createElectronAdapter({ WebContentsView: View, configureProjectView(view, project) {
    const release = controller.registerProjectView(view, project);
    early.push(() => controller.authorize(event(view.webContents)));
    assert.throws(early.at(-1)); return release;
  } });
  const registry = new DesktopTabs({ backend: { ensureProject: async () => binding('A') }, electron, canonicalizeRoot: async root => root });
  controller = registerTabsIpc({ ipcMain: ipc, registry });
  const window = new Window(1); registry.registerWindow(window); controller.registerWindow(window);
  const tab = await ipc.request(C.open, window.webContents, { windowId: '1', windowRevision: 0, root: '/projects/A' });
  assert.equal(views.length, 1); assert.equal(controller.authorize(event(views[0].webContents)).tab.tabId, tab.tabId);
  assert.equal(registry.getTab(tab.tabId).view, undefined);
  registry.closeTab(tab.tabId); assert.equal(views[0].webContents.listenerCount('destroyed'), 0);
  assert.throws(early[0]); controller.dispose(); registry.dispose();
});

test('preload exposes fixed methods, preserves existing bridges and strips Electron events', async () => {
  const exposed = {}, invoked = [], listeners = new EventEmitter();
  const source = fs.readFileSync(path.join(__dirname, '..', 'preload.cjs'), 'utf8');
  vm.runInNewContext(source, { require(name) {
    assert.equal(name, 'electron');
    return { contextBridge: { exposeInMainWorld: (name, bridge) => { exposed[name] = bridge; } },
      ipcRenderer: { invoke: (...args) => { invoked.push(args); return Promise.resolve('ok'); },
        on: (channel, listener) => listeners.on(channel, listener),
        removeListener: (channel, listener) => listeners.removeListener(channel, listener) } };
  } });
  assert.deepEqual(Object.keys(exposed).sort(), ['voxariumNotifications', 'voxariumStartup', 'voxariumTabs', 'voxariumUpdates', 'voxariumWindow']);
  assert.deepEqual(Object.keys(exposed.voxariumNotifications).sort(), ['getState', 'setEnabled']);
  await exposed.voxariumNotifications.getState('ignored');
  await exposed.voxariumNotifications.setEnabled(true);
  assert.deepEqual(invoked.splice(0), [['studio:notifications', 'state'], ['studio:notifications', 'set-enabled', true]]);
  const tabs = exposed.voxariumTabs;
  assert.deepEqual(Object.keys(tabs).sort(), ['acknowledgeClose', 'acknowledgePageRestore', 'activateTab', 'beginDrag', 'bootstrap', 'browseDirectory',
    'cancelDrag', 'closeTab', 'detachTab', 'forgetRecent', 'getState', 'inspectPath', 'onPrepareClose', 'onRestorePage', 'onStateChange', 'openProject',
    'pickDirectory', 'pickerStatus', 'prepareClose', 'recentProjects', 'reorderTab', 'reportPage', 'setDragGeometry', 'setMenuOpen']);
  const request = { windowId: '1', windowRevision: 0, root: '/projects/A' };
  await tabs.bootstrap('ignored-channel'); await tabs.getState('ignored-channel');
  await tabs.openProject(request); await tabs.activateTab(request); await tabs.prepareClose(request);
  await tabs.recentProjects(); await tabs.inspectPath('/A'); await tabs.browseDirectory('');
  await tabs.pickerStatus(); await tabs.pickDirectory(); await tabs.pickDirectory('/A');
  await tabs.forgetRecent('/A'); await tabs.setMenuOpen(request);
  await tabs.closeTab(request); await tabs.acknowledgeClose(request);
  await tabs.reportPage(request); await tabs.acknowledgePageRestore(request);
  assert.deepEqual(invoked, [[C.bootstrap], [C.state], [C.open, request], [C.activate, request], [C.prepareClose, request],
    [C.recentProjects], [C.inspectPath, '/A'], [C.browseDirectory, ''], [C.pickerStatus], [C.pickDirectory, undefined],
    [C.pickDirectory, '/A'], [C.forgetRecent, '/A'], [C.setMenuOpen, request], [C.close, request], [C.closeAck, request],
    [C.reportPage, request], [C.pageRestoreAck, request]]);
  assert.equal(tabs.send, undefined); assert.equal(tabs.invoke, undefined); assert.equal(tabs.on, undefined);
  const received = [], callback = (...args) => received.push(args);
  const release = tabs.onStateChange(callback), otherRelease = tabs.onStateChange(callback);
  const native = { sender: 'SECRET', reply() {} }, state = { role: 'shell' };
  listeners.emit(C.stateChanged, native, state); assert.deepEqual(received, [[state], [state]]);
  release(); release(); assert.equal(listeners.listenerCount(C.stateChanged), 1);
  otherRelease(); assert.equal(listeners.listenerCount(C.stateChanged), 0);
  assert.throws(() => tabs.onStateChange('not a callback'));
  const releaseClose = tabs.onPrepareClose(callback);
  listeners.emit(C.closeRequest, native, request); assert.deepEqual(received.at(-1), [request]);
  releaseClose(); releaseClose(); assert.equal(listeners.listenerCount(C.closeRequest), 0);
  assert.throws(() => tabs.onPrepareClose(null));
  const releasePage = tabs.onRestorePage(callback);
  listeners.emit(C.restorePage, native, request); assert.deepEqual(received.at(-1), [request]);
  releasePage(); releasePage(); assert.equal(listeners.listenerCount(C.restorePage), 0);
  assert.throws(() => tabs.onRestorePage(null));
  assert.equal(typeof exposed.voxariumWindow.close, 'function'); assert.equal(typeof exposed.voxariumUpdates.install, 'function');
  assert.equal(typeof exposed.voxariumStartup.setEnabled, 'function');
});

function closeRequest(view) { return view.webContents.sent.findLast(([channel]) => channel === C.closeRequest)?.[1]; }

function pageReport(tab, page = { path: '/tasks', query: { task: 'task-1', chat: 'chat.2', assign: 'team:agent' } }) {
  return { tabId: tab.tabId, tabRevision: tab.revision, transferGeneration: tab.transferGeneration, page };
}
function pageRestoreRequest(view) { return view.webContents.sent.findLast(([channel]) => channel === C.restorePage)?.[1]; }
function pageRestoreAck(request, status = 'restored') {
  const { page, ...tuple } = request;
  return { ...tuple, status };
}

test('page reports pin project native sender, main frame, identity, revision and transfer generation', () => {
  const changes = [];
  const f = fixture({ onPageChanged: () => { changes.push(f.controller.snapshotPages()); throw new Error('observer failed'); } });
  const a = f.addTab('A'), b = f.addTab('B', '2'), report = pageReport(a.tab);
  try {
    assert.throws(() => f.ipc.request(C.reportPage, f.w1.webContents, report), /Project page/);
    assert.throws(() => f.ipc.request(C.reportPage, b.view.webContents, report), /Foreign/);
    assert.throws(() => f.ipc.request(C.reportPage, new Contents(a.view.webContents.getURL()), report), /Untrusted/);
    assert.throws(() => f.ipc.handlers.get(C.reportPage)({ sender: a.view.webContents,
      senderFrame: { ...a.view.webContents.mainFrame } }, report), /Untrusted/);
    for (const patch of [{ tabRevision: 0 }, { transferGeneration: 1 }, { transferGeneration: '0' },
      { tabId: b.tab.tabId }, { token: 'SECRET' }, { windowId: '1' }]) {
      assert.throws(() => f.ipc.request(C.reportPage, a.view.webContents, { ...report, ...patch }));
    }
    assert.throws(() => f.ipc.request(C.reportPage, a.view.webContents, report, 'extra'));
    assert.deepEqual(f.controller.snapshotPages(), []);
    assert.deepEqual(changes, []);
    f.ipc.request(C.reportPage, a.view.webContents, report);
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tab.tabId, page: report.page }]);
    assert.deepEqual(changes, [[{ tabId: a.tab.tabId, page: report.page }]]);
    const original = event(a.view.webContents);
    a.view.webContents.navigate(`http://127.0.0.1:8765/p/${a.tab.project.project_id}/tasks`);
    assert.throws(() => f.ipc.handlers.get(C.reportPage)(original, report), /Untrusted/);
    a.view.webContents.navigate(`http://127.0.0.1:8765/p/${b.tab.project.project_id}/`);
    assert.throws(() => f.ipc.request(C.reportPage, a.view.webContents, report), /Untrusted project/);
    a.view.webContents.navigate(`http://127.0.0.1:8765/p/${a.tab.project.project_id}/`);
    f.tabs.delete(a.tab.tabId); f.tabs.set('replacement', { ...a.tab, tabId: 'replacement' });
    assert.throws(() => f.ipc.request(C.reportPage, a.view.webContents,
      { ...report, tabId: 'replacement' }), /no longer available/);
    assert.equal(changes.length, 1, 'stale and untrusted reports never notify persistence');
  } finally { f.controller.dispose(); }
});

test('page descriptor validation rejects secrets, drafts, files and navigation bypasses before IPC', async () => {
  const f = fixture(), a = f.addTab('A'), contents = a.view.webContents;
  const invalid = [null, [], {}, { path: '/tasks', secret: 'SECRET' }, { path: '/tasks', draft: 'text' },
    { path: '/tasks', files: [new Blob(['attachment'])] }, { path: '/tasks', query: { token: 'SECRET' } },
    { path: '/tasks', query: { task: ['task-1'] } }, { path: '/tasks', query: { task: new Blob(['file']) } },
    { path: '/tasks', query: { chat: 'x'.repeat(257) } }, { path: '/tasks', query: { assign: '' } },
    { path: '/tasks', query: { task: 'one two' } }, { path: '/tasks', query: { chat: 'chat/1' } },
    { path: '/tasks', query: null }, Object.create({ path: '/tasks' }),
    Object.defineProperty({ path: '/tasks' }, 'token', { value: 'SECRET' }),
    { path: '/tasks', [Symbol('token')]: 'SECRET' },
    Object.defineProperty({}, 'path', { enumerable: true, get() { throw new Error('must not read getter'); } }),
    ...['https://outside.test/tasks', '//outside.test', 'tasks', '/p', '/p/foreign/tasks', '/api', '/api/projects',
      '/ws', '/ws/projects', '/tasks?token=SECRET', '/tasks#draft', '/tasks\\evil', '/../tasks', '/tasks/./one',
      '/%70/foreign', '/%61pi/projects', '/%77s', '/%2e%2e/tasks', '/%252e%252e/tasks', '/tasks%3Ftoken=x',
      '/tasks%23draft', '/%2foutside', '/tasks/%5cevil', '/tasks/%00', '/tasks/%', '/tasks/\uD800',
      '/' + 'a'.repeat(2048)].map(path => ({ path })),
    ...['/absolute', 'C:/absolute', '\\absolute', 'one/../two', '.', '..', 'one//two',
      'one?token=x', 'one\u0000two',
      'a'.repeat(4097)].map(path => ({ path: '/explorer', query: { path } })),
  ];
  try {
    for (const page of invalid) {
      assert.throws(() => f.ipc.request(C.reportPage, contents, pageReport(a.tab, page)));
      assert.deepEqual(await f.controller.restorePage(a.tab.tabId, page), { status: 'invalid' });
    }
    assert.equal(contents.sent.filter(([channel]) => channel === C.restorePage).length, 0);
    assert.deepEqual(f.controller.snapshotPages(), []);
    assert.equal(f.calls.length, 0);
  } finally { f.controller.dispose(); }
});

test('page snapshots are copied, manifest-compatible and never broadcast in public state', async () => {
  const f = fixture(), a = f.addTab('A'), b = f.addTab('B', '2');
  const { createSessionStateAdapter } = require('../session-state.cjs');
  const adapter = createSessionStateAdapter({ fetch: async (_url, options) => ({ ok: true, status: 200,
    json: async () => JSON.parse(options.body) }) });
  const accepted = [{ path: '/' }, { path: '/docs/space/doc.md' }, { path: '/chat/chat-1' },
    { path: '/pipelines/pipe-1' }, { path: '/plugins/plugin-1' },
    { path: '/explorer', query: { path: 'Assets/Модели/mesh.fbx' } },
    { path: '/explorer', query: { path: 'Assets/space%20name.fbx' } },
    { path: '/explorer', query: { path: 'notes/100%#plan.md' } },
    { path: '/explorer', query: { path: '%2e%2e/%2fabsolute%5cfile.md' } },
    { path: '/tasks', query: { task: 'A'.repeat(256), chat: 'chat:one.2', assign: 'agent-1' } }];
  try {
    for (const page of accepted) {
      f.ipc.request(C.reportPage, a.view.webContents, pageReport(a.tab, page));
      const snapshot = f.controller.snapshotPages();
      assert.deepEqual(snapshot, [{ tabId: a.tab.tabId, page }]);
      const manifest = { version: 1, windows: [{ tabs: [{ root: 'D:/fixture/A', page: snapshot[0].page }], active_tab: 0 }], active_window: 0 };
      assert.deepEqual(await adapter.save(manifest), manifest);
    }
    const page = { path: '/tasks', query: { task: 'task-original' } };
    f.ipc.request(C.reportPage, a.view.webContents, pageReport(a.tab, page));
    page.query.task = 'caller-mutated';
    const snapshot = f.controller.snapshotPages(); snapshot[0].page.query.task = 'snapshot-mutated';
    snapshot.push({ tabId: b.tab.tabId, page: { path: '/' } });
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tab.tabId,
      page: { path: '/tasks', query: { task: 'task-original' } } }]);
    f.controller.publishState();
    for (const contents of [f.w1.webContents, f.w2.webContents, b.view.webContents]) {
      const state = contents.sent.at(-1)[1];
      assert.equal(JSON.stringify(state).includes('task-original'), false);
      assert.equal(JSON.stringify(state).includes('"page"'), false);
    }
    f.tabs.delete(a.tab.tabId);
    assert.deepEqual(f.controller.snapshotPages(), []);
  } finally { f.controller.dispose(); }
});

test('main page restore roundtrip targets only its native project and consumes one exact ACK', async () => {
  const changes = [];
  const f = fixture({ onPageChanged: () => { changes.push(f.controller.snapshotPages()); throw new Error('observer failed'); } });
  const a = f.addTab('A'), b = f.addTab('B', '2');
  try {
    const page = { path: '/explorer', query: { path: 'Assets/mesh.fbx' } };
    const restoring = f.controller.restorePage(a.tab.tabId, page);
    const request = pageRestoreRequest(a.view), ack = pageRestoreAck(request);
    assert.deepEqual(Object.keys(request).sort(), ['page', 'requestId', 'tabId', 'tabRevision', 'transferGeneration']);
    assert.deepEqual(request.page, page); assert.equal(request.tabId, a.tab.tabId);
    assert.equal(b.view.webContents.sent.length, 0); assert.equal(f.w1.webContents.sent.length, 0);
    assert.throws(() => f.ipc.request(C.pageRestoreAck, f.w1.webContents, ack), /Project page ACK/);
    assert.throws(() => f.ipc.request(C.pageRestoreAck, b.view.webContents, ack), /Stale page restore ACK/);
    for (const patch of [{ requestId: 'forged' }, { tabId: b.tab.tabId }, { tabRevision: 2 },
      { transferGeneration: 1 }, { status: 'ok' }, { page }, { secret: 'SECRET' }]) {
      assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, { ...ack, ...patch }));
    }
    assert.throws(() => f.ipc.handlers.get(C.pageRestoreAck)({ sender: a.view.webContents,
      senderFrame: { ...a.view.webContents.mainFrame } }, ack), /Untrusted/);
    assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, ack, 'extra'));
    assert.deepEqual(changes, [], 'no tentative restore snapshot is published');
    page.query.path = 'caller-mutated'; request.page.query.path = 'sent-payload-mutated';
    f.ipc.request(C.pageRestoreAck, a.view.webContents, ack);
    assert.deepEqual(await restoring, { status: 'restored' });
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tab.tabId,
      page: { path: '/explorer', query: { path: 'Assets/mesh.fbx' } } }]);
    assert.deepEqual(changes, [f.controller.snapshotPages()]);
    assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, ack), /Stale page restore ACK/);
    assert.equal(changes.length, 1);
  } finally { f.controller.dispose(); }
});

test('restore ACK preserves latest reported selection, otherwise snapshots the requested page', async () => {
  let changes = 0;
  const f = fixture({ onPageChanged: () => { changes++; } }), a = f.addTab('A');
  try {
    f.ipc.request(C.reportPage, a.view.webContents, pageReport(a.tab, { path: '/explorer' }));
    const requested = { path: '/tasks', query: { task: 'requested', assign: 'agent-1' } };
    const restoring = f.controller.restorePage(a.tab.tabId, requested);
    f.ipc.request(C.reportPage, a.view.webContents, pageReport(a.tab, { path: '/tasks' }));
    const actual = { path: '/tasks', query: { task: 'actual-consumed' } };
    f.ipc.request(C.reportPage, a.view.webContents, pageReport(a.tab, actual));
    assert.equal(changes, 3);
    f.ipc.request(C.pageRestoreAck, a.view.webContents, pageRestoreAck(pageRestoreRequest(a.view)));
    assert.deepEqual(await restoring, { status: 'restored' });
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tab.tabId, page: actual }]);
    assert.equal(changes, 3, 'an ACK preserving the reported snapshot does not notify twice');

    const fallback = { path: '/explorer', query: { path: 'notes/100%#plan.md' } };
    const next = f.controller.restorePage(a.tab.tabId, fallback);
    assert.deepEqual(pageRestoreRequest(a.view).page, fallback);
    f.ipc.request(C.pageRestoreAck, a.view.webContents, pageRestoreAck(pageRestoreRequest(a.view)));
    assert.deepEqual(await next, { status: 'restored' });
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tab.tabId, page: fallback }]);
    assert.equal(changes, 4);
  } finally { f.controller.dispose(); }
});

test('page restore rejects obsolete frame, revision, generation, binding and owner after await', async () => {
  for (const mutate of [
    (f, a) => { a.tab.revision++; },
    (f, a) => { a.tab.transferGeneration++; },
    (f, a) => { a.tab.ownerWindowId = '2'; },
    (f, a) => { a.tab.project = { ...a.tab.project, epoch: 'epoch-replaced' }; },
    (f, a) => { a.view.webContents.navigate(a.view.webContents.getURL()); },
    (f, a) => { a.view.webContents.mainFrame.url = 'https://outside.test/'; },
    (f, a) => { a.tab.state = 'crashed'; },
  ]) {
    let changes = 0;
    const f = fixture({ pageRestoreTimeout: 15, onPageChanged: () => { changes++; } }), a = f.addTab('A');
    try {
      const restoring = f.controller.restorePage(a.tab.tabId, { path: '/tasks' });
      const ack = pageRestoreAck(pageRestoreRequest(a.view));
      mutate(f, a);
      assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, ack));
      assert.deepEqual(await restoring, { status: 'stale' });
      assert.deepEqual(f.controller.snapshotPages(), []);
      assert.equal(changes, 0, 'stale restore never notifies persistence');
    } finally { f.controller.dispose(); }
  }
});

test('page restore returns explicit target failure, timeout, send failure and lifecycle cleanup results', async () => {
  const f = fixture({ pageRestoreTimeout: 10 }), a = f.addTab('A');
  try {
    assert.deepEqual(await f.controller.restorePage('missing', { path: '/tasks' }), { status: 'unavailable' });
    a.tab.state = 'loading';
    assert.deepEqual(await f.controller.restorePage(a.tab.tabId, { path: '/tasks' }), { status: 'unavailable' });
    a.tab.state = 'ready';
    for (const status of ['invalid', 'unavailable', 'failed', 'stale']) {
      const restoring = f.controller.restorePage(a.tab.tabId, { path: '/tasks', query: { task: 'deleted-task' } });
      f.ipc.request(C.pageRestoreAck, a.view.webContents, pageRestoreAck(pageRestoreRequest(a.view), status));
      assert.deepEqual(await restoring, { status });
      assert.deepEqual(f.controller.snapshotPages(), []);
    }
    const timeout = f.controller.restorePage(a.tab.tabId, { path: '/tasks' });
    const timedOutAck = pageRestoreAck(pageRestoreRequest(a.view));
    assert.deepEqual(await timeout, { status: 'failed' });
    assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, timedOutAck), /Stale/);
    const old = f.controller.restorePage(a.tab.tabId, { path: '/tasks' });
    const oldAck = pageRestoreAck(pageRestoreRequest(a.view));
    const newer = f.controller.restorePage(a.tab.tabId, { path: '/docs' });
    assert.deepEqual(await old, { status: 'stale' });
    assert.throws(() => f.ipc.request(C.pageRestoreAck, a.view.webContents, oldAck), /Stale/);
    f.ipc.request(C.pageRestoreAck, a.view.webContents, pageRestoreAck(pageRestoreRequest(a.view)));
    assert.deepEqual(await newer, { status: 'restored' });
    const send = a.view.webContents.send;
    a.view.webContents.send = () => { throw new Error('native send failed'); };
    assert.deepEqual(await f.controller.restorePage(a.tab.tabId, { path: '/tasks' }), { status: 'failed' });
    a.view.webContents.send = send;
    const release = f.controller.restorePage(a.tab.tabId, { path: '/tasks' });
    a.release(); assert.deepEqual(await release, { status: 'unavailable' });
    const b = f.addTab('B'), destroy = f.controller.restorePage(b.tab.tabId, { path: '/tasks' });
    b.view.webContents.close(); assert.deepEqual(await destroy, { status: 'unavailable' });
    const c = f.addTab('C'), dispose = f.controller.restorePage(c.tab.tabId, { path: '/tasks' });
    f.controller.dispose(); assert.deepEqual(await dispose, { status: 'unavailable' });
    assert.deepEqual(f.controller.snapshotPages(), []);
    assert.equal(f.ipc.handlers.size, 0);
  } finally { f.controller.dispose(); }
});

test('live transfer retains page snapshots without restoring, navigating or reviving old generation reports', async () => {
  const f = await liveFixture(), a = await f.open('A'), view = f.views[0], contents = view.webContents;
  let loads = 0;
  contents.loadURL = async () => { loads++; throw new Error('Transfer must not navigate'); };
  try {
    const report = pageReport(a, { path: '/tasks', query: { task: 'consumed-task' } });
    f.ipc.request(C.reportPage, contents, report);
    const restoring = f.controller.restorePage(a.tabId, { path: '/docs' });
    const ack = pageRestoreAck(pageRestoreRequest(view));
    const beforeTransferRequests = contents.sent.filter(([channel]) => channel === C.restorePage).length;
    const token = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const moved = await f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, token.token));
    assert.equal(moved.ownerWindowId, '2');
    assert.deepEqual(await restoring, { status: 'stale' });
    assert.equal(f.views.length, 1); assert.equal(contents.isDestroyed(), false); assert.equal(loads, 0);
    assert.equal(contents.sent.filter(([channel]) => channel === C.restorePage).length, beforeTransferRequests);
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tabId, page: report.page }]);
    assert.throws(() => f.ipc.request(C.reportPage, contents, report), /Stale/);
    assert.throws(() => f.ipc.request(C.pageRestoreAck, contents, ack), /Stale/);
    f.ipc.request(C.reportPage, contents, pageReport(f.registry.getTab(a.tabId), report.page));
    assert.deepEqual(f.controller.snapshotPages(), [{ tabId: a.tabId, page: report.page }]);
    assert.deepEqual(f.nativeCalls, [['ensure', '/projects/A']]);
  } finally { f.dispose(); }
});

function closeAck(f, view, prepared = true) {
  return f.ipc.request(C.closeAck, view.webContents, { ...closeRequest(view), prepared });
}

test('new edit after A prepare ACK while B saves vetoes disposal; retry fences input until commit', async () => {
  // Exercise the real TS renderer barrier with the existing live IPC fixture,
  // in memory only: no Electron, server, bundle or new test harness.
  const ts = require('../../frontend/node_modules/typescript');
  const { JSDOM } = require('../../frontend/node_modules/jsdom');
  const source = fs.readFileSync(path.join(__dirname, '../../frontend/src/desktop/close-barrier.ts'), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const f = await liveFixture(), saveB = deferred(), finalB = deferred();
  const clients = [], acks = [];
  let dirtyA = false, holdFinalB = false;
  try {
    const a = await f.open('A'); await f.open('B');
    f.registry.activateTab(a.tabId);
    for (let index = 0; index < 2; index++) {
      const dom = new JSDOM('<textarea></textarea>'), module = { exports: {} }, contents = f.views[index].webContents;
      dom.window.document.documentElement.inert = false;
      vm.runInNewContext(code, { module, exports: module.exports, window: dom.window, document: dom.window.document,
        require: name => { assert.equal(name, 'vue'); return require('../../frontend/node_modules/vue'); } });
      const barrier = module.exports.createCloseBarrier();
      if (index === 0) barrier.register(() => { dirtyA = false; return true; }, () => !dirtyA);
      else barrier.register(() => saveB.promise);
      let listener;
      const send = contents.send.bind(contents);
      contents.send = (channel, request) => { send(channel, request); if (channel === C.closeRequest) listener?.(request); };
      const connection = module.exports.connectRendererCloseBarrier({
        getState: async () => f.ipc.request(C.state, contents),
        onPrepareClose: callback => { listener = callback; return () => { listener = undefined; }; },
        acknowledgeClose: async ack => {
          acks.push(structuredClone(ack));
          if (index === 1 && holdFinalB && ack.phase === 'validate' && ack.prepared) await finalB.promise;
          f.ipc.request(C.closeAck, contents, structuredClone(ack));
          if (index === 0 && holdFinalB && ack.phase === 'validate') throw new Error('ACK response lost after delivery');
        },
      }, barrier);
      clients.push({ dom, barrier, connection }); await connection.ready;
    }
    const input = clients[0].dom.window.document.querySelector('textarea');
    input.addEventListener('beforeinput', () => { dirtyA = true; });
    const edit = () => input.dispatchEvent(new clients[0].dom.window.Event('beforeinput', { bubbles: true, cancelable: true }));
    const closing = f.controller.requestCloseWindow('1');
    await new Promise(setImmediate);
    assert.equal(acks.length, 1); assert.equal(acks[0].prepared, true);
    assert.equal(edit(), true); assert.equal(dirtyA, true); assert.equal(clients[0].barrier.validate(), false);
    saveB.resolve(true);
    assert.equal(await closing, false);
    assert.ok(f.views.every(view => !view.webContents.isDestroyed()));
    assert.equal(dirtyA, true, 'the new edit remains in the same live renderer');
    assert.ok(clients.every(client => client.dom.window.document.documentElement.inert === false));
    assert.equal(edit(), true, 'aborted close restores editing');

    const destroy = f.views.map(view => {
      const close = view.webContents.close.bind(view.webContents);
      view.webContents.close = () => {}; // Native disposal need not be synchronous.
      return close;
    });
    holdFinalB = true;
    const retry = f.controller.requestCloseWindow('1');
    await new Promise(setImmediate);
    assert.ok(clients.every(client => client.dom.window.document.documentElement.inert === true));
    assert.equal(edit(), false); assert.equal(dirtyA, false, 'final ACK holds the fence while the other ACK travels');
    assert.ok(f.views.every(view => !view.webContents.isDestroyed()));
    finalB.resolve(); assert.equal(await retry, true);
    assert.ok(clients.every(client => client.dom.window.document.documentElement.inert === true));
    assert.ok(f.views.every(view => closeRequest(view).phase === 'validate'), 'no release before actual disposal');
    for (const close of destroy) close();
    assert.ok(f.views.every(view => view.webContents.isDestroyed()));
    assert.deepEqual(f.nativeCalls, [['ensure', '/projects/A'], ['ensure', '/projects/B']]);
  } finally {
    for (const client of clients) { client.connection.dispose(); client.dom.window.close(); }
    f.dispose();
  }
});

test('last-tab close consumes phased renderer ACKs, coalesces repeats, and disposes only UI', async () => {
  const empty = [], f = await liveFixture({ onWindowEmpty: id => empty.push(id) }), a = await f.open('A');
  const request = f.tabRequest(a), view = f.views[0];
  const first = f.ipc.request(C.close, f.w1.webContents, request);
  const repeated = f.ipc.request(C.close, f.w1.webContents, request);
  assert.equal(view.webContents.isDestroyed(), false);
  assert.equal(view.webContents.sent.filter(([channel]) => channel === C.closeRequest).length, 1);
  const ack = { ...closeRequest(view), prepared: true };
  closeAck(f, view);
  await new Promise(setImmediate);
  assert.equal(closeRequest(view).phase, 'validate');
  assert.throws(() => f.ipc.request(C.closeAck, view.webContents, ack), /Stale close ACK/);
  closeAck(f, view);
  assert.deepEqual(await first, { closed: true }); assert.deepEqual(await repeated, { closed: true });
  assert.equal(view.webContents.isDestroyed(), true); assert.equal(f.registry.getTab(a.tabId), null);
  assert.deepEqual(empty, ['1']); assert.deepEqual(f.nativeCalls, [['ensure', '/projects/A']]);
  assert.throws(() => f.ipc.request(C.closeAck, view.webContents, ack));
  assert.ok(f.ipc.handlers.has(C.open), 'app IPC remains registered at zero tabs'); f.dispose();
});

test('window close keeps all views on cancel/error ACK and closes all only after every approval', async () => {
  const f = await liveFixture(); await f.open('A'); await f.open('B');
  try {
    const cancel = f.controller.requestCloseWindow('1');
    assert.equal(f.controller.requestCloseWindow('1'), cancel);
    closeAck(f, f.views[0]); closeAck(f, f.views[1], false);
    assert.equal(await cancel, false);
    assert.ok(f.views.every(view => !view.webContents.isDestroyed()));
    const close = f.controller.requestCloseWindow('1');
    closeAck(f, f.views[0]); await Promise.resolve();
    assert.ok(f.views.every(view => !view.webContents.isDestroyed()), 'no partial window disposal');
    closeAck(f, f.views[1]);
    await new Promise(setImmediate);
    for (const view of f.views) { assert.equal(closeRequest(view).phase, 'validate'); closeAck(f, view); }
    assert.equal(await close, true);
    assert.ok(f.views.every(view => view.webContents.isDestroyed()));
    assert.equal(f.registry.getWindow('1').tabIds.length, 0);
  } finally { f.dispose(); }
});

test('app close barrier covers both windows without disposal and holds fences/gate through callback', async () => {
  const f = await liveFixture(), a = await f.open('A'); await f.open('B', '2');
  const [av, bv] = f.views, callbackDone = deferred();
  let calls = 0;
  try {
    const cancelled = f.controller.withAppCloseBarrier(() => { calls++; });
    assert.equal(closeRequest(av).phase, 'prepare'); assert.equal(closeRequest(bv).phase, 'prepare');
    closeAck(f, av); closeAck(f, bv, false);
    assert.equal(await cancelled, false); assert.equal(calls, 0);
    assert.ok(f.views.every(view => closeRequest(view).phase === 'release'));

    const failed = f.controller.withAppCloseBarrier(() => { calls++; });
    for (const view of f.views) closeAck(f, view);
    await new Promise(setImmediate);
    assert.ok(f.views.every(view => closeRequest(view).phase === 'validate'));
    closeAck(f, av); closeAck(f, bv, false);
    assert.equal(await failed, false); assert.equal(calls, 0);
    assert.ok(f.views.every(view => closeRequest(view).phase === 'release'));

    const error = new Error('stop failed');
    const protectedWork = f.controller.withAppCloseBarrier(async () => {
      calls++;
      assert.ok(f.views.every(view => closeRequest(view).phase === 'validate'));
      assert.equal(f.registry.snapshot().tabs.length, 2);
      await callbackDone.promise;
      throw error;
    });
    const rejected = assert.rejects(protectedWork, value => value === error);
    for (const view of f.views) closeAck(f, view);
    await new Promise(setImmediate);
    closeAck(f, av); await new Promise(setImmediate);
    assert.equal(calls, 0, 'the other window must seal before protected work');
    closeAck(f, bv); await new Promise(setImmediate);
    assert.equal(calls, 1);
    assert.ok(f.views.every(view => !view.webContents.isDestroyed() && closeRequest(view).phase === 'validate'));
    await assert.rejects(f.controller.requestCloseWindow('1'), /busy/);
    assert.throws(() => f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a)), /busy/);
    callbackDone.resolve(); await rejected;
    assert.ok(f.views.every(view => !view.webContents.isDestroyed() && closeRequest(view).phase === 'release'));
    assert.equal(f.registry.snapshot().tabs.length, 2);

    // A successful quit can synchronously dispose everything inside its work.
    const quit = f.controller.withAppCloseBarrier(() => { f.dispose(); return true; });
    for (const view of f.views) closeAck(f, view);
    await new Promise(setImmediate);
    for (const view of f.views) closeAck(f, view);
    assert.equal(await quit, true);
    assert.ok(f.views.every(view => view.webContents.isDestroyed()));
    assert.equal(await f.controller.withAppCloseBarrier(() => { calls++; }), false);
    assert.equal(calls, 1);
  } finally { f.dispose(); }
});

test('close ACK rejects shell, foreign view, old frame and forged tuple without consuming approval', async () => {
  const f = await liveFixture(), a = await f.open('A'); await f.open('B', '2');
  try {
    const closing = f.ipc.request(C.close, f.w1.webContents, f.tabRequest(a)), view = f.views[0];
    const ack = { ...closeRequest(view), prepared: true };
    assert.throws(() => f.ipc.request(C.closeAck, f.w1.webContents, ack), /Project ACK/);
    assert.throws(() => f.ipc.request(C.closeAck, f.views[1].webContents, ack), /Stale close ACK/);
    assert.throws(() => f.ipc.request(C.closeAck, view.webContents, { ...ack, epoch: 'other' }), /Stale close ACK/);
    assert.throws(() => f.ipc.request(C.closeAck, view.webContents, { ...ack, phase: 'validate' }), /Stale close ACK/);
    assert.throws(() => f.ipc.handlers.get(C.closeAck)({ sender: view.webContents, senderFrame: { ...view.webContents.mainFrame } }, ack), /Untrusted/);
    closeAck(f, view, false); assert.deepEqual(await closing, { closed: false });
    assert.equal(view.webContents.isDestroyed(), false);
  } finally { f.dispose(); }
});

test('missing ACK times out closed-safe and stale revisions cannot consume an old approval', async () => {
  const f = await liveFixture({ closeTimeout: 10 }), a = await f.open('A');
  try {
    assert.deepEqual(await f.ipc.request(C.close, f.w1.webContents, f.tabRequest(a)), { closed: false });
    const closing = f.controller.requestCloseWindow('1');
    f.registry.setContentInsets('1', { top: 50 });
    closeAck(f, f.views[0]); await assert.rejects(closing, /Stale window/);
    assert.equal(f.views[0].webContents.isDestroyed(), false);
  } finally { f.dispose(); }
});

test('close and native picker exclude transfer; transferred last-view survives empty source disposal', async () => {
  let f;
  const empty = id => { if (f.registry.getWindow(id)?.tabIds.length === 0) (id === '1' ? f.w1 : f.w2).close(); };
  const picker = deferred();
  f = await liveFixture({ coreOptions: { onWindowEmpty: empty }, onWindowEmpty: empty,
    shellServices: { pickDirectory: () => picker.promise } });
  const a = await f.open('A'), view = f.views[0];
  try {
    const modal = f.ipc.request(C.pickDirectory, f.w1.webContents);
    await assert.rejects(f.controller.requestCloseWindow('1'), /busy/);
    await assert.rejects(async () => f.ipc.request(C.detach, f.w1.webContents, f.tabRequest(a)), /busy/);
    picker.resolve({ cancelled: true }); await modal;
    const closing = f.controller.requestCloseWindow('1');
    await assert.rejects(async () => f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a)), /busy/);
    closeAck(f, view, false); assert.equal(await closing, false);
    const lease = f.ipc.request(C.transferBegin, f.w1.webContents, f.tabRequest(a));
    const attach = deferred(); view.webContents.executeJavaScript = () => attach.promise;
    const moving = f.ipc.request(C.transfer, f.w1.webContents, f.transferRequest(a, lease.token));
    await assert.rejects(f.controller.requestCloseWindow('1'), /busy/);
    attach.resolve(true); const moved = await moving;
    assert.equal(moved.ownerWindowId, '2'); assert.equal(f.w1.isDestroyed(), true);
    assert.equal(view.webContents.isDestroyed(), false);
    assert.equal(f.registry.getTab(a.tabId).ownerWindowId, '2');
    assert.equal(f.views.length, 1); assert.deepEqual(f.nativeCalls, [['ensure', '/projects/A']]);
  } finally { f.dispose(); }
});
