'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const { DesktopTabs, createBackendAdapter, createElectronAdapter, canonicalProjectRoot } = require('../tabs.cjs');

const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const binding = (name = 'A', epoch = 'epoch-1') => ({ root: `/projects/${name}`, project_id: `project-${name}`, epoch });

class Contents extends EventEmitter {
  destroyed = false;
  loads = [];
  focuses = 0;
  isDestroyed() { return this.destroyed; }
  loadURL(url) { this.loads.push(url); return Promise.resolve(); }
  focus() { this.focuses++; }
  close() { if (!this.destroyed) { this.destroyed = true; this.emit('destroyed'); } }
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; }
}
class Window extends EventEmitter {
  destroyed = false;
  minimized = false;
  size = [900, 700];
  shown = 0;
  focused = 0;
  restores = 0;
  children = new Set();
  webContents = new Contents();
  constructor(id) {
    super(); this.id = id;
    this.contentView = {
      addChildView: view => { this.children.add(view); },
      removeChildView: view => { this.children.delete(view); },
    };
  }
  isDestroyed() { return this.destroyed; }
  isMinimized() { return this.minimized; }
  restore() { this.minimized = false; this.restores++; }
  show() { this.shown++; }
  focus() { this.focused++; }
  getContentSize() { return this.size; }
  close() { this.destroyed = true; this.emit('closed'); }
}
function fixture(options = {}) {
  const views = [], calls = [], loaded = [], destroyed = [], nativeCalls = [];
  let failAttach = false;
  const electron = {
    createProjectView(project) {
      const view = { webContents: new Contents(), project, visible: false };
      views.push(view); return view;
    },
    async loadProjectView(view, project) { loaded.push(view); await options.load?.(view, project); },
    attachView(window, view) {
      nativeCalls.push(['attach', window, view]);
      if (failAttach) { failAttach = false; throw new Error('Attach failed'); }
      window.children.add(view); view.visible = true;
    },
    parkView(window, view) { nativeCalls.push(['park', window, view]); window.children.delete(view); view.visible = false; },
    setBounds(view, bounds) { nativeCalls.push(['bounds', view, bounds]); view.bounds = bounds; },
    focusView(view) { nativeCalls.push(['focus', view]); view.webContents.focus(); },
    destroyView(view) { nativeCalls.push(['destroy', view]); destroyed.push(view); view.webContents.close(); },
    async waitForTransferAttach() { return true; },
  };
  Object.assign(electron, options.electron);
  const backend = { async ensureProject(request) {
    calls.push(request);
    return options.ensure ? options.ensure(request) : binding(request.path.split('/').at(-1));
  } };
  const tabs = new DesktopTabs({ electron, backend,
    canonicalizeRoot: options.canonicalize ?? (async value => value.toLowerCase().replace('/alias/', '/projects/')),
    ...options.controller });
  const w1 = new Window(1), w2 = new Window(2);
  tabs.registerWindow(w1, { insets: { top: 48 } }); tabs.registerWindow(w2);
  return { tabs, w1, w2, views, calls, loaded, destroyed, nativeCalls, electron,
    failAttach() { failAttach = true; }, open: (name, windowId = '1', extra = {}) => tabs.openProject(`/projects/${name}`, { windowId, ...extra }) };
}

test('aliases and concurrent windows coalesce ensure and reserve one live view', async () => {
  const pending = deferred(); const f = fixture({ ensure: () => pending.promise });
  const a = f.open('A'); const alias = f.tabs.openProject('/ALIAS/A', { windowId: '2' });
  await tick(); assert.equal(f.calls.length, 1); assert.equal(f.views.length, 0);
  pending.resolve(binding());
  const [first, second] = await Promise.all([a, alias]);
  assert.equal(first.tabId, second.tabId); assert.equal(second.ownerWindowId, '1');
  assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1);
  assert.equal(f.w1.children.size, 1); assert.equal(f.w2.children.size, 0);
  assert.deepEqual(f.tabs.getWindow('2').tabIds, []);
  assert.equal(f.w2.focused, 0); assert.ok(f.w1.focused > 0);
  f.tabs.dispose();
});

test('backend-confirmed root deduplicates aliases missed by the preliminary key', async () => {
  const gate = deferred(); const f = fixture({ ensure: () => binding(), canonicalize: async x => x, load: () => gate.promise });
  const a = f.open('A'); await tick();
  const b = f.tabs.openProject('/junction-to-A', { windowId: '2' }); await tick();
  assert.equal(f.calls.length, 2); assert.equal(f.views.length, 1);
  gate.resolve(); const [first, second] = await Promise.all([a, b]);
  assert.equal(first.tabId, second.tabId); assert.equal(second.ownerWindowId, '1');
  f.tabs.dispose();
});

test('late open from another window focuses owner, never moves or reloads the view', async () => {
  const late = deferred();
  const f = fixture({ canonicalize: async x => x, ensure: request => request.path === '/late-alias' ? late.promise : binding() });
  const pending = f.tabs.openProject('/late-alias', { windowId: '2' }); await tick();
  const first = await f.open('A');
  late.resolve(binding()); const result = await pending;
  assert.equal(result.tabId, first.tabId); assert.equal(result.ownerWindowId, '1');
  assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1); assert.equal(f.w2.children.size, 0);
  f.tabs.dispose();
});

test('two projects retain distinct renderers across two windows', async () => {
  const f = fixture(); const [a, b] = await Promise.all([f.open('A'), f.open('B', '2')]);
  assert.notEqual(a.tabId, b.tabId); assert.equal(f.views.length, 2);
  assert.equal(f.w1.children.size, 1); assert.equal(f.w2.children.size, 1);
  assert.equal(a.project.project_id, 'project-A'); assert.equal(b.project.project_id, 'project-B');
  f.tabs.activateTab(a.tabId); assert.equal(f.loaded.length, 2);
  f.tabs.dispose();
});

test('activation parks inactive view and preserves contents, bounds and renderer state', async () => {
  const f = fixture(); const a = await f.open('A'); const original = f.views[0];
  original.sentinel = { draft: 'unsaved', file: Buffer.from('attachment') };
  const b = await f.open('B');
  assert.equal(original.visible, false); assert.equal(f.w1.children.has(original), false);
  assert.equal(original.webContents.isDestroyed(), false);
  f.w1.minimized = true;
  f.tabs.activateTab(a.tabId);
  assert.equal(f.w1.children.size, 1); assert.equal(f.w1.children.has(original), true);
  assert.equal(f.views[1].visible, false); assert.equal(f.w1.restores, 1);
  assert.deepEqual(original.bounds, { x: 0, y: 48, width: 900, height: 652 });
  f.w1.size = [600, 400]; f.w1.emit('resize');
  assert.deepEqual(original.bounds, { x: 0, y: 48, width: 600, height: 352 });
  f.tabs.setContentInsets('1', { top: 60, left: 4, right: 5, bottom: 6 });
  assert.deepEqual(original.bounds, { x: 4, y: 60, width: 591, height: 334 });
  assert.equal(original.sentinel.draft, 'unsaved'); assert.equal(f.loaded.length, 2);
  assert.equal(f.tabs.getTab(b.tabId).ownerWindowId, '1');
  assert.equal(f.calls.length, 2); f.tabs.dispose();
});

test('snapshots cannot mutate ownership, immutable binding or ordered tab ids', async () => {
  const f = fixture(); const tab = await f.open('A');
  assert.throws(() => { tab.ownerWindowId = '2'; }, TypeError);
  assert.throws(() => { tab.project.epoch = 'other'; }, TypeError);
  assert.throws(() => { f.tabs.getWindow('1').tabIds.push('foreign'); }, TypeError);
  assert.equal('view' in tab, false); assert.equal('window' in f.tabs.getWindow('1'), false);
  assert.equal('shell' in f.tabs.getWindow('1'), false);
  assert.equal(tab.transferGeneration, 0); f.tabs.dispose();
});

test('failed ensure leaves existing project active and permits an explicit retry', async () => {
  let failed = true;
  const f = fixture({ ensure: request => {
    if (request.path.endsWith('/B') && failed) throw Object.assign(new Error('not initialized'), { status: 409 });
    return binding(request.path.split('/').at(-1));
  } });
  const a = await f.open('A');
  await assert.rejects(f.open('B'), { status: 409 });
  assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId); assert.equal(f.views.length, 1);
  failed = false; await f.open('B', '1', { create: true });
  assert.deepEqual(f.calls.map(x => x.create), [false, false, true]); f.tabs.dispose();
});

