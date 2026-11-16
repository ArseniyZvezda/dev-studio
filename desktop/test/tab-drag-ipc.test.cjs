'use strict';
// VS-52 / dev spec §5.3.4: mocked IPC protocol proof, not native OS validation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { registerTabsIpc, TABS_CHANNELS: C } = require('../tabs-ipc.cjs');
const SHELL = pathToFileURL(path.join(__dirname, '..', 'shell.html')).href;

class Contents extends EventEmitter {
  constructor(url = SHELL) { super(); this.mainFrame = { url }; this.destroyed = false; }
  getURL() { return this.mainFrame.url; }
  isDestroyed() { return this.destroyed; }
  send() {}
}
class Window extends EventEmitter {
  constructor(id) { super(); this.id = id; this.webContents = new Contents(); }
  isDestroyed() { return false; }
}
const eventFor = sender => ({ sender, senderFrame: sender.mainFrame });
function fixture(t, { unavailable = false } = {}) {
  const handlers = new Map(), calls = [];
  const windows = new Map(['1', '2'].map(windowId => [windowId, {
    windowId, revision: 4, tabIds: [`tab-${windowId}`], activeTabId: `tab-${windowId}`,
    insets: { top: 44, right: 0, bottom: 0, left: 0 },
  }]));
  const tabs = new Map(['1', '2'].map(id => [`tab-${id}`, {
    tabId: `tab-${id}`, ownerWindowId: id, revision: 3, state: 'ready', transferGeneration: 0,
    project: { root: `/project/${id}`, project_id: `project-${id}`, epoch: 'epoch-1' },
  }]));
  const registry = { getWindow: id => windows.get(id), getTab: id => tabs.get(id),
    snapshot: () => ({ windows: [...windows.values()], tabs: [...tabs.values()] }) };
  const tabDrag = Object.fromEntries(['begin', 'cancel', 'setGeometry'].map(method => [method,
    (event, request) => { calls.push({ method, event, request }); return `${method}-result`; }]));
  const controller = registerTabsIpc({ registry,
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: channel => handlers.delete(channel) },
    ...(!unavailable ? { tabDrag } : {}),
  });
  t.after(() => controller.dispose());
  const w1 = new Window(1), w2 = new Window(2);
  const release = controller.registerWindow(w1); controller.registerWindow(w2);
  const project = new Contents('http://127.0.0.1:8765/p/project-1/');
  controller.registerProjectView({ webContents: project }, tabs.get('tab-1').project);
  const begin = () => ({ windowId: '1', windowRevision: windows.get('1').revision,
    tabId: 'tab-1', tabRevision: tabs.get('tab-1').revision, point: { x: 40, y: 20 } });
  const geometry = () => ({ windowId: '1', windowRevision: windows.get('1').revision,
    tabs: [{ tabId: 'tab-1', x: 4, y: 0, width: 120, height: 44 }] });
  const cancel = () => ({ windowId: '1', windowRevision: windows.get('1').revision, token: 'opaque-token' });
  const invoke = (channel, event, ...args) => handlers.get(channel)(event, ...args);
  return { controller, handlers, calls, registry, windows, tabs, w1, w2, project, release,
    tabDrag, begin, geometry, cancel, invoke, event: eventFor(w1.webContents) };
}

test('native drag channels forward validated requests and the actual captured sender/frame', t => {
  const f = fixture(t);
  assert.equal(C.dragBegin, 'studio:tabs-drag-begin');
  assert.equal(C.dragCancel, 'studio:tabs-drag-cancel');
  assert.equal(C.dragGeometry, 'studio:tabs-drag-geometry');
  for (const [channel, method, request] of [[C.dragBegin, 'begin', f.begin()],
    [C.dragCancel, 'cancel', f.cancel()], [C.dragGeometry, 'setGeometry', f.geometry()]]) {
    assert.equal(f.invoke(channel, f.event, request), `${method}-result`);
    const call = f.calls.at(-1);
    assert.equal(call.method, method); assert.equal(call.event, f.event);
    assert.equal(call.event.sender, f.w1.webContents);
    assert.equal(call.event.senderFrame, f.w1.webContents.mainFrame);
    assert.equal(call.request, request);
  }
  const authorized = f.controller.authorizeDrag(f.event, f.begin());
  assert.equal(authorized.role, 'shell'); assert.equal(authorized.window, f.w1);
  assert.equal(authorized.contents, f.w1.webContents);
});

