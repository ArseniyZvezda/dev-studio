'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { DesktopTabs } = require('../tabs.cjs');
const { createTabDrag } = require('../tab-drag.cjs');
const { NativeTerminal } = require('../native-terminal.cjs');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

class Contents extends EventEmitter {
  zoom = 1; destroyed = false; mainFrame = {}; focuses = 0;
  getZoomFactor() { return this.zoom; }
  isDestroyed() { return this.destroyed; }
  close() { this.destroyed = true; this.emit('destroyed'); }
  focus() { this.focuses++; }
}
class Window extends EventEmitter {
  webContents = new Contents(); visible = true; destroyed = false; minimized = false; focused = true;
  bounds = { x: 0, y: 0, width: 900, height: 700 }; children = new Set();
  constructor(id, x = 0) { super(); this.id = id; this.bounds.x = x; }
  getBounds() { return { ...this.bounds }; }
  getContentBounds() { return { ...this.bounds }; }
  getContentSize() { return [this.bounds.width, this.bounds.height]; }
  setBounds(bounds) { this.bounds = { ...bounds }; this.emit('resize'); }
  getNativeWindowHandle() { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(this.id)); return b; }
  isVisible() { return this.visible; }
  isFocused() { return this.focused; }
  isMinimized() { return this.minimized; }
  isDestroyed() { return this.destroyed; }
  restore() { this.minimized = false; }
  show() { this.visible = true; }
  focus() { this.focused = true; }
  close() { this.destroyed = true; this.emit('closed'); }
}
async function fixture() {
  const windows = [new Window(1), new Window(2, 1000)], views = [], calls = [], menus = new Set(), logs = [];
  let publish = 0, shells = 0, deny = false, authorizeExternal;
  const input = { async request(op, payload) {
    calls.push({ op, ...payload });
    return input.respond ? input.respond(op, payload) : { ok: true, valid: true, consumed: op === 'finish' };
  }, async stop() { input.stopped = true; } };
  const electron = {
    createProjectView() { const view = { webContents: new Contents(), sentinel: { draft: 'alive', attachment: Buffer.from('same') } }; views.push(view); return view; },
    async loadProjectView() {},
    attachView(w, v) { w.children.add(v); }, parkView(w, v) { w.children.delete(v); },
    setBounds(v, b) { v.bounds = b; }, focusView(v) { v.webContents.focus(); }, destroyView(v) { v.webContents.close(); },
    async waitForTransferAttach() { return true; },
  };
  let drag;
  const registry = new DesktopTabs({ canonicalizeRoot: async x => x, transferTimeout: 100,
    backend: { async ensureProject({ path }) { return { root: path, project_id: path.slice(1), epoch: 'one' }; } }, electron,
    createShell: async () => {
      shells++; const w = new Window(2 + shells); w.visible = false; windows.push(w);
      registry.registerWindow(w, { insets: { top: 44 } }); drag.registerWindow(w); return w;
    } });
  for (const w of windows) registry.registerWindow(w, { insets: { top: 44 } });
  const a = await registry.openProject('/a', { windowId: '1' });
  const b = await registry.openProject('/b', { windowId: '1' });
  const c = await registry.openProject('/c', { windowId: '2' });
  const screen = { screenToDipPoint: p => ({ ...p }), getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }) };
  drag = createTabDrag({ registry, screen, getWindows: () => windows, nativeTerminal: input,
    authorizeSource(event, request) {
      if (authorizeExternal) return authorizeExternal(event, request);
      if (deny || !windows.some(w => String(w.id) === String(request.windowId) && w.webContents === event.sender)
        || event.senderFrame !== event.sender.mainFrame) throw new Error('Unauthorized shell');
    }, isMenuOpen: id => menus.has(id), publishState: () => { publish++; }, log: (name, data) => logs.push({ name, ...data }) });
  for (const w of windows) drag.registerWindow(w);
  const event = id => ({ sender: windows[id - 1].webContents, senderFrame: windows[id - 1].webContents.mainFrame });
  const geometry = id => {
    const w = registry.getWindow(id);
    return drag.setGeometry(event(Number(id)), { windowId: id, windowRevision: w.revision, appendStart: w.tabIds.length * 120 + 44, appendEnd: 700,
      tabs: w.tabIds.map((tabId, i) => ({ tabId, x: i * 120, y: 0, width: 120, height: 44 })) });
  };
  geometry('1'); geometry('2');
  const request = (tab = a) => ({ windowId: tab.ownerWindowId, windowRevision: registry.getWindow(tab.ownerWindowId).revision,
    tabId: tab.tabId, tabRevision: registry.getTab(tab.tabId).revision,
    point: { x: registry.getWindow(tab.ownerWindowId).tabIds.indexOf(tab.tabId) * 120 + 30, y: 20 } });
  const begin = tab => drag.begin(event(Number((tab || a).ownerWindowId)), request(tab));
  const up = (lease, x = 1100, y = 20, target = { hwnd: '2', pid: process.pid, className: 'Chrome_WidgetWin_1' }, extra = {}) =>
    input.onTerminal({ token: lease.token, kind: 'up', provenance: 'native-hook', point: { x, y }, target, ...extra });
  return { drag, registry, electron, windows, views, input, calls, logs, menus, screen, a, b, c, event, request, begin, up, geometry,
    setAuthorization: callback => { authorizeExternal = callback; },
    deny: () => { deny = true; }, shells: () => shells, publishes: () => publish,
    async cleanup() { await drag.dispose(); registry.dispose(); } };
}