test('coalesced failure rejects all callers and never escalates create without a retry', async () => {
  const gate = deferred(); let fail = true;
  const f = fixture({ ensure: () => fail ? gate.promise : binding() });
  const first = f.open('A'); const second = f.open('A', '2', { create: true });
  const results = Promise.allSettled([first, second]); await tick();
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].create, false);
  gate.reject(new Error('preflight failed'));
  for (const result of await results) assert.equal(result.status, 'rejected');
  assert.equal(f.views.length, 0); fail = false;
  const retried = await f.open('A', '2', { create: true });
  assert.equal(retried.ownerWindowId, '2'); assert.equal(f.calls[1].create, true); f.tabs.dispose();
});

test('an adapter cannot reuse an incumbent contents for another project', async () => {
  const f = fixture(); const a = await f.open('A');
  f.electron.createProjectView = () => f.views[0];
  await assert.rejects(f.open('B'), /unique live project view/);
  assert.equal(f.tabs.snapshot().tabs.length, 1); assert.equal(f.tabs.getTab(a.tabId).state, 'ready');
  assert.equal(f.views[0].webContents.isDestroyed(), false); f.tabs.dispose();
});

test('failed load cleans indexes/listeners/contents and retry creates just one replacement', async () => {
  let failed = true;
  const f = fixture({ load: (_view, p) => { if (p.project_id === 'project-B' && failed) throw new Error('Load failed'); } });
  const a = await f.open('A');
  await assert.rejects(f.open('B'), /Load failed/);
  assert.equal(f.tabs.snapshot().tabs.length, 1); assert.equal(f.destroyed.length, 1);
  assert.equal(f.views[1].webContents.eventNames().length, 0);
  assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId);
  failed = false; await f.open('B'); assert.equal(f.tabs.snapshot().tabs.length, 2); f.tabs.dispose();
});

test('initial native attach failure rolls back open without replacing previous active view', async () => {
  const f = fixture(); const a = await f.open('A'); f.failAttach();
  await assert.rejects(f.open('B'), /Attach failed/);
  assert.equal(f.tabs.snapshot().tabs.length, 1); assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId);
  assert.equal(f.w1.children.has(f.views[0]), true); assert.equal(f.views[1].webContents.isDestroyed(), true);
  await f.open('B'); f.tabs.dispose();
});

test('activation failure keeps both existing records and previous active ownership', async () => {
  const f = fixture(); const a = await f.open('A'); const b = await f.open('B'); f.failAttach();
  assert.throws(() => f.tabs.activateTab(a.tabId), /Attach failed/);
  assert.equal(f.tabs.getWindow('1').activeTabId, b.tabId); assert.equal(f.tabs.snapshot().tabs.length, 2);
  f.tabs.activateTab(a.tabId); assert.equal(f.loaded.length, 2); f.tabs.dispose();
});

test('owner close during ensure cannot publish a late view or use a recycled window id', async () => {
  const gate = deferred(); const f = fixture({ ensure: () => gate.promise });
  const pending = f.open('A'); const rejection = assert.rejects(pending, /Window|window/);
  await tick(); f.tabs.unregisterWindow('1'); f.tabs.registerWindow(new Window(1));
  gate.resolve(binding()); await rejection;
  assert.equal(f.views.length, 0); assert.deepEqual(f.tabs.getWindow('1').tabIds, []); f.tabs.dispose();
});

test('owner close during load destroys the late view and does not fall back to another window', async () => {
  const gate = deferred(); const f = fixture({ load: () => gate.promise });
  const pending = f.open('A'); const rejection = assert.rejects(pending, /closed|Window/);
  await tick(); f.w1.close(); gate.resolve(); await rejection;
  assert.equal(f.views[0].webContents.isDestroyed(), true); assert.equal(f.tabs.snapshot().tabs.length, 0);
  assert.deepEqual(f.tabs.getWindow('2').tabIds, []); f.tabs.dispose();
});

test('dispose during load tears down listeners and late completion cannot republish', async () => {
  const gate = deferred(); const f = fixture({ load: () => gate.promise });
  const pending = f.open('A'); const rejected = assert.rejects(pending, /closed|Window/);
  await tick(); f.tabs.dispose();
  assert.equal(f.views[0].webContents.eventNames().length, 0);
  assert.equal(f.views[0].webContents.isDestroyed(), true);
  gate.resolve(); await rejected; assert.equal(f.tabs.snapshot().tabs.length, 0);
});

test('dispose invalidates outstanding canonicalization and ensure without starting surfaces', async () => {
  for (const stage of ['canonicalize', 'ensure']) {
    const gate = deferred(); const f = fixture(stage === 'canonicalize'
      ? { canonicalize: () => gate.promise } : { ensure: () => gate.promise });
    const pending = f.open('A'); const rejection = assert.rejects(pending, /Window|window|disposed/);
    await tick(); f.tabs.dispose(); gate.resolve(stage === 'canonicalize' ? '/projects/a' : binding()); await rejection;
    assert.equal(f.views.length, 0); assert.deepEqual(f.tabs.snapshot(), { windows: [], tabs: [] });
  }
});

test('closing an active tab selects a parked survivor, with idempotent UI-only cleanup', async () => {
  const f = fixture(); const a = await f.open('A'); const b = await f.open('B');
  assert.equal(f.tabs.closeTab(b.tabId), true); assert.equal(f.tabs.closeTab(b.tabId), false);
  assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId); assert.equal(f.loaded.length, 2);
  assert.equal(f.views[1].webContents.eventNames().length, 0);
  f.tabs.unregisterWindow('1'); assert.equal(f.w1.children.size, 0);
  assert.equal(f.w1.listenerCount('resize'), 0); assert.equal(f.w1.listenerCount('closed'), 0);
  assert.equal(f.tabs.unregisterWindow('1'), false); f.tabs.dispose(); f.tabs.dispose();
  assert.equal(f.destroyed.length, 2); assert.equal(f.calls.length, 2);
});

test('unexpected renderer destruction cleans only its record and allows reopen', async () => {
  const f = fixture(); const a = await f.open('A'); const b = await f.open('B', '2');
  f.views[0].webContents.close(); assert.equal(f.tabs.getTab(a.tabId), null);
  assert.equal(f.tabs.getTab(b.tabId).state, 'ready'); await f.open('A');
  assert.equal(f.views.length, 3); f.tabs.dispose();
});

test('renderer crash is explicit and never reloads or rebinds the project', async () => {
  const f = fixture(); const a = await f.open('A');
  f.views[0].webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.equal(f.tabs.getTab(a.tabId).state, 'crashed'); f.tabs.activateTab(a.tabId);
  assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1); f.tabs.dispose();
});

test('conflicting epoch/id for a confirmed root is rejected, never mutates live binding', async () => {
  const f = fixture({ canonicalize: async x => x, ensure: req => binding('A', req.path === '/changed' ? 'epoch-2' : 'epoch-1') });
  const a = await f.open('A');
  await assert.rejects(f.tabs.openProject('/changed', { windowId: '2' }), /binding changed/);
  assert.equal(f.tabs.getTab(a.tabId).project.epoch, 'epoch-1'); assert.equal(f.views.length, 1); f.tabs.dispose();
});

test('bad binding/create/insets and duplicate window cannot corrupt records', async () => {
  const f = fixture({ ensure: () => ({ ...binding(), project_id: '../legacy' }) });
  await assert.rejects(f.open('A'), /Invalid project binding/);
  await assert.rejects(f.open('A', '1', { create: 'yes' }), /boolean/);
  assert.throws(() => f.tabs.setContentInsets('1', { top: -1 }), /Insets/);
  assert.throws(() => f.tabs.registerWindow(f.w1), /already registered/);
  assert.equal(f.views.length, 0); f.tabs.dispose();
});

test('backend adapter uses only scoped ensure on :8765, including authenticated preflight', async () => {
  const calls = []; let legacySelection = 'legacy-project'; let launches = 0;
  const backend = createBackendAdapter({ token: () => 'secret', fetch: async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith('/api/project/open')) legacySelection = 'changed';
    if (url.endsWith('/launcher')) launches++;
    return { ok: true, json: async () => binding() };
  } });
  const f = fixture(); const tabs = new DesktopTabs({ backend, electron: f.electron, canonicalizeRoot: async x => x });
  tabs.registerWindow(new Window(3)); const a = await tabs.openProject('/projects/A', { windowId: '3' });
  tabs.activateTab(a.tabId); await tabs.openProject('/projects/A', { windowId: '3' });
  assert.equal(calls.length, 1); assert.equal(calls[0][0], 'http://127.0.0.1:8765/api/projects/open');
  assert.deepEqual(JSON.parse(calls[0][1].body), { path: '/projects/A', create: false });
  assert.equal(calls[0][1].headers.Authorization, 'Bearer secret');
  assert.equal(calls[0][1].redirect, 'error'); assert.equal(legacySelection, 'legacy-project'); assert.equal(launches, 0);
  tabs.dispose(); f.tabs.dispose();
});