test('all native drag channels reject project, subframe, unregistered and navigated senders', t => {
  const f = fixture(t);
  const badEvents = [eventFor(f.project), eventFor(new Contents()),
    { sender: f.w1.webContents, senderFrame: { url: SHELL } },
    { sender: f.w1.webContents }, {}, null];
  for (const channel of [C.dragBegin, C.dragCancel, C.dragGeometry]) {
    const request = channel === C.dragBegin ? f.begin() : channel === C.dragCancel ? f.cancel() : f.geometry();
    for (const event of badEvents) assert.throws(() => f.invoke(channel, event, request));
    for (const url of ['https://evil.example/', 'http://127.0.0.1:8765/', `${SHELL}?role=shell`]) {
      f.w1.webContents.mainFrame.url = url;
      assert.throws(() => f.invoke(channel, f.event, request));
    }
    f.w1.webContents.mainFrame.url = SHELL;
    f.event.senderFrame.isDestroyed = () => true;
    assert.throws(() => f.invoke(channel, f.event, request));
    delete f.event.senderFrame.isDestroyed;
  }
  assert.deepEqual(f.calls, []);
});

test('geometry forwards the explicit append boundary and rejects malformed boundaries', t => {
  const f = fixture(t), request = { ...f.geometry(), appendStart: 192, appendEnd: 600 };
  assert.equal(f.invoke(C.dragGeometry, f.event, request), 'setGeometry-result');
  assert.equal(f.calls.at(-1).request.appendStart, 192);
  for (const appendEnd of [191, 192, Infinity, NaN, null, undefined, 1e8]) {
    assert.throws(() => f.invoke(C.dragGeometry, f.event, { ...request, appendEnd }));
  }
  for (const appendStart of [-1, Infinity, NaN, '192', null, 1e8, undefined]) {
    assert.throws(() => f.invoke(C.dragGeometry, f.event, { ...f.geometry(), appendStart }));
  }
  assert.equal(f.calls.length, 1);
});

test('begin and geometry require current own window; begin also requires current own tab', t => {
  const f = fixture(t);
  for (const channel of [C.dragBegin, C.dragGeometry]) {
    const good = channel === C.dragBegin ? f.begin() : f.geometry();
    for (const bad of [{ ...good, windowId: '2' }, { ...good, windowRevision: 0 },
      { ...good, windowRevision: '4' }, { ...good, windowRevision: -1 }]) {
      assert.throws(() => f.invoke(channel, f.event, bad));
    }
  }
  for (const bad of [{ ...f.begin(), tabId: 'tab-2' }, { ...f.begin(), tabId: 'missing' },
    { ...f.begin(), tabRevision: 2 }, { ...f.begin(), tabRevision: '3' }]) {
    assert.throws(() => f.invoke(C.dragBegin, f.event, bad));
  }
  const original = f.begin();
  f.tabs.get('tab-1').ownerWindowId = '2';
  assert.throws(() => f.controller.authorizeDrag(f.event, original));
  assert.deepEqual(f.calls, []);
});

test('malformed and renderer terminal/success claims fail before any native callback', t => {
  const f = fixture(t);
  for (const [channel, make] of [[C.dragBegin, f.begin], [C.dragCancel, f.cancel], [C.dragGeometry, f.geometry]]) {
    const good = make();
    for (const bad of [null, [], {}, Object.create(good), { ...good, terminal: 'released' },
      { ...good, success: true }, { ...good, dropEffect: 'none' }, { ...good, dragend: true },
      { ...good, sender: f.w2.webContents }]) assert.throws(() => f.invoke(channel, f.event, bad));
    assert.throws(() => f.invoke(channel, f.event, good, 'extra'));
  }
  for (const point of [null, [], {}, { x: NaN, y: 0 }, { x: 0, y: Infinity },
    { x: '4', y: 0 }, { x: 4, y: 0, success: true }]) {
    assert.throws(() => f.invoke(C.dragBegin, f.event, { ...f.begin(), point }));
  }
  for (const token of ['', 'x'.repeat(129), null, 1]) {
    assert.throws(() => f.invoke(C.dragCancel, f.event, { ...f.cancel(), token }));
  }
  for (const windowRevision of [-1, NaN, Infinity, '4']) {
    assert.throws(() => f.invoke(C.dragCancel, f.event, { ...f.cancel(), windowRevision }));
  }
  const rect = f.geometry().tabs[0];
  for (const tabs of [null, {}, Array(513).fill(rect), [null], [{ ...rect, width: 0 }],
    [{ ...rect, height: -1 }], [{ ...rect, x: Infinity }], [{ ...rect, y: NaN }],
    [{ ...rect, tabId: '' }], [{ ...rect, targetWindowId: '2' }]]) {
    assert.throws(() => f.invoke(C.dragGeometry, f.event, { ...f.geometry(), tabs }));
  }
  assert.deepEqual(f.calls, []);
});