// All native terminals below are injected state-machine records: these tests do
// NOT prove physical pointer delivery, mixed-DPI OS hooks or real Electron focus.
test('cross-strip moves the exact live DesktopTabs view without another shell; native finish follows ACK', async () => {
  const f = await fixture(), lease = await f.begin(), original = f.views[0], sentinel = original.sentinel;
  f.electron.waitForTransferAttach = async () => {
    assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1');
    assert.equal(f.calls.some(c => c.op === 'finish'), false); return true;
  };
  f.input.respond = async op => {
    if (op === 'finish') {
      assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1');
      assert.equal(f.windows[1].children.has(original), true);
    }
    return { valid: true, consumed: true };
  };
  assert.ok(lease.expiresAt > Date.now() && lease.expiresAt <= Date.now() + 30000);
  assert.equal(await f.up(lease), true);
  assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '2');
  assert.deepEqual(f.registry.getWindow('2').tabIds, [f.c.tabId, f.a.tabId]);
  assert.equal(f.shells(), 0); assert.equal(f.views.length, 3); assert.equal(original.sentinel, sentinel);
  assert.equal(original.webContents.isDestroyed(), false); assert.ok(original.webContents.focuses > 0);
  assert.deepEqual(f.calls.map(c => c.op), ['arm', 'validate', 'finish']); await f.cleanup();
});

test('same-strip reorder has one native finish, no native reparent, duplicate token is ignored', async () => {
  const f = await fixture(), lease = await f.begin(), before = f.windows[0].children;
  assert.equal(await f.up(lease, 220, 20, { hwnd: '1', pid: process.pid, className: 'Chrome_WidgetWin_1' }), true);
  assert.deepEqual(f.registry.getWindow('1').tabIds, [f.b.tabId, f.a.tabId]);
  assert.equal(f.windows[0].children, before); assert.equal(f.shells(), 0);
  assert.equal(await f.up(lease), undefined); assert.equal(f.calls.filter(c => c.op === 'finish').length, 1); await f.cleanup();
});

test('trailing empty strip appends, plus button is not append (including empty windows)', async () => {
  const f = await fixture();
  assert.equal(await f.up(await f.begin(), 1170), true); // target tab ends at 1120, plus ends at 1164
  assert.deepEqual(f.registry.getWindow('2').tabIds, [f.c.tabId, f.a.tabId]); await f.cleanup();
  const g = await fixture(), token = await g.begin();
  assert.equal(await g.up(token, 1140), false); assert.equal(g.registry.getTab(g.a.tabId).ownerWindowId, '1'); await g.cleanup();
});