test('backend failure preserves status without retry, implicit create or legacy fallback', async () => {
  let calls = 0; const backend = createBackendAdapter({ fetch: async () => { calls++; return { ok: false, status: 409 }; } });
  await assert.rejects(backend.ensureProject({ path: '/new' }), { status: 409 }); assert.equal(calls, 1);
});

test('native adapter creates secure scoped contents, parks same view and runs cleanup once', async () => {
  let preferences, disposed = 0, acks = 0;
  class View {
    webContents = new Contents();
    constructor(options) { preferences = options.webPreferences; }
    setVisible(visible) { this.visible = visible; }
    setBounds(bounds) { this.bounds = bounds; }
  }
  const electron = createElectronAdapter({ WebContentsView: View, webPreferences: { nodeIntegration: true, sandbox: false },
    configureProjectView: () => () => { disposed++; }, waitForProjectReady: async () => { acks++; } });
  const project = Object.freeze(binding()); const view = electron.createProjectView(project); const window = new Window(1);
  assert.equal(preferences.nodeIntegration, false); assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.sandbox, true); assert.equal(preferences.webSecurity, true); assert.equal(preferences.backgroundThrottling, false);
  await electron.loadProjectView(view, project); assert.equal(acks, 1);
  assert.deepEqual(view.webContents.loads, ['http://127.0.0.1:8765/p/project-A/']);
  for (const event of ['will-navigate', 'will-redirect']) {
    for (const url of ['http://127.0.0.1:8765/', 'http://127.0.0.1:8765/p/other/docs', 'https://evil.invalid/p/project-A/']) {
      let prevented = false; view.webContents.emit(event, { preventDefault() { prevented = true; } }, url);
      assert.equal(prevented, true);
    }
    view.webContents.emit(event, { preventDefault() { assert.fail('same project navigation blocked'); } }, 'http://127.0.0.1:8765/p/project-A/docs');
  }
  assert.deepEqual(view.webContents.windowOpenHandler({ url: 'https://evil.invalid' }), { action: 'deny' });
  electron.attachView(window, view); electron.setBounds(view, { x: 0, y: 30, width: 100, height: 70 });
  electron.parkView(window, view); assert.equal(view.visible, false); assert.equal(window.children.size, 0);
  electron.attachView(window, view); electron.focusView(view); assert.equal(view.webContents.loads.length, 1);
  electron.destroyView(view); electron.destroyView(view); assert.equal(disposed, 1);
  assert.equal(view.webContents.eventNames().length, 0);
});

test('canonical root resolves real junction/symlink aliases and rejects missing roots', async t => {
  const temp = path.resolve(__dirname, '../../.voxarium_studio/temp/TASK-60'); fs.mkdirSync(temp, { recursive: true });
  const fixture = fs.mkdtempSync(path.join(temp, 'canonical-'));
  t.after(() => {
    assert.ok(path.resolve(fixture).startsWith(temp + path.sep));
    fs.rmSync(fixture, { recursive: true, force: true });
  });
  const actual = path.join(fixture, 'actual'), alias = path.join(fixture, 'alias');
  fs.mkdirSync(actual); fs.symlinkSync(actual, alias, process.platform === 'win32' ? 'junction' : 'dir');
  assert.equal(await canonicalProjectRoot(actual), await canonicalProjectRoot(alias));
  if (process.platform === 'win32') assert.equal(await canonicalProjectRoot(actual.toUpperCase()), await canonicalProjectRoot(actual));
  await assert.rejects(canonicalProjectRoot(path.join(fixture, 'missing')));
});

// VS-51 / dev spec §5.3: native membership is deliberately separate from the
// attach ACK. These controller tests do not claim native Electron validation.
function transferSource(f, tabId, sender) {
  const tab = f.tabs.getTab(tabId), window = f.tabs.getWindow(tab.ownerWindowId);
  return { windowId: window.windowId, windowRevision: window.revision, tabRevision: tab.revision, sender };
}
function transferTarget(f, windowId, sender, extra = {}) {
  const window = f.tabs.getWindow(windowId);
  return { targetWindowId: window.windowId, targetWindowRevision: window.revision, sender, ...extra };
}
function startTransfer(f, tabId, windowId, sender = {}, extra = {}) {
  const ticket = f.tabs.beginTransfer(tabId, transferSource(f, tabId, sender));
  return f.tabs.transferTab(ticket.token, transferTarget(f, windowId, sender, extra));
}
function liveFixture(t, options) {
  const f = fixture(options);
  t.after(() => f.tabs.dispose());
  return f;
}
function assertRetained(f, tabId, view, ownerWindowId) {
  assert.equal(f.tabs.getTab(tabId).ownerWindowId, ownerWindowId);
  assert.equal(view.webContents.isDestroyed(), false);
  assert.equal(f.destroyed.includes(view), false);
  assert.equal(f.views.filter(item => item === view).length, 1);
}

test('rollback cannot resurrect a transferred or target tab destroyed during attach ACK', async t => {
  for (const destroyedIndex of [0, 1]) {
    const ack = deferred(), f = liveFixture(t, { electron: { waitForTransferAttach: () => ack.promise } });
    const a = await f.open('A'); await f.open('B', '2');
    const moving = startTransfer(f, a.tabId, '2');
    f.views[destroyedIndex].webContents.close(); ack.resolve(true);
    await assert.rejects(moving);
    const snapshot = f.tabs.snapshot();
    for (const window of snapshot.windows) {
      for (const id of window.tabIds) assert.equal(f.tabs.getTab(id)?.ownerWindowId, window.windowId);
      assert.ok(window.activeTabId === null || window.tabIds.includes(window.activeTabId));
    }
    assert.equal(snapshot.tabs.length, 1);
  }
});

test('after-effect source-survivor attach failure rolls back to exactly one visible source view', async t => {
  const f = liveFixture(t); await f.open('C'); const a = await f.open('A'); await f.open('B', '2');
  const survivor = f.views[0], transferred = f.views[1], attach = f.electron.attachView;
  let armed = true;
  f.electron.attachView = (window, candidate) => {
    attach(window, candidate);
    if (armed && window === f.w1 && candidate === survivor) { armed = false; throw new Error('survivor after-effect'); }
  };
  await assert.rejects(startTransfer(f, a.tabId, '2'), /survivor after-effect/);
  assert.deepEqual([...f.w1.children], [transferred]); assert.equal(survivor.visible, false);
  assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId); assertRetained(f, a.tabId, transferred, '1');
});

test('a target active view whose rollback attach fails can reattach on its next activation', async t => {
  const f = liveFixture(t), a = await f.open('A'), b = await f.open('B', '2');
  const attach = f.electron.attachView, focus = f.electron.focusView;
  let focusFault = true, attachFault = true;
  f.electron.focusView = candidate => {
    if (focusFault && candidate === f.views[0]) { focusFault = false; throw new Error('forward focus failed'); }
    focus(candidate);
  };
  f.electron.attachView = (window, candidate) => {
    if (attachFault && window === f.w2 && candidate === f.views[1]) { attachFault = false; throw new Error('rollback attach failed'); }
    attach(window, candidate);
  };
  await assert.rejects(startTransfer(f, a.tabId, '2'), /forward focus failed/);
  assert.equal(f.w2.children.size, 0);
  assert.equal(f.tabs.activateTab(b.tabId).tabId, b.tabId);
  assert.deepEqual([...f.w2.children], [f.views[1]]); assert.equal(f.views[1].visible, true);
  assertRetained(f, b.tabId, f.views[1], '2');
});

test('commit uses target geometry after a resize while the same contents ACK is pending', async t => {
  const ack = deferred(), f = liveFixture(t, { electron: { waitForTransferAttach: () => ack.promise } });
  const a = await f.open('A'), moving = startTransfer(f, a.tabId, '2');
  f.w2.size = [1200, 850]; f.w2.emit('resize'); ack.resolve(true); await moving;
  assert.deepEqual(f.views[0].bounds, { x: 0, y: 0, width: 1200, height: 850 });
});