test('late cancellation ignores stale revision but cannot impersonate another source shell', t => {
  const f = fixture(t), request = f.cancel();
  const source = f.controller.authorizeDrag(f.event, request);
  f.windows.get('1').revision++;
  assert.equal(f.controller.authorizeDrag(f.event, request).contents, source.contents);
  assert.equal(f.invoke(C.dragCancel, f.event, request), 'cancel-result');
  assert.throws(() => f.invoke(C.dragCancel, eventFor(f.w2.webContents), request));
  // Opaque-token ownership remains the coordinator's responsibility. The IPC
  // boundary must pass the real caller, not a renderer-supplied source identity.
  f.tabDrag.cancel = (event, payload) => event.sender === source.contents && payload.token === request.token;
  assert.equal(f.invoke(C.dragCancel, eventFor(f.w2.webContents), { ...request, windowId: '2' }), false);
  f.w1.webContents.mainFrame = { url: SHELL };
  assert.throws(() => f.invoke(C.dragCancel, f.event, request), /Untrusted/);
  assert.throws(() => f.controller.authorizeDrag(f.event, request), /Untrusted/);
});

test('captured source authorization fails after revision, ownership, navigation, destruction or release', t => {
  for (const change of [f => f.windows.get('1').revision++, f => f.tabs.get('tab-1').revision++,
    f => { f.tabs.get('tab-1').ownerWindowId = '2'; },
    f => { f.w1.webContents.mainFrame = { url: SHELL }; },
    f => { f.w1.webContents.destroyed = true; }, f => f.release()]) {
    const f = fixture(t), request = f.begin();
    assert.equal(f.controller.authorizeDrag(f.event, request).contents, f.w1.webContents);
    change(f);
    assert.throws(() => f.controller.authorizeDrag(f.event, request));
  }
});

test('missing native coordinator fails closed; disposal removes all native drag handlers', t => {
  const f = fixture(t, { unavailable: true });
  assert.throws(() => f.invoke(C.dragBegin, f.event, f.begin()), /unavailable/);
  assert.throws(() => f.invoke(C.dragGeometry, f.event, f.geometry()), /unavailable/);
  assert.equal(f.invoke(C.dragCancel, f.event, f.cancel()), undefined);
  f.controller.dispose();
  for (const channel of [C.dragBegin, C.dragCancel, C.dragGeometry]) assert.equal(f.handlers.has(channel), false);
  assert.throws(() => f.controller.authorizeDrag(f.event, f.begin()));
});

function preload(f) {
  const exposed = {}, invoked = [], listeners = new EventEmitter();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.cjs'), 'utf8'), {
    require(name) {
      assert.equal(name, 'electron');
      return { contextBridge: { exposeInMainWorld: (name, value) => { exposed[name] = value; } },
        ipcRenderer: {
          invoke: (channel, ...args) => {
            invoked.push([channel, ...args]);
            return Promise.resolve().then(() => f.invoke(channel, f.event, ...args));
          },
          on: (channel, callback) => listeners.on(channel, callback),
          removeListener: (channel, callback) => listeners.removeListener(channel, callback),
        } };
    },
  });
  return { exposed, invoked, listeners };
}

test('actual VM preload routes only fixed native drag operations through actual registered IPC', async t => {
  const f = fixture(t), { exposed, invoked, listeners } = preload(f), bridge = exposed.voxariumTabs;
  assert.ok(Object.isFrozen(bridge));
  const expected = [['beginDrag', C.dragBegin, f.begin(), 'begin-result'],
    ['cancelDrag', C.dragCancel, f.cancel(), 'cancel-result'],
    ['setDragGeometry', C.dragGeometry, f.geometry(), 'setGeometry-result']];
  for (const [method, channel, request, result] of expected) {
    assert.equal(await bridge[method](request, 'studio:arbitrary'), result);
    assert.deepEqual(invoked.at(-1), [channel, request]);
    assert.equal(f.calls.at(-1).event.sender, f.w1.webContents);
  }
  for (const method of ['send', 'invoke', 'on', 'require', 'transferTab', 'beginTransfer',
    'finishDrag', 'completeDrag', 'dragEnd', 'dragend', 'drop', 'terminal', 'nativeTerminal']) {
    assert.equal(bridge[method], undefined, `${method} must not be a renderer capability`);
  }
  assert.deepEqual(Object.keys(bridge).filter(name => /drag|terminal|transfer|drop/i.test(name)).sort(),
    ['beginDrag', 'cancelDrag', 'setDragGeometry']);
  assert.deepEqual(Object.keys(exposed).sort(), ['voxariumNotifications', 'voxariumStartup', 'voxariumTabs', 'voxariumUpdates', 'voxariumWindow']);
  assert.equal(typeof exposed.voxariumWindow.close, 'function');
  assert.equal(typeof exposed.voxariumUpdates.install, 'function');
  assert.equal(typeof exposed.voxariumStartup.setEnabled, 'function');
  const received = [], state = { role: 'shell' };
  const release = bridge.onStateChange((...args) => received.push(args));
  listeners.emit(C.stateChanged, { sender: 'secret', reply() {} }, state);
  assert.deepEqual(received, [[state]]); release();
  assert.equal(listeners.listenerCount(C.stateChanged), 0);
  await assert.rejects(bridge.beginDrag({ ...f.begin(), success: true }));
  f.w1.webContents.mainFrame.url = 'https://evil.example/';
  await assert.rejects(bridge.beginDrag(f.begin()), /Untrusted/);
});