for (const [name, x, y, target, extra] of [
  ['window controls', 1700, 20], ['below header', 1100, 44], ['right border', 1900, 20],
  ['source click threshold', 35, 20, { hwnd: '1', pid: process.pid, className: 'Chrome_WidgetWin_1' }],
  ['Escape terminal', 1100, 20, undefined, { kind: 'cancel', point: null }],
  ['renderer provenance', 1100, 20, undefined, { provenance: 'renderer' }],
  ['zero HWND', 1100, 20, { hwnd: '0', pid: 99, className: 'Desktop' }],
  ['taskbar', 1100, 20, { hwnd: '99', pid: 99, className: 'Shell_TrayWnd' }],
  ['menu', 1100, 20, { hwnd: '99', pid: 99, className: '#32768' }],
  ['dialog', 1100, 20, { hwnd: '99', pid: 99, className: '#32770' }],
  ['unregistered app surface', 1100, 20, { hwnd: '99', pid: process.pid, className: 'Chrome_WidgetWin_1' }],
]) test(`${name} fails closed without detach/finish`, async () => {
  const f = await fixture(), lease = await f.begin();
  assert.equal(await f.up(lease, x, y, target, extra), false);
  assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1'); assert.equal(f.shells(), 0);
  assert.equal(f.calls.some(c => c.op === 'finish'), false); await f.cleanup();
});

for (const change of ['revision', 'tabRevision', 'bounds', 'contentBounds', 'zoom', 'menu', 'geometry', 'authorization', 'nativeGeneration'])
  test(`late ${change} change invalidates before commit and retains live source`, async () => {
    const f = await fixture(), lease = await f.begin();
    f.electron.waitForTransferAttach = async () => {
      if (change === 'revision') f.registry.setContentInsets('2', { top: 90 });
      if (change === 'tabRevision') f.views[0].webContents.emit('render-process-gone');
      if (change === 'bounds') f.windows[1].bounds.width--;
      if (change === 'contentBounds') f.windows[1].getContentBounds = () => ({ ...f.windows[1].bounds, x: 1001 });
      if (change === 'zoom') f.windows[1].webContents.zoom = 1.25;
      if (change === 'menu') f.menus.add('2');
      if (change === 'geometry') {
        const w = f.registry.getWindow('2'); f.drag.setGeometry(f.event(2), { windowId: '2', windowRevision: w.revision,
          tabs: [{ tabId: f.c.tabId, x: 0, y: 0, width: 110, height: 44 }] });
      }
      if (change === 'authorization') f.deny();
      if (change === 'nativeGeneration') { const replacement = new Window(2, 1000); f.windows[1] = replacement; }
      return true;
    };
    assert.equal(await f.up(lease), false);
    assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1'); assert.equal(f.views[0].webContents.isDestroyed(), false);
    assert.equal(f.calls.some(c => c.op === 'finish'), false); await f.cleanup();
  });

for (const change of ['document', 'crash']) test(`target shell ${change} during attach rolls back and retires geometry`, async () => {
  const f = await fixture(), before = f.registry.snapshot(), view = f.views[1], sentinel = view.sentinel;
  const lease = await f.begin(f.b);
  f.electron.waitForTransferAttach = async () => {
    if (change === 'document') f.windows[1].webContents.mainFrame = {};
    else f.windows[1].webContents.emit('render-process-gone');
    return true;
  };
  assert.equal(await f.up(lease), false);
  assert.deepEqual(f.registry.snapshot(), before);
  assert.equal(f.windows[0].children.has(view), true); assert.equal(f.windows[1].children.has(view), false);
  assert.equal(view.webContents.isDestroyed(), false); assert.equal(view.sentinel, sentinel);
  assert.equal(f.views.length, 3); assert.equal(f.shells(), 0);
  assert.equal(f.calls.some(c => c.op === 'finish'), false);
  assert.equal(f.calls.filter(c => c.op === 'cancel').length, 1);
  assert.equal(await f.up(lease), undefined);
  // The next gesture must not reuse the old document's hit zones. Only a new
  // geometry report from the current shell makes it a target again.
  f.electron.waitForTransferAttach = async () => true;
  assert.equal(await f.up(await f.begin(f.b)), false);
  assert.deepEqual(f.registry.snapshot(), before);
  f.geometry('2');
  assert.equal(await f.up(await f.begin(f.b)), true);
  assert.equal(f.calls.filter(c => c.op === 'finish').length, 1); await f.cleanup();
});