test('source lost synchronously during rollback focus is recovered, not left with a missing owner', async t => {
  let f, recovery;
  f = liveFixture(t, { controller: { createShell: async () => {
    recovery = new Window(3); f.tabs.registerWindow(recovery); return recovery;
  } } });
  const a = await f.open('A'), attach = f.electron.attachView, focus = f.electron.focusView;
  let failed = false;
  f.electron.attachView = (window, view) => {
    if (!failed && window === f.w2) { failed = true; throw new Error('forward attach failed'); }
    attach(window, view);
  };
  let destroyed = false;
  f.electron.focusView = view => {
    if (!destroyed) { destroyed = true; f.w1.close(); }
    focus(view);
  };
  await assert.rejects(startTransfer(f, a.tabId, '2'), /forward attach failed/);
  assert.ok(recovery); assertRetained(f, a.tabId, f.views[0], '3');
  assert.equal(recovery.children.has(f.views[0]), true);
});

test('contents destroyed in commit focus cannot strand the ready source-survivor', async t => {
  const f = liveFixture(t), c = await f.open('C'), a = await f.open('A');
  const focus = f.electron.focusView;
  f.electron.focusView = candidate => { if (candidate === f.views[1]) candidate.webContents.close(); else focus(candidate); };
  await assert.rejects(startTransfer(f, a.tabId, '2'));
  assert.equal(f.tabs.getTab(a.tabId), null);
  assert.deepEqual(f.tabs.getWindow('1').tabIds, [c.tabId]);
  assert.equal(f.tabs.getWindow('1').activeTabId, c.tabId);
  assert.deepEqual([...f.w1.children], [f.views[0]]); assert.equal(f.views[0].visible, true);
});

test('transfer keeps source ownership, order, active tab and revisions until explicit attach ACK', async t => {
  const ack = deferred(), entered = deferred();
  const f = liveFixture(t, { electron: { waitForTransferAttach(view, target, { signal }) {
    entered.resolve({ view, target, signal }); return ack.promise;
  } } });
  const a = await f.open('A'), b = await f.open('B', '2'), view = f.views[0];
  view.sentinel = { draft: 'unsaved', attachment: Buffer.from('file'), undo: ['before', 'after'] };
  const sender = {}, before = f.tabs.snapshot();
  const ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  assert.equal(typeof ticket.token, 'string'); assert.ok(ticket.token.length > 0);
  assert.ok(Number.isFinite(ticket.expiresAt));
  let validations = 0;
  const moving = f.tabs.transferTab(ticket.token, transferTarget(f, '2', sender, { validateSender() {
    validations++;
    assert.equal(f.tabs.getTab(a.tabId).ownerWindowId, '1');
    assert.equal(f.tabs.getTab(a.tabId).revision, a.revision);
    return true;
  } }));
  const observed = await entered.promise;
  assert.equal(observed.view, view); assert.equal(observed.target, f.w2);
  assert.equal(typeof observed.signal?.addEventListener, 'function');
  assert.equal(observed.signal.aborted, false);
  const validationsBeforeACK = validations;
  assert.equal(f.tabs.getTab(a.tabId).ownerWindowId, '1');
  assert.equal(f.tabs.getTab(a.tabId).revision, a.revision);
  assert.deepEqual(f.tabs.getWindow('1'), before.windows[0]);
  assert.deepEqual(f.tabs.getWindow('2'), before.windows[1]);
  assert.equal(f.w2.children.has(view), true); // attach alone is not a commit
  ack.resolve(true); await moving;
  assert.equal(validations, validationsBeforeACK + 1);
  assertRetained(f, a.tabId, view, '2');
  assert.deepEqual(f.tabs.getWindow('1').tabIds, []);
  assert.equal(f.tabs.getWindow('1').activeTabId, null);
  assert.deepEqual(f.tabs.getWindow('2').tabIds, [b.tabId, a.tabId]);
  assert.equal(f.tabs.getWindow('2').activeTabId, a.tabId);
  assert.ok(f.tabs.getWindow('1').revision > before.windows[0].revision);
  assert.ok(f.tabs.getWindow('2').revision > before.windows[1].revision);
  assert.ok(f.tabs.getTab(a.tabId).revision > a.revision);
  assert.ok(f.tabs.getTab(a.tabId).transferGeneration > a.transferGeneration);
  assert.equal(f.w1.children.has(view), false); assert.equal(f.views[1].visible, false);
  assert.deepEqual(view.bounds, { x: 0, y: 0, width: 900, height: 700 });
  assert.ok(view.webContents.focuses > 0); assert.ok(f.w2.focused > 0);
  assert.deepEqual(view.sentinel.undo, ['before', 'after']);
  assert.equal(f.views.length, 2); assert.equal(f.loaded.length, 2); assert.equal(f.calls.length, 2);
});

test('last tab transfers A→B→A with the same view, contents, immutable binding and no navigation', async t => {
  const f = liveFixture(t), a = await f.open('A'), view = f.views[0], contents = view.webContents;
  const project = a.project, sentinel = { draft: 'text', file: Buffer.from('bytes') };
  view.sentinel = sentinel;
  await startTransfer(f, a.tabId, '2');
  assertRetained(f, a.tabId, view, '2');
  assert.deepEqual(f.tabs.getWindow('1').tabIds, []);
  assert.equal(f.w1.isDestroyed(), false);
  const generation = f.tabs.getTab(a.tabId).transferGeneration;
  await startTransfer(f, a.tabId, '1');
  assertRetained(f, a.tabId, view, '1');
  assert.equal(view.webContents, contents); assert.equal(view.sentinel, sentinel);
  assert.equal(f.tabs.getTab(a.tabId).project, project);
  assert.ok(f.tabs.getTab(a.tabId).transferGeneration > generation);
  assert.equal(f.w1.children.has(view), true); assert.equal(f.w2.children.has(view), false);
  assert.deepEqual(view.bounds, { x: 0, y: 48, width: 900, height: 652 });
  assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1);
  assert.deepEqual(contents.loads, []);
  // Reopening still finds the unique transferred record; it does not steal it.
  assert.equal((await f.open('A', '2')).tabId, a.tabId);
  assert.equal(f.tabs.getTab(a.tabId).ownerWindowId, '1');
});

test('inactive transfer leaves the source active renderer untouched and inserts at target index', async t => {
  const f = liveFixture(t), a = await f.open('A'), b = await f.open('B');
  const c = await f.open('C', '2'), d = await f.open('D', '2');
  const sourceActive = f.views[1], focuses = sourceActive.webContents.focuses;
  assert.equal(f.views[0].visible, false);
  await startTransfer(f, a.tabId, '2', {}, { index: 1 });
  assertRetained(f, a.tabId, f.views[0], '2');
  assert.deepEqual(f.tabs.getWindow('1').tabIds, [b.tabId]);
  assert.equal(f.tabs.getWindow('1').activeTabId, b.tabId);
  assert.equal(f.w1.children.has(sourceActive), true);
  assert.equal(sourceActive.webContents.focuses, focuses);
  assert.deepEqual(f.tabs.getWindow('2').tabIds, [c.tabId, a.tabId, d.tabId]);
  assert.equal(f.tabs.getWindow('2').activeTabId, a.tabId);
  assert.equal(f.loaded.length, 4); assert.equal(f.calls.length, 4);
});

test('reorder is synchronous metadata only and rejects stale source revisions', async t => {
  const f = liveFixture(t), a = await f.open('A'), b = await f.open('B'), c = await f.open('C');
  const stale = transferSource(f, a.tabId), calls = f.nativeCalls.length;
  const result = f.tabs.reorderTab(a.tabId, { ...stale, index: 2 });
  assert.equal(typeof result?.then, 'undefined');
  assert.deepEqual(f.tabs.getWindow('1').tabIds, [b.tabId, c.tabId, a.tabId]);
  assert.equal(f.tabs.getWindow('1').activeTabId, c.tabId);
  assert.equal(f.nativeCalls.length, calls);
  assert.throws(() => f.tabs.reorderTab(a.tabId, { ...stale, index: 0 }));
  assert.deepEqual(f.tabs.getWindow('1').tabIds, [b.tabId, c.tabId, a.tabId]);
  assert.equal(f.nativeCalls.length, calls);
});

