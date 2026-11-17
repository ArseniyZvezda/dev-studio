'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createTaskCounts } = require('../task-counts.cjs');
const { ORIGIN } = require('../policy.cjs');

const tab = (name, patch = {}) => ({ tabId: `tab-${name}`, ownerWindowId: '1', revision: 1,
  state: 'ready', transferGeneration: 0,
  project: { root: `/projects/${name}`, project_id: `project-${name}`, epoch: 'epoch & one' }, ...patch });
const response = value => ({ ok: true, json: async () => value });
const counts = (in_progress, needs_review) => ({ in_progress, needs_review });
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
function fakeTimers() {
  let now = 0, next = 0;
  const pending = new Map();
  return { pending,
    setTimeout(callback, ms) { const id = ++next; pending.set(id, { callback, at: now + ms }); return id; },
    clearTimeout(id) { pending.delete(id); },
    async tick(ms) {
      const end = now + ms;
      while (true) {
        const due = [...pending].filter(([, value]) => value.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at; pending.delete(due[0]); due[1].callback(); await settle();
      }
      now = end; await settle();
    },
  };
}
function fixture(initial = [tab('A')], options = {}) {
  const timers = fakeTimers(), calls = [], changes = [];
  let tabs = initial, secret = 'first-token';
  const service = createTaskCounts({ timers, token: () => secret, getTabs: () => tabs,
    onChange: () => { changes.push(tabs.map(item => service.getCounts(item))); service.refresh(); },
    fetch: (url, request) => { const gate = deferred(); calls.push({ url, request, gate }); return gate.promise; }, ...options });
  return { timers, calls, changes, service, setTabs(value) { tabs = value; }, setToken(value) { secret = value; } };
}

test('inert construction; one start polls all windows/inactive/crashed tabs every three seconds without registry writes', async () => {
  const a = tab('A'), b = tab('B', { ownerWindowId: '2', state: 'crashed' });
  const before = JSON.stringify([a, b]), f = fixture([a, b]);
  assert.equal(f.calls.length, 0); assert.equal(f.timers.pending.size, 0);
  f.service.refresh(); assert.equal(f.calls.length, 0);
  f.service.start(); f.service.start(); assert.equal(f.calls.length, 2);
  for (const [i, project] of [a, b].entries()) {
    const { url, request } = f.calls[i];
    assert.equal(url, `${ORIGIN}/api/projects/${project.project.project_id}/project/task-counts?epoch=epoch%20%26%20one`);
    assert.equal(request.method, 'GET'); assert.equal(request.redirect, 'error'); assert.equal(request.cache, 'no-store');
    assert.deepEqual(request.headers, { Authorization: 'Bearer first-token' });
    assert.equal(f.service.getCounts(project), null);
  }
  f.calls[0].gate.resolve(response(counts(2, 1))); f.calls[1].gate.resolve(response(counts(0, 0))); await settle();
  assert.deepEqual(f.service.getCounts(a), counts(2, 1)); assert.deepEqual(f.service.getCounts(b), counts(0, 0));
  assert.ok(Object.isFrozen(f.service.getCounts(a)));
  assert.equal(f.changes.length, 2, 'each completed project can promptly update the shell');
  f.service.refresh(); assert.equal(f.calls.length, 2, 'onChange/beforePublish refresh cannot recursively poll existing entries');
  await f.timers.tick(2999); assert.equal(f.calls.length, 2);
  f.setToken('new-token'); await f.timers.tick(1); assert.equal(f.calls.length, 4);
  assert.equal(f.calls[2].request.headers.Authorization, 'Bearer new-token');
  f.calls[2].gate.resolve(response(counts(2, 1))); f.calls[3].gate.resolve(response(counts(0, 0))); await settle();
  assert.equal(f.changes.length, 2, 'equal data does not republish');
  assert.equal(JSON.stringify([a, b]), before, 'no state, revision or transfer generation writes');
  f.service.stop(); assert.equal(f.timers.pending.size, 0);
});

test('new tabs poll promptly, transfer preserves cache and in-flight polling never overlaps', async () => {
  const a = tab('A'), b = tab('B'), f = fixture([a]); f.service.start();
  await f.timers.tick(6000); assert.equal(f.calls.length, 1, 'slow request is not overlapped by interval ticks');
  f.setTabs([a, b]); f.service.refresh(); assert.equal(f.calls.length, 2, 'new tab does not wait for polling interval');
  f.calls[0].gate.resolve(response(counts(8, 3))); f.calls[1].gate.resolve(response(counts(0, 4))); await settle();
  const transferred = { ...a, ownerWindowId: '2', revision: 2, transferGeneration: 1 };
  f.setTabs([transferred, b]); f.service.refresh();
  assert.equal(f.calls.length, 2); assert.deepEqual(f.service.getCounts(transferred), counts(8, 3));
  await f.timers.tick(3000); assert.equal(f.calls.length, 4); f.service.stop();
  assert.equal(f.calls[2].request.signal.aborted, true); assert.equal(f.calls[3].request.signal.aborted, true);
});

test('closed, replaced root/id/epoch and reused tab identities discard late responses even without a refresh', async () => {
  for (const replacement of [null, { root: '/projects/replaced' }, { project_id: 'project-replaced' }, { epoch: 'new-epoch' }]) {
    const a = tab('A'), f = fixture([a]); f.service.start();
    const next = replacement ? { ...a, project: { ...a.project, ...replacement } } : null;
    f.setTabs(next ? [next] : []);
    f.calls[0].gate.resolve(response(counts(99, 99))); await settle();
    assert.equal(f.changes.length, 0); assert.equal(f.service.getCounts(a), null);
    if (next) assert.equal(f.service.getCounts(next), null);
    f.service.refresh();
    if (next) {
      assert.equal(f.calls.length, 2); f.calls[1].gate.resolve(response(counts(1, 0))); await settle();
      assert.deepEqual(f.service.getCounts(next), counts(1, 0));
    }
    f.service.stop();
  }
  const a = tab('A'), f = fixture([a]); f.service.start(); f.setTabs([]); f.service.refresh();
  assert.equal(f.calls[0].request.signal.aborted, true);
  f.setTabs([a]); f.service.refresh();
  f.calls[1].gate.resolve(response(counts(1, 2))); await settle();
  f.calls[0].gate.resolve(response(counts(99, 99))); await settle();
  assert.deepEqual(f.service.getCounts(a), counts(1, 2)); assert.equal(f.changes.length, 1); f.service.stop();
});

test('request binding/query is immutable when caller mutates a source descriptor', async () => {
  const a = tab('A'), f = fixture([a]); f.service.start();
  a.project.epoch = 'replacement'; a.project.project_id = 'replacement';
  assert.ok(f.calls[0].url.includes('/project-A/')); assert.ok(f.calls[0].url.endsWith('epoch=epoch%20%26%20one'));
  f.calls[0].gate.resolve(response(counts(9, 9))); await settle();
  assert.equal(f.changes.length, 0); assert.equal(f.service.getCounts(a), null); f.service.stop();
});

test('null, malformed counts, HTTP/JSON/transport failures remain unknown, never zero or stale success', async () => {
  const invalid = [null, {}, [], { in_progress: 2 }, counts(-1, 0), counts(0, 0.5), counts('2', 0),
    counts(Number.MAX_SAFE_INTEGER + 1, 0), counts(0, Infinity)];
  for (const value of [...invalid, 'http', 'json', 'transport']) {
    const a = tab('A'), f = fixture([a]); f.service.start();
    f.calls[0].gate.resolve(response(counts(4, 2))); await settle(); await f.timers.tick(3000);
    if (value === 'transport') f.calls[1].gate.reject(new Error('offline'));
    else if (value === 'http') f.calls[1].gate.resolve({ ok: false, status: 404, json() { throw new Error('do not read'); } });
    else if (value === 'json') f.calls[1].gate.resolve({ ok: true, json() { throw new Error('bad json'); } });
    else f.calls[1].gate.resolve(response(value));
    await settle(); assert.equal(f.service.getCounts(a), null); assert.equal(f.changes.length, 2);
    await f.timers.tick(3000); f.calls[2].gate.resolve(response(counts(0, 0))); await settle();
    assert.deepEqual(f.service.getCounts(a), counts(0, 0)); f.service.stop();
  }
});

test('stop clears timers, aborts work and ignores late transport/JSON completions; restart cannot resurrect it', async () => {
  for (const waitingForJson of [false, true]) {
    const a = tab('A'), f = fixture([a]), json = deferred(); f.service.start();
    if (waitingForJson) { f.calls[0].gate.resolve({ ok: true, json: () => json.promise }); await settle(); }
    f.service.stop(); f.service.stop();
    assert.equal(f.calls[0].request.signal.aborted, true); assert.equal(f.timers.pending.size, 0);
    f.calls[0].gate.resolve(response(counts(5, 2))); json.resolve(counts(5, 2)); await settle();
    f.service.start(); f.service.refresh(); await f.timers.tick(30000);
    assert.equal(f.calls.length, 1); assert.equal(f.changes.length, 0); assert.equal(f.service.getCounts(a), null);
  }
});

test('timeout aborts a hung request, rejects late success and permits subsequent polling after settlement', async () => {
  const a = tab('A'), f = fixture([a]); f.service.start(); await f.timers.tick(10000);
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].request.signal.aborted, true);
  f.calls[0].gate.resolve(response(counts(99, 99))); await settle();
  assert.equal(f.service.getCounts(a), null); assert.equal(f.changes.length, 0);
  await f.timers.tick(2000); assert.equal(f.calls.length, 2);
  f.calls[1].gate.resolve(response({ ...counts(1, 2), token: 'SECRET' })); await settle();
  assert.deepEqual(f.service.getCounts(a), counts(1, 2)); f.service.stop();
});

test('empty registry stays inert and observer errors cannot break scheduling', async () => {
  const f = fixture([], { onChange() { throw new Error('observer'); } });
  f.service.start(); await f.timers.tick(3000); assert.equal(f.calls.length, 0);
  const a = tab('A'); f.setTabs([a]); f.service.refresh();
  f.calls[0].gate.resolve(response(counts(1, 1))); await settle(); await f.timers.tick(3000);
  assert.equal(f.calls.length, 2); f.service.stop();
});