for (const op of ['validate', 'finish']) test(`native ${op} rejection rolls back speculative attachment`, async () => {
  const f = await fixture(), before = f.registry.snapshot();
  f.input.respond = async actual => ({ valid: actual !== op, consumed: actual === 'finish', reason: 'topology-changed' });
  assert.equal(await f.up(await f.begin()), false);
  assert.deepEqual(f.registry.snapshot(), before); assert.equal(f.windows[0].children.has(f.views[0]), false); // A was parked before drag
  assert.equal(f.windows[1].children.has(f.views[0]), false); await f.cleanup();
});

test('foreign backdrop detach prepares exactly one hidden HWND before late validation; safe workArea bounds', async () => {
  const f = await fixture();
  f.screen.getDisplayNearestPoint = () => ({ workArea: { x: -1200, y: -800, width: 800, height: 600 } });
  const original = f.views[0];
  f.input.respond = async op => {
    if (['prepare', 'validate', 'finish'].includes(op)) {
      const shell = f.windows[2]; assert.equal(shell.isVisible(), false);
      assert.deepEqual(shell.bounds, { x: -1200, y: -800, width: 800, height: 600 });
      assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1');
    }
    return { valid: true, consumed: true };
  };
  assert.equal(await f.up(await f.begin(), -3000, -2000, { hwnd: '99', pid: 99, className: 'Notepad' }), true);
  assert.equal(f.shells(), 1); assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '3');
  assert.equal(f.windows[2].children.has(original), true); assert.equal(f.windows[2].isVisible(), true);
  assert.deepEqual(f.calls.map(c => c.op), ['arm', 'prepare', 'validate', 'finish']); await f.cleanup();
});

for (const fail of ['prepare', 'attach', 'finish', 'prepared-shown', 'prepared-moved']) test(`detach ${fail} failure closes its empty shell and preserves source`, async () => {
  const f = await fixture(), before = f.registry.getWindow('1');
  f.input.respond = async op => {
    if (op === fail) throw new Error('helper failure');
    if (op === 'prepare' && fail === 'prepared-shown') f.windows[2].visible = true;
    if (op === 'validate' && fail === 'prepared-moved') f.windows[2].bounds.x++;
    return { valid: true, consumed: true };
  };
  if (fail === 'attach') f.electron.waitForTransferAttach = async () => { throw new Error('target attach failed'); };
  assert.equal(await f.up(await f.begin(), 2500, 800, { hwnd: '99', pid: 99, className: 'Notepad' }), false);
  assert.equal(f.shells(), 1); assert.equal(f.windows[2].destroyed, true);
  assert.deepEqual(f.registry.getWindow('1'), before); assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1'); await f.cleanup();
});

test('DPI physical terminal converts to DIP; source CSS origin and zoom are applied, not screen renderer coords', async () => {
  const f = await fixture(); f.windows[0].bounds.x = -1000; f.windows[0].webContents.zoom = 1.25;
  f.screen.screenToDipPoint = p => ({ x: p.x / 2, y: p.y / 2 });
  const r = f.request(); r.point = { x: 24, y: 16 }; // DIP (-970,20), exactly matches physical terminal below
  const lease = await f.drag.begin(f.event(1), r);
  assert.equal(await f.up(lease, -1940, 40, { hwnd: '1', pid: process.pid, className: 'Chrome_WidgetWin_1' }), false);
  assert.equal(f.calls.some(c => c.op === 'finish'), false); await f.cleanup();
});