test('transfer tokens are one-use, expire on the injected clock and bind sender by identity', async t => {
  let now = 1000;
  const f = liveFixture(t, { controller: { now: () => now, transferTTL: 25 } });
  const a = await f.open('A'), sender = { id: 'same-text-is-not-same-sender' };
  let ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  assert.equal(ticket.expiresAt, 1025);
  await assert.rejects(f.tabs.transferTab(ticket.token, transferTarget(f, '2', { ...sender })));
  assertRetained(f, a.tabId, f.views[0], '1');
  ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  now = ticket.expiresAt + 1;
  await assert.rejects(f.tabs.transferTab(ticket.token, transferTarget(f, '2', sender)));
  assert.equal(f.w2.children.size, 0);
  ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  await f.tabs.transferTab(ticket.token, transferTarget(f, '2', sender));
  const committed = f.tabs.snapshot(), nativeCalls = f.nativeCalls.length;
  await assert.rejects(f.tabs.transferTab(ticket.token, transferTarget(f, '1', sender)));
  assert.deepEqual(f.tabs.snapshot(), committed); assert.equal(f.nativeCalls.length, nativeCalls);
});

test('new transfer generation supersedes an older token without moving the renderer', async t => {
  const f = liveFixture(t), a = await f.open('A'), sender = {};
  const old = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  const current = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  assert.notEqual(old.token, current.token);
  await assert.rejects(f.tabs.transferTab(old.token, transferTarget(f, '2', sender)));
  assertRetained(f, a.tabId, f.views[0], '1');
  await f.tabs.transferTab(current.token, transferTarget(f, '2', sender));
  assertRetained(f, a.tabId, f.views[0], '2');
});

test('stale source/tab/target revisions and a recycled target window fail before native mutation', async t => {
  const f = liveFixture(t), a = await f.open('A'), sender = {};
  const source = transferSource(f, a.tabId, sender);
  assert.throws(() => f.tabs.beginTransfer(a.tabId, { ...source, windowRevision: source.windowRevision - 1 }));
  assert.throws(() => f.tabs.beginTransfer(a.tabId, { ...source, tabRevision: source.tabRevision - 1 }));
  assert.throws(() => f.tabs.beginTransfer(a.tabId, { ...source, windowId: '2' }));
  let ticket = f.tabs.beginTransfer(a.tabId, source);
  const target = transferTarget(f, '2', sender);
  f.tabs.setContentInsets('2', { top: 60 });
  let nativeCalls = f.nativeCalls.length;
  await assert.rejects(f.tabs.transferTab(ticket.token, target));
  assert.equal(f.nativeCalls.length, nativeCalls);
  ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  const recycledTarget = transferTarget(f, '2', sender);
  f.w2.close(); f.tabs.registerWindow(new Window(2));
  nativeCalls = f.nativeCalls.length;
  await assert.rejects(f.tabs.transferTab(ticket.token, recycledTarget));
  assert.equal(f.nativeCalls.length, nativeCalls);
  assertRetained(f, a.tabId, f.views[0], '1');
});

test('a token minted before source reorder is stale even though the owner id is unchanged', async t => {
  const f = liveFixture(t), a = await f.open('A'); await f.open('B');
  const sender = {}, source = transferSource(f, a.tabId, sender);
  const ticket = f.tabs.beginTransfer(a.tabId, source);
  f.tabs.reorderTab(a.tabId, { ...source, index: 1 });
  const calls = f.nativeCalls.length;
  await assert.rejects(f.tabs.transferTab(ticket.token, transferTarget(f, '2', sender)));
  assert.equal(f.nativeCalls.length, calls); assertRetained(f, a.tabId, f.views[0], '1');
});

test('sender is revalidated after ACK immediately before commit and revocation rolls back', async t => {
  const ack = deferred(), f = liveFixture(t, { electron: { waitForTransferAttach: () => ack.promise } });
  const a = await f.open('A'), b = await f.open('B', '2'), before = f.tabs.snapshot();
  let validations = 0, revoked = false;
  const moving = startTransfer(f, a.tabId, '2', {}, { validateSender() {
    validations++; assert.equal(f.tabs.getTab(a.tabId).ownerWindowId, '1');
    if (revoked) throw new Error('Sender permission revoked');
    return true;
  } });
  const rejected = assert.rejects(moving, /Sender permission revoked/);
  await tick(); const beforeACK = validations; revoked = true; ack.resolve(true); await rejected;
  assert.equal(validations, beforeACK + 1); assertRetained(f, a.tabId, f.views[0], '1');
  assert.equal(f.tabs.getWindow('1').activeTabId, a.tabId);
  assert.equal(f.tabs.getWindow('2').activeTabId, b.tabId);
  assert.deepEqual(f.tabs.snapshot().windows, before.windows);
  assert.equal(f.w1.children.has(f.views[0]), true); assert.equal(f.w2.children.has(f.views[1]), true);
  assert.equal(f.w2.children.has(f.views[0]), false);
});

for (const failure of ['before-attach', 'after-attach', 'bounds', 'focus']) {
  test(`transfer ${failure} failure restores both active renderers without replacing contents`, async t => {
    const f = liveFixture(t), a = await f.open('A'), b = await f.open('B', '2');
    const view = f.views[0], before = f.tabs.snapshot();
    const attach = f.electron.attachView, bounds = f.electron.setBounds, focus = f.electron.focusView;
    let armed = true;
    f.electron.attachView = (window, candidate) => {
      if (armed && window === f.w2 && candidate === view && failure.includes('attach')) {
        armed = false;
        if (failure === 'after-attach') attach(window, candidate);
        throw new Error(`Injected ${failure}`);
      }
      return attach(window, candidate);
    };
    f.electron.setBounds = (candidate, value) => {
      if (armed && candidate === view && failure === 'bounds' && value.y === 0) {
        armed = false; throw new Error('Injected bounds');
      }
      return bounds(candidate, value);
    };
    f.electron.focusView = candidate => {
      if (armed && candidate === view && failure === 'focus') {
        armed = false; throw new Error('Injected focus');
      }
      return focus(candidate);
    };
    await assert.rejects(startTransfer(f, a.tabId, '2'), new RegExp(`Injected ${failure}`));
    assertRetained(f, a.tabId, view, '1');
    assert.equal(f.tabs.getTab(a.tabId).revision, a.revision);
    assert.deepEqual(f.tabs.snapshot().windows, before.windows);
    assert.equal(f.tabs.getWindow('2').activeTabId, b.tabId);
    assert.equal(f.w1.children.has(view), true); assert.equal(f.w2.children.has(view), false);
    assert.equal(f.w2.children.has(f.views[1]), true);
    assert.deepEqual(view.bounds, { x: 0, y: 48, width: 900, height: 652 });
    assert.equal(f.loaded.length, 2); assert.equal(f.calls.length, 2);
    await startTransfer(f, a.tabId, '2'); // rollback released the transaction lock
    assertRetained(f, a.tabId, view, '2');
  });
}

for (const result of [false, undefined, 'missing', 'reject', 'stalled']) {
  test(`transfer fails closed for ${String(result)} ACK and aborts timed-out work`, async t => {
    const ack = deferred(); let signal;
    const f = liveFixture(t, { controller: { transferTimeout: 20 }, electron: {
      waitForTransferAttach: result === 'missing' ? undefined : (_view, _window, options) => {
        signal = options.signal;
        if (result === 'reject') return Promise.reject(new Error('ACK rejected'));
        return result === 'stalled' ? ack.promise : Promise.resolve(result);
      },
    } });
    const a = await f.open('A'), before = f.tabs.snapshot();
    // Keep the process alive even if the implementation unrefs its timeout.
    const keepAlive = setTimeout(() => {}, 1000); t.after(() => clearTimeout(keepAlive));
    await assert.rejects(startTransfer(f, a.tabId, '2'));
    if (result === 'stalled') assert.equal(signal.aborted, true);
    ack.resolve(true); await tick(); // late ACK must not resurrect a failed transaction
    assertRetained(f, a.tabId, f.views[0], '1');
    assert.deepEqual(f.tabs.snapshot().windows, before.windows);
    assert.equal(f.tabs.getTab(a.tabId).revision, a.revision);
    assert.equal(f.w1.children.has(f.views[0]), true); assert.equal(f.w2.children.size, 0);
    assert.equal(f.calls.length, 1); assert.equal(f.loaded.length, 1);
  });
}

test('pending transfer locks mutating operations app-wide, including close and close barrier', async t => {
  const ack = deferred(), f = liveFixture(t, { electron: { waitForTransferAttach: () => ack.promise } });
  const a = await f.open('A'), b = await f.open('B', '2'), sender = {};
  const bSource = transferSource(f, b.tabId, sender);
  const moving = startTransfer(f, a.tabId, '2'); await tick();
  assert.throws(() => f.tabs.activateTab(b.tabId), /busy|progress|operation|transfer/i);
  assert.throws(() => f.tabs.closeTab(a.tabId), /busy|progress|operation|transfer/i);
  assert.throws(() => f.tabs.closeTab(b.tabId), /busy|progress|operation|transfer/i);
  assert.throws(() => f.tabs.reorderTab(b.tabId, { ...bSource, index: 0 }), /busy|progress|operation|transfer/i);
  assert.throws(() => f.tabs.beginTransfer(b.tabId, bSource), /busy|progress|operation|transfer/i);
  await assert.rejects(f.open('C', '2'), /busy|progress|operation|transfer/i);
  let barrierCalled = false;
  await assert.rejects(f.tabs.withCloseBarrier(async () => { barrierCalled = true; }), /busy|progress|operation|transfer/i);
  assert.equal(barrierCalled, false); assert.equal(f.calls.length, 2);
  ack.resolve(true); await moving;
  assert.equal(f.tabs.closeTab(a.tabId), true); assertRetained(f, b.tabId, f.views[1], '2');
});

test('close barrier blocks transfer/detach before native work and releases its lock after rejection', async t => {
  const barrier = deferred(), f = liveFixture(t), a = await f.open('A'), sender = {};
  const ticket = f.tabs.beginTransfer(a.tabId, transferSource(f, a.tabId, sender));
  const pending = f.tabs.withCloseBarrier(async () => barrier.promise);
  const rejection = assert.rejects(pending, /Close cancelled/); await tick();
  const calls = f.nativeCalls.length;
  await assert.rejects(f.tabs.transferTab(ticket.token, transferTarget(f, '2', sender)), /busy|progress|operation|barrier/i);
  await assert.rejects(f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, sender)), /busy|progress|operation|barrier/i);
  assert.throws(() => f.tabs.closeTab(a.tabId), /busy|progress|operation|barrier/i);
  assert.equal(f.nativeCalls.length, calls);
  barrier.reject(new Error('Close cancelled')); await rejection;
  await startTransfer(f, a.tabId, '2'); assertRetained(f, a.tabId, f.views[0], '2');
});

test('detach creates exactly one registered shell then uses the same attach ACK transaction', async t => {
  const ack = deferred(), reasons = []; let f;
  f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
    reasons.push(reason); const shell = new Window(3); f.tabs.registerWindow(shell); return shell;
  } }, electron: { waitForTransferAttach: () => ack.promise } });
  const a = await f.open('A'), before = f.tabs.getWindow('1');
  const moving = f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, {}));
  await tick(); assert.deepEqual(reasons, ['detach']);
  assertRetained(f, a.tabId, f.views[0], '1');
  assert.deepEqual(f.tabs.getWindow('1'), before);
  ack.resolve(true); await moving;
  assertRetained(f, a.tabId, f.views[0], '3');
  assert.deepEqual(f.tabs.getWindow('1').tabIds, []);
  assert.deepEqual(f.tabs.getWindow('3').tabIds, [a.tabId]);
  assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1);
});

test('detach factory failure leaves source tab live and permits retry', async t => {
  let fail = true, f; const reasons = [];
  f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
    reasons.push(reason); if (fail) throw new Error('Shell factory failed');
    const shell = new Window(3); f.tabs.registerWindow(shell); return shell;
  } } });
  const a = await f.open('A'), before = f.tabs.snapshot();
  await assert.rejects(f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, {})), /Shell factory failed/);
  assertRetained(f, a.tabId, f.views[0], '1'); assert.deepEqual(f.tabs.snapshot().windows, before.windows);
  fail = false; await f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, {}));
  assert.deepEqual(reasons, ['detach', 'detach']); assertRetained(f, a.tabId, f.views[0], '3');
});

test('target physical destruction aborts pending ACK and rolls back to the live source', async t => {
  const ack = deferred(); let signal;
  const f = liveFixture(t, { electron: { waitForTransferAttach: (_view, _target, options) => {
    signal = options.signal; return ack.promise;
  } } });
  const a = await f.open('A'), source = f.tabs.getWindow('1'), moving = startTransfer(f, a.tabId, '2');
  const rejected = assert.rejects(moving); await tick(); f.w2.close(); await rejected;
  assert.equal(signal.aborted, true); assert.equal(f.tabs.getWindow('2'), null);
  assertRetained(f, a.tabId, f.views[0], '1'); assert.deepEqual(f.tabs.getWindow('1'), source);
  assert.equal(f.w1.children.has(f.views[0]), true);
  ack.resolve(true); await tick(); assertRetained(f, a.tabId, f.views[0], '1');
});

for (const death of ['source', 'both']) {
  test(`${death} physical destruction preserves the transferring view in one recovery shell`, async t => {
    const ack = deferred(), reasons = []; let f, attempts = 0, signal;
    f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
      reasons.push(reason); const shell = new Window(3); f.tabs.registerWindow(shell); return shell;
    } }, electron: { waitForTransferAttach: (_view, _target, options) => {
      if (++attempts === 1) { signal = options.signal; return ack.promise; }
      return Promise.resolve(true);
    } } });
    const a = await f.open('A'), moving = startTransfer(f, a.tabId, '2');
    const rejected = assert.rejects(moving); await tick();
    f.w1.close(); if (death === 'both') f.w2.close();
    await rejected;
    assert.equal(signal.aborted, true); assert.deepEqual(reasons, ['recovery']);
    assertRetained(f, a.tabId, f.views[0], '3');
    assert.equal(f.tabs.getTab(a.tabId).state, 'ready');
    assert.deepEqual(f.tabs.getWindow('3').tabIds, [a.tabId]);
    assert.equal(f.tabs.getWindow('1'), null); assert.equal(f.views.length, 1);
    ack.resolve(true); await tick(); assertRetained(f, a.tabId, f.views[0], '3');
    assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1);
  });
}

for (const failure of ['absent-factory', 'factory', 'recovery-ack']) {
  test(`${failure} recovery failure retains an ownerless view and recoverTab retries without reload`, async t => {
    const ack = deferred(); let f, attempts = 0, retryAck = deferred();
    const controller = failure === 'absent-factory' ? {} : { createShell: async () => {
      if (failure === 'factory') throw new Error('Recovery shell failed');
      const shell = new Window(3); f.tabs.registerWindow(shell); return shell;
    } };
    f = liveFixture(t, { controller, electron: { waitForTransferAttach: () => {
      attempts++;
      if (attempts === 1) return ack.promise;
      if (failure === 'recovery-ack' && attempts === 2) return Promise.resolve(false);
      return retryAck.promise;
    } } });
    const a = await f.open('A'), view = f.views[0], moving = startTransfer(f, a.tabId, '2');
    const rejected = assert.rejects(moving); await tick(); f.w1.close(); await rejected;
    assertRetained(f, a.tabId, view, null); assert.equal(f.tabs.getTab(a.tabId).state, 'recovering');
    assert.equal(f.tabs.getWindow('2').tabIds.includes(a.tabId), false);
    await assert.rejects(f.tabs.recoverTab(a.tabId, { windowId: '2', windowRevision: -1 }));
    const recovering = f.tabs.recoverTab(a.tabId, { windowId: '2', windowRevision: f.tabs.getWindow('2').revision });
    await tick(); assertRetained(f, a.tabId, view, null);
    retryAck.resolve(true); await recovering;
    assertRetained(f, a.tabId, view, '2'); assert.equal(f.tabs.getTab(a.tabId).state, 'ready');
    assert.equal(f.w2.children.has(view), true);
    ack.resolve(true); await tick(); assertRetained(f, a.tabId, view, '2');
    assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1);
  });
}

for (const death of ['source', 'target']) {
  test(`${death} synchronous loss inside attach ACK observes the returned rejected promise`, async t => {
    const unhandled = [], reasons = []; let f, first = true;
    const onUnhandled = error => unhandled.push(error);
    process.on('unhandledRejection', onUnhandled);
    t.after(() => process.removeListener('unhandledRejection', onUnhandled));
    f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
      reasons.push(reason); const shell = new Window(3); f.tabs.registerWindow(shell); return shell;
    } }, electron: { waitForTransferAttach: () => {
      if (!first) return Promise.resolve(true);
      first = false;
      (death === 'source' ? f.w1 : f.w2).close();
      // This promise is produced after the operation's signal already aborted.
      // #wait must still observe it instead of returning early and leaking it.
      return Promise.reject(new Error('ACK rejection after synchronous window loss'));
    } } });
    const a = await f.open('A'), view = f.views[0];
    await assert.rejects(startTransfer(f, a.tabId, '2'), /window|lost/i);
    await tick(); await tick();
    assert.deepEqual(unhandled, []);
    assertRetained(f, a.tabId, view, death === 'source' ? '3' : '1');
    assert.deepEqual(reasons, death === 'source' ? ['recovery'] : []);
    assert.equal(f.views.length, 1); assert.equal(f.calls.length, 1); assert.equal(f.loaded.length, 1);
  });
}