test('cancel is source+frame+token bound, permits stale revisions, late native record cannot resurrect gesture', async () => {
  const f = await fixture(), lease = await f.begin();
  assert.deepEqual(f.drag.cancel(f.event(2), { windowId: '2', token: lease.token }), { cancelled: false });
  assert.deepEqual(f.drag.cancel(f.event(1), { windowId: '1', token: 'wrong' }), { cancelled: false });
  assert.throws(() => f.drag.cancel({ sender: f.event(1).sender, senderFrame: {} }, { windowId: '1', token: lease.token }), /Unauthorized/);
  assert.deepEqual(f.drag.cancel(f.event(1), { windowId: '1', windowRevision: -99, token: lease.token }), { cancelled: true });
  assert.equal(await f.up(lease), undefined); assert.equal(f.shells(), 0);
  const second = await f.begin(); assert.notEqual(second.token, lease.token);
  assert.equal(await f.up(lease), undefined); assert.equal(await f.up(second), true); await f.cleanup();
});

for (const event of ['blur', 'move', 'resize', 'hide', 'minimize']) test(`native ${event} invalidates an armed gesture`, async () => {
  const f = await fixture(), lease = await f.begin(); f.windows[0].emit(event);
  assert.equal(await f.up(lease), undefined); assert.equal(f.shells(), 0); await f.cleanup();
});

test('source cancel and helper failure while ACK pending rollback without finishing; registry gate blocks competing close', async () => {
  for (const kind of ['cancel', 'helper']) {
    const f = await fixture(), gate = deferred(), lease = await f.begin();
    f.electron.waitForTransferAttach = () => gate.promise;
    const pending = f.up(lease); await tick();
    assert.throws(() => f.registry.closeTab(f.b.tabId), /busy/);
    if (kind === 'helper') f.input.onFailure(new Error('helper exited'));
    else f.drag.cancel(f.event(1), { windowId: '1', token: lease.token });
    gate.resolve(true); assert.equal(await pending, false);
    assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1'); assert.equal(f.calls.some(c => c.op === 'finish'), false); await f.cleanup();
  }
});

test('cancel between native validate and finish response prevents owner commit', async () => {
  const f = await fixture(), lease = await f.begin(), gate = deferred();
  f.input.respond = op => op === 'finish' ? gate.promise : Promise.resolve({ valid: true });
  const pending = f.up(lease); await tick();
  f.drag.cancel(f.event(1), { windowId: '1', token: lease.token });
  gate.resolve({ valid: true, consumed: true }); assert.equal(await pending, false);
  assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, '1'); await f.cleanup();
});

test('same-window asynchronous finish holds the registry gate and stale geometry fails closed', async () => {
  const f = await fixture(), lease = await f.begin(), gate = deferred();
  f.input.respond = op => op === 'finish' ? gate.promise : Promise.resolve({ valid: true });
  const pending = f.up(lease, 220, 20, { hwnd: '1', pid: process.pid, className: 'Chrome_WidgetWin_1' }); await tick();
  assert.throws(() => f.registry.closeTab(f.b.tabId), /busy/);
  f.windows[0].bounds.width--;
  gate.resolve({ valid: true, consumed: true }); assert.equal(await pending, false);
  assert.deepEqual(f.registry.getWindow('1').tabIds, [f.a.tabId, f.b.tabId]); await f.cleanup();
});

test('menu/hidden/stale geometry at begin or target are unsupported, not foreign detach', async () => {
  const f = await fixture(); f.menus.add('1'); await assert.rejects(f.begin(), /menu/); f.menus.clear();
  f.windows[0].focused = false; await assert.rejects(f.begin(), /source/); f.windows[0].focused = true;
  assert.throws(() => f.drag.setGeometry(f.event(1), { windowId: '1', windowRevision: -1, tabs: [] }), /stale/);
  f.menus.add('2'); assert.equal(await f.up(await f.begin()), false); assert.equal(f.shells(), 0); await f.cleanup();
});