test('source loss while detach factory is pending recovers once and closes the late empty shell', async t => {
  const lateFactory = deferred(), factoryEntered = deferred(), reasons = [], shells = [];
  let f;
  f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
    reasons.push(reason);
    if (reason === 'detach') { factoryEntered.resolve(); await lateFactory.promise; }
    const shell = new Window(reason === 'detach' ? 3 : 4);
    shells.push(shell); f.tabs.registerWindow(shell); return shell;
  } } });
  const a = await f.open('A'), view = f.views[0];
  const moving = f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, {}));
  const rejected = assert.rejects(moving, /window|lost/i);
  await factoryEntered.promise; f.w1.close(); await rejected;
  assert.deepEqual(reasons, ['detach', 'recovery']);
  assertRetained(f, a.tabId, view, '4');
  assert.equal(f.tabs.getWindow('3'), null);
  lateFactory.resolve(); await tick(); await tick();
  const abandoned = shells.find(shell => shell.id === 3), recovery = shells.find(shell => shell.id === 4);
  assert.equal(abandoned.isDestroyed(), true); assert.equal(abandoned.children.size, 0);
  assert.equal(f.tabs.getWindow('3'), null);
  assert.equal(recovery.isDestroyed(), false); assert.equal(recovery.children.has(view), true);
  assertRetained(f, a.tabId, view, '4');
  assert.deepEqual(reasons, ['detach', 'recovery']);
  assert.deepEqual(f.tabs.getWindow('4').tabIds, [a.tabId]);
  assert.equal(f.w2.children.has(view), false);
  assert.equal(f.views.length, 1); assert.equal(f.loaded.length, 1); assert.equal(f.calls.length, 1);
});

for (const event of ['render-process-gone', 'destroyed']) {
  test(`${event} during pending ACK aborts promptly instead of waiting for timeout`, async t => {
    const ack = deferred(), entered = deferred(); let signal;
    const f = liveFixture(t, { controller: { transferTimeout: 5000 }, electron: {
      waitForTransferAttach: (_view, _target, options) => {
        signal = options.signal; entered.resolve(); return ack.promise;
      },
    } });
    const a = await f.open('A'), b = await f.open('B'); f.tabs.activateTab(a.tabId);
    const c = await f.open('C', '2'), view = f.views[0];
    const sourceRevision = f.tabs.getWindow('1').revision;
    const moving = startTransfer(f, a.tabId, '2');
    const rejected = assert.rejects(moving, /crash|destroy/i);
    await entered.promise;
    if (event === 'destroyed') view.webContents.close();
    else view.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
    let timer;
    try {
      await Promise.race([rejected, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Renderer loss did not abort promptly (500ms)')), 500);
      })]);
    } finally { clearTimeout(timer); }
    assert.equal(signal.aborted, true); assert.doesNotMatch(signal.reason.message, /timeout/i);
    assert.equal(f.tabs.getWindow('2').activeTabId, c.tabId);
    assert.equal(f.w2.children.has(f.views[2]), true); assert.equal(f.w2.children.has(view), false);
    if (event === 'destroyed') {
      assert.equal(f.tabs.getTab(a.tabId), null);
      assert.deepEqual(f.tabs.getWindow('1').tabIds, [b.tabId]);
      assert.equal(f.tabs.getWindow('1').activeTabId, b.tabId);
      assert.equal(f.w1.children.has(view), false); assert.equal(f.w1.children.has(f.views[1]), true);
    } else {
      assertRetained(f, a.tabId, view, '1');
      assert.equal(f.tabs.getTab(a.tabId).state, 'crashed');
      assert.ok(f.tabs.getTab(a.tabId).revision > a.revision);
      assert.equal(f.w1.children.has(view), true);
    }
    assert.ok(f.tabs.getWindow('1').revision >= sourceRevision);
    const afterAbort = f.tabs.snapshot(); ack.resolve(true); await tick();
    assert.deepEqual(f.tabs.snapshot(), afterAbort);
    assert.equal(f.views.length, 3); assert.equal(f.loaded.length, 3); assert.equal(f.calls.length, 3);
    // A fast abort must also release the app-wide transfer/close lock.
    assert.equal(f.tabs.closeTab(c.tabId), true);
  });
}

test('source inset change during pending ACK rejects stale commit but preserves the newer window revision', async t => {
  const ack = deferred(), entered = deferred();
  const f = liveFixture(t, { electron: { waitForTransferAttach: () => { entered.resolve(); return ack.promise; } } });
  const a = await f.open('A'), b = await f.open('B', '2'), view = f.views[0];
  const sourceBefore = f.tabs.getWindow('1'), targetBefore = f.tabs.getWindow('2');
  const moving = startTransfer(f, a.tabId, '2'), rejected = assert.rejects(moving, /stale/i);
  await entered.promise;
  const updated = f.tabs.setContentInsets('1', { top: 73, left: 4, right: 6, bottom: 9 });
  assert.ok(updated.revision > sourceBefore.revision);
  ack.resolve(true); await rejected;
  assertRetained(f, a.tabId, view, '1');
  assert.deepEqual(f.tabs.getWindow('1'), updated);
  assert.deepEqual(f.tabs.getWindow('2'), targetBefore);
  assert.equal(f.tabs.getTab(a.tabId).revision, a.revision);
  assert.deepEqual(view.bounds, { x: 4, y: 73, width: 890, height: 618 });
  assert.equal(f.w1.children.has(view), true); assert.equal(f.w2.children.has(view), false);
  assert.equal(f.tabs.getWindow('2').activeTabId, b.tabId);
  assert.equal(f.loaded.length, 2); assert.equal(f.calls.length, 2);
});

for (const rollbackFailure of ['attach', 'focus']) {
  test(`target focus failure plus rollback source ${rollbackFailure} failure recovers without orphan attachment`, async t => {
    const reasons = []; let f, recovery;
    f = liveFixture(t, { controller: { createShell: async ({ reason }) => {
      reasons.push(reason); recovery = new Window(3); f.tabs.registerWindow(recovery); return recovery;
    } } });
    const a = await f.open('A'), b = await f.open('B'); f.tabs.activateTab(a.tabId);
    const c = await f.open('C', '2'), view = f.views[0];
    const attach = f.electron.attachView, focus = f.electron.focusView;
    let targetFocusFailed = false, rollbackFailed = false;
    f.electron.attachView = (window, candidate) => {
      attach(window, candidate);
      if (candidate === view && window === f.w1 && targetFocusFailed && rollbackFailure === 'attach') {
        rollbackFailed = true;
        // Native membership already changed: recovery must remove this orphan.
        throw new Error('Rollback source attach failed after native add');
      }
    };
    f.electron.focusView = candidate => {
      if (candidate === view && f.w2.children.has(view)) {
        targetFocusFailed = true; throw new Error('Target focus failed');
      }
      if (candidate === view && f.w1.children.has(view) && targetFocusFailed && rollbackFailure === 'focus') {
        rollbackFailed = true; throw new Error('Rollback source focus failed');
      }
      return focus(candidate);
    };
    await assert.rejects(startTransfer(f, a.tabId, '2'), /Target focus failed/);
    assert.equal(targetFocusFailed, true); assert.equal(rollbackFailed, true);
    assert.deepEqual(reasons, ['recovery']); assertRetained(f, a.tabId, view, '3');
    assert.equal(f.tabs.getTab(a.tabId).state, 'ready');
    assert.equal(f.w1.children.has(view), false); assert.equal(f.w2.children.has(view), false);
    assert.equal(recovery.children.has(view), true); assert.equal(recovery.children.size, 1);
    assert.deepEqual(f.tabs.getWindow('1').tabIds, [b.tabId]);
    assert.equal(f.tabs.getWindow('1').activeTabId, b.tabId); assert.equal(f.w1.children.has(f.views[1]), true);
    assert.deepEqual(f.tabs.getWindow('2').tabIds, [c.tabId]);
    assert.equal(f.tabs.getWindow('2').activeTabId, c.tabId); assert.equal(f.w2.children.has(f.views[2]), true);
    assert.deepEqual(f.tabs.getWindow('3').tabIds, [a.tabId]);
    assert.equal(f.views.length, 3); assert.equal(f.loaded.length, 3); assert.equal(f.calls.length, 3);
  });
}