test('dispose cancels native lease and removes listeners exactly once, without disposing project registry', async () => {
  const f = await fixture(), lease = await f.begin(), before = f.windows[0].listenerCount('resize');
  await Promise.all([f.drag.dispose(), f.drag.dispose()]); assert.equal(f.input.stopped, true);
  assert.equal(f.windows[0].listenerCount('resize'), before - 1);
  assert.equal(await f.up(lease), undefined); assert.equal(f.views[0].webContents.isDestroyed(), false);
  await assert.rejects(f.begin(), /disposed/); f.registry.dispose();
});

function transport(options = {}) {
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.stdin = new PassThrough();
  child.kills = 0; child.kill = () => { child.kills++; };
  const errors = [], input = new NativeTerminal(() => {}, () => {}, { spawnProcess: () => child,
    startupTimeout: 30, commandTimeout: 20, stopTimeout: 10, ...options });
  input.onFailure = e => errors.push(e);
  const emit = object => child.stdout.write(JSON.stringify(object) + '\n');
  const ready = () => emit({ event: 'ready', ready: true, provenance: 'native-hook' });
  return { child, input, errors, emit, ready };
}
for (const kind of ['startup-timeout', 'command-timeout', 'malformed-json', 'exit', 'pipe'])
  test(`production helper transport ${kind} poisons requests, fails closed and bounded stop kills helper`, async () => {
    const f = transport();
    if (kind !== 'startup-timeout') f.ready();
    const pending = f.input.request('arm', { token: 'one', synthetic: false });
    await tick();
    if (kind === 'malformed-json') f.child.stdout.write('not JSON\n');
    if (kind === 'exit') f.child.emit('exit', 1);
    if (kind === 'pipe') f.child.stdin.emit('error', new Error('broken pipe'));
    await assert.rejects(pending); assert.equal(f.errors.length, 1); assert.ok(f.child.kills > 0);
    await assert.rejects(f.input.request('validate', { token: 'one' }));
    await Promise.all([f.input.stop(), f.input.stop()]);
  });

test('production transport routes only native records and replies, stop before ready remains bounded', async () => {
  const f = transport({ startupTimeout: 1000 }); let terminal;
  f.input.onTerminal = record => { terminal = record; }; f.ready();
  const pending = f.input.request('validate', { token: 'one' }); await tick();
  f.emit({ id: '1', ok: true, valid: true }); assert.equal((await pending).valid, true);
  f.emit({ event: 'terminal', token: 'one', kind: 'cancel' }); assert.equal(terminal.kind, 'cancel'); await f.input.stop();
  const g = transport({ startupTimeout: 1000 }); await g.input.stop(); assert.ok(g.child.kills > 0);
});


test('active source tab rollback restores exact view, order, active state and focus after native finish rejection', async () => {
  const f = await fixture(), before = f.registry.snapshot();
  const view = f.views[1];
  f.input.respond = async op => ({ valid: op !== 'finish', consumed: op === 'finish' });
  assert.equal(await f.up(await f.begin(f.b)), false);
  assert.deepEqual(f.registry.snapshot(), before);
  assert.equal(f.windows[0].children.has(view), true); assert.equal(f.windows[1].children.has(view), false);
  assert.equal(f.windows[0].children.size, 1); assert.equal(f.windows[1].children.has(f.views[2]), true);
  assert.ok(view.webContents.focuses > 0); assert.equal(view.webContents.isDestroyed(), false); await f.cleanup();
});

test('expired main token and terminal token mismatch cannot detach or finish', async () => {
  const f = await fixture(), lease = await f.begin(), now = Date.now;
  assert.equal(await f.up({ ...lease, token: 'not-the-token' }), undefined);
  Date.now = () => lease.expiresAt;
  try { assert.equal(await f.up(lease), false); } finally { Date.now = now; }
  assert.equal(f.shells(), 0); assert.equal(f.calls.some(c => c.op === 'finish'), false); await f.cleanup();
});

for (const x of [1020, 1060]) test(`empty existing strip x=${x} respects plus exclusion and append`, async () => {
  const f = await fixture(); f.registry.closeTab(f.c.tabId); f.geometry('2');
  const success = x > 1044;
  assert.equal(await f.up(await f.begin(), x), success);
  assert.equal(f.registry.getTab(f.a.tabId).ownerWindowId, success ? '2' : '1'); assert.equal(f.shells(), 0); await f.cleanup();
});

test('source lost during hidden native preparation retains the live view in recovery, never orphaned', async () => {
  const f = await fixture();
  f.input.respond = async op => {
    if (op === 'prepare') f.windows[0].close();
    return { valid: true, consumed: true };
  };
  assert.equal(await f.up(await f.begin(f.b), 2500, 800, { hwnd: '99', pid: 99, className: 'Notepad' }), false);
  const tab = f.registry.getTab(f.b.tabId); assert.ok(tab); assert.equal(tab.state, 'ready'); assert.equal(tab.ownerWindowId, '4');
  assert.equal(f.views[1].webContents.isDestroyed(), false); assert.equal(f.windows[2].destroyed, true);
  assert.equal(f.windows[3].children.has(f.views[1]), true); await f.cleanup();
});


test('source window loss during asynchronous reorder finish recovers the same live renderer', async () => {
  const f = await fixture(), lease = await f.begin(f.b), gate = deferred();
  f.input.respond = op => op === 'finish' ? gate.promise : Promise.resolve({ valid: true });
  const pending = f.up(lease, 20, 20, { hwnd: '1', pid: process.pid, className: 'Chrome_WidgetWin_1' });
  await tick(); f.windows[0].close(); gate.resolve({ valid: true, consumed: true });
  assert.equal(await pending, false);
  assert.equal(f.registry.getTab(f.b.tabId).ownerWindowId, '3'); assert.equal(f.views[1].webContents.isDestroyed(), false);
  assert.equal(f.windows[2].children.has(f.views[1]), true); await f.cleanup();
});


test('clipped visible tab subsets preserve real insertion indices; unproven gutter never appends', async () => {
  const f = await fixture();
  const d = await f.registry.openProject('/d', { windowId: '2' });
  const w = f.registry.getWindow('2');
  f.drag.setGeometry(f.event(2), { windowId: '2', windowRevision: w.revision,
    tabs: [{ tabId: d.tabId, x: 0, y: 0, width: 120, height: 44 }] });
  assert.equal(await f.up(await f.begin(), 1020), true);
  assert.deepEqual(f.registry.getWindow('2').tabIds, [f.c.tabId, f.a.tabId, d.tabId]); await f.cleanup();
  const g = await fixture(), target = g.registry.getWindow('2');
  g.drag.setGeometry(g.event(2), { windowId: '2', windowRevision: target.revision,
    tabs: [{ tabId: g.c.tabId, x: 0, y: 0, width: 120, height: 44 }] });
  assert.equal(await g.up(await g.begin(), 1500), false); assert.equal(g.shells(), 0); await g.cleanup();
});


test('actual IPC authorization and actual native coordinator accept matching stale-revision cancellation', async () => {
  const { registerTabsIpc } = require('../tabs-ipc.cjs');
  const f = await fixture();
  for (const w of f.windows) {
    w.webContents.mainFrame.url = 'file:///shell.html';
    w.webContents.getURL = () => w.webContents.mainFrame.url;
    w.webContents.send = () => {};
  }
  const bridge = registerTabsIpc({ registry: f.registry, shellURL: 'file:///shell.html',
    ipcMain: { handle() {}, removeHandler() {} } });
  for (const w of f.windows) bridge.registerWindow(w);
  f.setAuthorization((event, request) => bridge.authorizeDrag(event, request));
  const lease = await f.begin();
  assert.deepEqual(f.drag.cancel(f.event(1), { windowId: '1', windowRevision: 0, token: lease.token }), { cancelled: true });
  assert.equal(await f.up(lease), undefined); bridge.dispose(); await f.cleanup();
});