// VS-60 / dev spec §5.3.6: only settled registry topology may reach a manifest.
test('committed snapshot excludes pending ensure/load and publishes only completed opens', async t => {
  const ensure = deferred(), load = deferred(), commits = [];
  const f = liveFixture(t, { ensure: () => ensure.promise, load: () => load.promise,
    controller: { onCommit: snapshot => commits.push(snapshot) } });
  const empty = f.tabs.committedSnapshot(), opening = f.open('A');
  await tick(); assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  assert.equal(f.tabs.committedSnapshot({ closeBarrier: true }), null, 'Quit cannot snapshot a pending open');
  ensure.resolve(binding()); await tick();
  assert.equal(f.tabs.snapshot().tabs[0].state, 'loading');
  assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  load.resolve(); const tab = await opening; await tick();
  assert.equal(commits.length, 1); assert.deepEqual(commits[0], f.tabs.snapshot());
  assert.equal(commits[0].tabs[0].tabId, tab.tabId);
  assert.equal(empty.tabs.length, 0); // snapshots remain detached from later edits
  assert.ok(Object.isFrozen(commits[0].windows[0].tabIds));
});

test('activation, reorder and transfer notify after commit, never during native preparation', async t => {
  const commits = [], ack = deferred(); let f;
  f = liveFixture(t, { controller: { onCommit: snapshot => {
    assert.deepEqual(snapshot, f.tabs.committedSnapshot()); commits.push(snapshot);
  } }, electron: { waitForTransferAttach: () => ack.promise } });
  const a = await f.open('A'), b = await f.open('B'); await tick(); commits.length = 0;
  f.tabs.activateTab(a.tabId); await tick();
  assert.equal(commits.length, 1); assert.equal(commits[0].windows[0].activeTabId, a.tabId);
  f.tabs.reorderTab(b.tabId, { ...transferSource(f, b.tabId, {}), index: 0 }); await tick();
  assert.equal(commits.length, 2); assert.deepEqual(commits[1].windows[0].tabIds, [b.tabId, a.tabId]);
  commits.length = 0;
  const moving = startTransfer(f, a.tabId, '2', {}, { beforeCommit: () => {
    assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  } });
  await tick(); assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  ack.resolve(true); await moving; await tick();
  assert.equal(commits.length, 1); assert.equal(commits[0].tabs.find(tab => tab.tabId === a.tabId).ownerWindowId, '2');
});

test('transfer focus rollback and cancelled reorder emit no tentative commit', async t => {
  const commits = [], f = liveFixture(t, { controller: { onCommit: snapshot => commits.push(snapshot) } });
  const a = await f.open('A'); await f.open('B'); await tick(); commits.length = 0;
  const before = f.tabs.committedSnapshot(), focus = f.electron.focusView;
  f.electron.focusView = view => {
    if (f.w2.children.has(view)) {
      assert.equal(f.tabs.committedSnapshot(), null); throw new Error('Focus failed');
    }
    focus(view);
  };
  await assert.rejects(startTransfer(f, a.tabId, '2'), /Focus failed/); await tick();
  assert.deepEqual(f.tabs.committedSnapshot(), before); assert.deepEqual(commits, []);
  await assert.rejects(f.tabs.reorderTab(a.tabId, { ...transferSource(f, a.tabId, {}), index: 1,
    beforeCommit: () => Promise.reject(new Error('Cancelled')) }), /Cancelled/);
  await tick(); assert.deepEqual(commits, []); assert.deepEqual(f.tabs.committedSnapshot(), before);
});

test('cancelled detach never publishes a temporary shell, even while native close is delayed', async t => {
  const commits = [], closing = []; let f, shell;
  f = liveFixture(t, { controller: { onCommit: snapshot => commits.push(snapshot),
    onWindowEmpty: id => closing.push(id), createShell: async () => {
      shell = new Window(3); f.tabs.registerWindow(shell); return shell;
    } } });
  const a = await f.open('A'); await tick(); commits.length = 0;
  const before = f.tabs.committedSnapshot();
  await assert.rejects(f.tabs.detachTab(a.tabId, { ...transferSource(f, a.tabId, {}),
    beforeCommit: () => Promise.reject(new Error('Cancelled')) }), /Cancelled/);
  await tick(); assert.deepEqual(commits, []); assert.deepEqual(closing, ['3']);
  assert.equal(f.tabs.committedSnapshot(), null); // hidden shell has not physically closed yet
  assert.equal(f.tabs.committedSnapshot({ closeBarrier: true }), null, 'Quit cannot persist a cancelled shell');
  shell.close(); await tick();
  assert.deepEqual(commits, []); assert.deepEqual(f.tabs.committedSnapshot(), before);
});

test('close barrier, direct close and window unregister commit without resurrecting closed tabs; dispose is silent', async t => {
  const commits = [], barrier = deferred();
  const f = liveFixture(t, { controller: { onCommit: snapshot => commits.push(snapshot) } });
  const a = await f.open('A'), b = await f.open('B'), c = await f.open('C', '2');
  await tick(); commits.length = 0;
  const closing = f.tabs.withCloseBarrier(async ({ closeTab }) => { closeTab(a.tabId); await barrier.promise; });
  await tick(); assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  assert.equal(f.tabs.committedSnapshot({ closeBarrier: true }).tabs.some(tab => tab.tabId === a.tabId), false);
  barrier.resolve(); await closing; await tick();
  assert.equal(commits.length, 1); assert.equal(commits[0].tabs.some(tab => tab.tabId === a.tabId), false);
  f.tabs.closeTab(b.tabId); await tick();
  assert.equal(commits.length, 2); assert.deepEqual(commits[1].tabs.map(tab => tab.tabId), [c.tabId]);
  f.tabs.unregisterWindow('2'); await tick();
  assert.equal(commits.length, 3); assert.deepEqual(commits[2].tabs, []);
  // Suppress both a queued commit and teardown's unregister notifications.
  f.tabs.unregisterWindow('1'); f.tabs.dispose(); await tick();
  assert.equal(commits.length, 3); assert.equal(f.tabs.committedSnapshot(), null);
});

test('source loss recovery publishes only the settled recovered owner, including late factory cleanup', async t => {
  const commits = [], late = deferred(), entered = deferred(); let f;
  f = liveFixture(t, { controller: { onCommit: snapshot => commits.push(snapshot),
    createShell: async ({ reason }) => {
      if (reason === 'detach') { entered.resolve(); await late.promise; }
      const shell = new Window(reason === 'detach' ? 3 : 4); f.tabs.registerWindow(shell); return shell;
    } } });
  const a = await f.open('A'); await tick(); commits.length = 0;
  const moving = f.tabs.detachTab(a.tabId, transferSource(f, a.tabId, {})), rejected = assert.rejects(moving);
  await entered.promise; f.w1.close(); await rejected; await tick();
  assert.equal(f.tabs.getTab(a.tabId).ownerWindowId, '4');
  assert.equal(f.tabs.committedSnapshot(), null); assert.deepEqual(commits, []);
  late.resolve(); await tick(); await tick();
  assert.equal(commits.length, 1); assert.equal(commits[0].tabs[0].ownerWindowId, '4');
  assert.deepEqual(commits[0].windows.map(window => window.windowId), ['2', '4']);
});

test('synchronous activation focus cannot expose a partial commit and a failed initial focus stays uncommitted', async t => {
  const commits = []; let f, fail = true;
  f = liveFixture(t, { controller: { onCommit: snapshot => commits.push(snapshot) }, electron: {
    focusView: () => {
      assert.equal(f.tabs.committedSnapshot(), null);
      if (fail) throw new Error('Initial focus failed');
    },
  } });
  await assert.rejects(f.open('A'), /Initial focus failed/); await tick();
  assert.deepEqual(commits, []); assert.equal(f.tabs.committedSnapshot(), null);
  fail = false;
  f.tabs.activateTab(f.tabs.snapshot().tabs[0].tabId); await tick();
  assert.equal(commits.length, 1); assert.deepEqual(commits[0], f.tabs.snapshot());
});
