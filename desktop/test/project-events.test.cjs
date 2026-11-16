'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createProjectEventReceiver, validateDesktopTarget } = require('../project-events.cjs');

const EPOCH = 'backend-one';
const locator = (id, epoch = EPOCH) => ({ project_id: id, epoch, root: `D:\\Games\\${id}`, name: id });
const notice = (id, target = '/tasks?task=same') => ({
  project: locator(id), source_seq: 7, target,
  notification: { automation_id: 'same', automation_name: 'Notify', title: 'Ready', message: 'Review', trigger: 'task', values: { task_id: 'same' } },
});
const attention = (id, chats = ['same'], epoch = EPOCH) => ({
  project: locator(id, epoch), source_seq: 7, target: '/producer',
  waiting: chats.map(chat_id => ({ chat_id, target: `/producer?chat=${encodeURIComponent(chat_id)}` })),
});
const event = (type, seq, data, epoch = EPOCH) => ({ type, seq, epoch, data });
const hello = (seq = 0, values = [], epoch = EPOCH, resumed_from = null) => event('system.hello', seq,
  { epoch, resumed_from, ...(resumed_from === null ? { attention: values } : {}) }, epoch);
const dropped = (seq, values = [], epoch = EPOCH) => ({ type: 'system.dropped', epoch,
  data: { resync: true, reason: 'overflow', count: 2, snapshot_seq: seq, attention: values } });

function harness(options = {}) {
  const sockets = [], pending = new Map(), cancelled = [], notices = [], attentionStates = [], gaps = [], errors = [];
  let nextTimer = 0;
  class WebSocket {
    constructor(url) { this.url = url; this.listeners = new Map(); this.closed = 0; sockets.push(this); }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    removeEventListener(name, fn) { if (this.listeners.get(name) === fn) this.listeners.delete(name); }
    close() { this.closed++; }
    send(frame) { this.raw(JSON.stringify(frame)); }
    raw(data) { this.listeners.get('message')?.({ data }); }
    end() { this.listeners.get('close')?.({}); }
    fail() { this.listeners.get('error')?.({}); }
  }
  const timers = {
    setTimeout(fn, ms) { const id = nextTimer++; pending.set(id, { fn, ms }); return id; },
    clearTimeout(id) { cancelled.push(id); pending.delete(id); },
  };
  const receiver = createProjectEventReceiver({ WebSocket, timers,
    onNotification: (data, meta) => notices.push({ data, meta }),
    onAttention: (data, meta) => attentionStates.push({ data, meta }),
    onGap: (data, meta) => gaps.push({ data, meta }),
    onError: error => errors.push(error), ...options });
  return { receiver, sockets, pending, cancelled, notices, attentionStates, gaps, errors,
    latest: () => sockets.at(-1),
    tick() {
      assert.equal(pending.size, 1);
      const [id, timer] = pending.entries().next().value;
      pending.delete(id); timer.fn();
    },
    states: () => attentionStates.map(({ data }) => data.map(value => [value.project.project_id, value.waiting.map(chat => chat.chat_id)])),
  };
}

test('targets allow only project pages and preserve normal encoded page-local selectors', () => {
  const pages = ['/explorer', '/tasks', '/producer', '/teams', '/chats', '/documents', '/documents/graph',
    '/pipelines', '/pipelines/safe_ID-1', '/automations', '/vcs', '/memory', '/skills', '/mcp', '/plugins', '/project-settings', '/statistics'];
  for (const page of pages) assert.equal(validateDesktopTarget(page), page);
  for (const target of ['/explorer?path=' + encodeURIComponent('src/a b.as'),
    '/producer?chat=' + encodeURIComponent('Пример /?&=+%'), '/documents?path=folder%2Fdoc.md', '/tasks?task=same&view=board']) {
    assert.equal(validateDesktopTarget(target), target);
  }
});

test('targets refuse global, foreign, scoped, traversing and encoded path destinations', () => {
  for (const value of [null, undefined, {}, '', 'tasks', '/', '/settings', '/api', '/api/tasks', '/ws/desktop',
    '/p/a/tasks', '/desktop', 'https://example.com/tasks', 'http://127.0.0.1:8765/tasks', '//example.com/tasks',
    '/\\example.com/tasks', '/tasks\\x', '/tasks#fragment', '/tasks#', '/tasks/../producer',
    '/tasks/%2e%2e/producer', '/%74asks', '/pipelines/%2fother', '/pipelines/%5cother',
    '/pipelines/a.b', '/pipelines/a/b', '/documents/other', '/tasks/', '/tasks\n', '/tasks\0',
    '/tasks?project_id=a', '/producer?epoch=e', '/tasks?%70roject_id=a', '/tasks?root=D%3A%5Ca', '/tasks?project=a']) {
    assert.equal(validateDesktopTarget(value), null, String(value));
  }
});

test('inert app-level receiver delivers A/B with equal local cursors and entity IDs, with zero windows', () => {
  const h = harness();
  assert.equal(h.sockets.length, 0);
  assert.equal(h.receiver.start(), h.receiver);
  h.receiver.start(); h.receiver.start();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.latest().url, 'ws://127.0.0.1:8765/ws/desktop');
  h.latest().send(hello());
  h.latest().send(event('desktop.notification', 1, notice('A')));
  h.latest().send(event('desktop.notification', 2, notice('B')));
  h.latest().send(event('desktop.notification', 3, notice('A', null)));
  assert.deepEqual(h.notices.map(({ data, meta }) => [data.project.project_id, data.source_seq, meta]), [
    ['A', 7, { epoch: EPOCH, seq: 1 }], ['B', 7, { epoch: EPOCH, seq: 2 }], ['A', 7, { epoch: EPOCH, seq: 3 }],
  ]);
  assert.deepEqual(h.errors, []);
  h.receiver.dispose();
});

test('attention is complete per-project replacement, not chat/entity-ID deduplication', () => {
  const h = harness(); h.receiver.start();
  h.latest().send(hello(0, [attention('A')]));
  h.latest().send(event('desktop.producer_attention', 1, attention('B')));
  h.latest().send(event('desktop.producer_attention', 2, attention('A', ['other', 'space / Пример'])));
  h.latest().send(event('desktop.producer_attention', 3, attention('B', [])));
  h.latest().send(event('desktop.producer_attention', 4, attention('A', [])));
  assert.deepEqual(h.states(), [
    [['A', ['same']]], [['A', ['same']], ['B', ['same']]],
    [['A', ['other', 'space / Пример']], ['B', ['same']]], [['A', ['other', 'space / Пример']]], [],
  ]);
  assert.deepEqual(h.attentionStates.map(({ meta }) => meta.snapshot), [true, false, false, false, false]);
  h.receiver.dispose();
});

test('fixed auth URL is browser-WebSocket compatible; reconnect sends app epoch/cursor and refreshed token', () => {
  let token = 'secret &+?#/%';
  const h = harness({ token: () => token, origin: 'https://untrusted.example', reconnectDelayMs: 23 });
  h.receiver.start();
  const first = new URL(h.latest().url);
  assert.equal(first.origin, 'ws://127.0.0.1:8765'); assert.equal(first.pathname, '/ws/desktop');
  assert.deepEqual([...first.searchParams], [['token', token]]);
  h.latest().send(hello(10));
  h.latest().send(event('desktop.notification', 11, notice('A'))); // local source_seq is 7, NOT 11.
  h.latest().end(); token = 'next';
  assert.equal([...h.pending.values()][0].ms, 23);
  h.receiver.start(); assert.equal(h.sockets.length, 1);
  h.tick();
  const next = new URL(h.latest().url);
  assert.deepEqual([...next.searchParams], [['token', 'next'], ['epoch', EPOCH], ['since_seq', '11']]);
  h.receiver.dispose();
});

test('resumed hello head does not skip replay; own attention survives, duplicates are scalar-cursor deduped', () => {
  const h = harness(); h.receiver.start();
  h.latest().send(hello());
  h.latest().send(event('desktop.producer_attention', 1, attention('A')));
  h.latest().send(event('desktop.notification', 2, notice('A')));
  h.latest().end(); h.tick();
  h.latest().send(hello(5, undefined, EPOCH, 2));
  assert.deepEqual(h.states(), [[], [['A', ['same']]]]);
  for (const seq of [2, 3, 3, 4, 5]) h.latest().send(event('desktop.notification', seq, notice(seq === 4 ? 'B' : 'A')));
  assert.deepEqual(h.notices.map(({ meta }) => meta.seq), [2, 3, 4, 5]);
  h.latest().send(event('desktop.producer_attention', 6, attention('B')));
  assert.deepEqual(h.states().at(-1), [['A', ['same']], ['B', ['same']]]);
  h.receiver.dispose();
});

test('gap replaces ALL attention; watermark suppresses old attention but not queued notifications', () => {
  const h = harness(); h.receiver.start(); h.latest().send(hello());
  h.latest().send(event('desktop.producer_attention', 1, attention('A')));
  h.latest().send(dropped(7, [attention('B')]));
  h.latest().send(event('desktop.producer_attention', 2, attention('A', ['stale'])));
  h.latest().send(event('desktop.notification', 3, notice('A')));
  h.latest().send(event('desktop.producer_attention', 4, attention('B', [])));
  h.latest().send(event('desktop.notification', 5, notice('B')));
  h.latest().send(event('desktop.producer_attention', 7, attention('A')));
  assert.deepEqual(h.states(), [[], [['A', ['same']]], [['B', ['same']]]]);
  assert.deepEqual(h.notices.map(({ meta }) => meta.seq), [3, 5]);
  assert.equal(h.gaps.length, 1); assert.equal(h.gaps[0].meta.seq, 7);
  h.latest().send(event('desktop.producer_attention', 8, attention('B', [])));
  assert.deepEqual(h.states().at(-1), []);
  h.latest().end(); h.tick();
  assert.equal(new URL(h.latest().url).searchParams.get('since_seq'), '8');
  h.receiver.dispose();
});

test('fresh/gap hello snapshot uses hello seq as watermark and resets attention authoritatively', () => {
  const h = harness(); h.receiver.start();
  h.latest().send(hello(20, [attention('A')]));
  h.latest().send(event('desktop.producer_attention', 20, attention('A', [])));
  h.latest().send(event('desktop.notification', 19, notice('B'))); // Predates initial subscription.
  h.latest().send(event('desktop.notification', 21, notice('A')));
  h.latest().end(); h.tick();
  h.latest().send(hello(30, [])); // Server could not resume: live-only + authoritative snapshot.
  h.latest().send(dropped(30, []));
  assert.deepEqual(h.states(), [[['A', ['same']]], [], []]);
  assert.deepEqual(h.notices.map(({ meta }) => meta.seq), [21]);
  assert.equal(h.gaps.length, 1);
  h.receiver.dispose();
});

test('sequenced source-overflow markers dedup by app seq, not the newer snapshot watermark', () => {
  const h = harness(); h.receiver.start(); h.latest().send(hello());
  const marker = { ...dropped(10, [attention('A')]), seq: 1 };
  h.latest().send(marker); h.latest().send(marker);
  assert.equal(h.gaps.length, 1);
  h.latest().send(event('desktop.notification', 2, notice('B')));
  assert.equal(h.notices.length, 1);
  h.latest().send({ ...dropped(10), seq: '100' });
  h.latest().send({ ...dropped(10), seq: 11 });
  h.latest().end(); h.tick();
  assert.equal(new URL(h.latest().url).searchParams.get('since_seq'), '2');
  h.receiver.dispose();
});

test('epoch change requires new connection hello; duplicate hello and delayed old-socket callbacks cannot rotate scope', () => {
  const h = harness(); h.receiver.start();
  const old = h.latest(); old.send(hello(8, [attention('A')]));
  const queuedMessage = old.listeners.get('message'), queuedClose = old.listeners.get('close'), queuedError = old.listeners.get('error');
  old.send(hello(0, [], 'backend-two'));
  old.send(event('desktop.notification', 999, { ...notice('B'), project: locator('B', 'backend-two') }, 'backend-two'));
  old.send(event('desktop.notification', 9, notice('A')));
  assert.equal(h.notices.length, 1);
  old.end(); h.tick();
  const current = h.latest();
  current.send(event('desktop.notification', 99, notice('B'))); // No connection hello yet.
  current.send(hello(0, [], 'backend-two'));
  queuedMessage({ data: JSON.stringify(hello(900, [attention('A')])) }); queuedClose(); queuedError();
  current.send(event('desktop.notification', 999, notice('A'))); // Delayed old epoch, no cursor poisoning.
  current.send(event('desktop.notification', 1, { ...notice('B'), project: locator('B', 'backend-two') }, 'backend-two'));
  assert.deepEqual(h.notices.map(({ data, meta }) => [data.project.project_id, meta.epoch, meta.seq]), [
    ['A', EPOCH, 9], ['B', 'backend-two', 1],
  ]);
  assert.deepEqual(h.states(), [[['A', ['same']]], []]);
  assert.equal(h.pending.size, 0); assert.equal(current.closed, 0);
  current.end(); h.tick();
  const url = new URL(h.latest().url);
  assert.equal(url.searchParams.get('epoch'), 'backend-two'); assert.equal(url.searchParams.get('since_seq'), '1');
  h.receiver.dispose();
});

test('malformed, disallowed and foreign frames never invoke domain callbacks or poison cursor', () => {
  const h = harness(); h.receiver.start();
  const socket = h.latest();
  for (const frame of [hello(-1), hello(0, undefined, 'wrong', 0), { ...hello(), data: { epoch: EPOCH, resumed_from: null } },
    hello(0, [attention('A', ['same'], 'wrong')])]) socket.send(frame);
  socket.send(event('desktop.notification', 100, notice('A'))); // All hellos above are invalid.
  assert.equal(h.attentionStates.length, 0); assert.equal(h.notices.length, 0);
  socket.send(hello());
  const baseline = h.attentionStates.length;
  for (const raw of ['', '{', 'null', '[]', 'true', '42', '{}']) socket.raw(raw);
  const invalid = [
    event('automation.notification', 100, notice('A')), event('project.opened', 100, notice('A')),
    event('desktop.notification', 100, { ...notice('A'), source_seq: true }),
    event('desktop.notification', 100, { ...notice('A'), target: '/p/B/tasks' }),
    event('desktop.notification', 100, { ...notice('A'), target: '/settings' }),
    event('desktop.notification', 100, { ...notice('A'), target: undefined }),
    event('desktop.notification', 100, { ...notice('A'), notification: {} }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), epoch: 'wrong' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), root: 'relative' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), root: 'https://foreign.example' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), root: 'D:\\A\\..\\B' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), project_id: '../B' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), project_id: 'A\n' } }),
    event('desktop.notification', 100, { ...notice('A'), project: { ...locator('A'), name: '' } }),
    event('desktop.notification', '100', notice('A')), event('desktop.notification', Infinity, notice('A')),
    { ...event('desktop.notification', 100, notice('A')), project_id: 'A' },
    event('desktop.producer_attention', 100, { ...attention('A'), target: '/tasks' }),
    event('desktop.producer_attention', 100, { ...attention('A'), waiting: [{ chat_id: 'same', target: '/producer?chat=foreign' }] }),
    event('desktop.producer_attention', 100, { ...attention('A'), waiting: [{ chat_id: 'same', target: '/producer?chat=same&epoch=other' }] }),
    event('desktop.producer_attention', 100, attention('A', ['same', 'same'])),
    { ...dropped(100), data: { ...dropped(100).data, attention: [attention('A'), attention('A')] } },
    { ...dropped(100), data: { ...dropped(100).data, snapshot_seq: -1 } },
    { ...dropped(100), data: { resync: true, count: 1, reason: 'overflow' } },
    dropped(100, [attention('A', ['same'], 'wrong')]), dropped(100, [], 'wrong'),
  ];
  for (const frame of invalid) socket.send(frame);
  socket.send({ type: 'system.pong', epoch: EPOCH, seq: 100, data: {} });
  assert.equal(h.attentionStates.length, baseline); assert.deepEqual(h.gaps, []); assert.deepEqual(h.notices, []);
  socket.send(event('desktop.notification', 1, notice('A')));
  assert.equal(h.notices.length, 1);
  socket.end(); h.tick();
  assert.equal(new URL(h.latest().url).searchParams.get('since_seq'), '1');
  h.receiver.dispose();
});

test('invalid resumed hello cannot replace scope or skip replay', () => {
  const h = harness(); h.receiver.start(); h.latest().send(hello(10));
  h.latest().end(); h.tick();
  for (const frame of [hello(20, [], EPOCH, 11), hello(9, [], EPOCH, 10), hello(20, [], 'foreign', 10),
    { ...hello(20, [], EPOCH, 10), data: { epoch: EPOCH, resumed_from: 10, attention: [] } }]) h.latest().send(frame);
  h.latest().send(event('desktop.notification', 11, notice('A')));
  assert.equal(h.notices.length, 0);
  h.latest().send(hello(20, [], EPOCH, 10));
  h.latest().send(event('desktop.notification', 11, notice('A')));
  assert.equal(h.notices.length, 1);
  h.receiver.dispose();
});

test('dispose is terminal/idempotent, removes all listeners and cancels even timer ID zero', () => {
  const h = harness(); h.receiver.start(); const old = h.latest();
  const queued = old.listeners.get('message');
  h.receiver.dispose(); h.receiver.dispose(); h.receiver.start();
  queued({ data: JSON.stringify(hello()) }); old.end(); old.fail();
  assert.equal(old.closed, 1); assert.equal(old.listeners.size, 0); assert.equal(h.pending.size, 0);
  assert.equal(h.sockets.length, 1); assert.deepEqual(h.attentionStates, []);
  const pending = harness(); pending.receiver.start(); pending.latest().end();
  const queuedTimer = [...pending.pending.values()][0].fn;
  assert.ok(pending.pending.has(0)); pending.receiver.dispose(); pending.receiver.dispose();
  queuedTimer();
  assert.deepEqual(pending.cancelled, [0]); assert.equal(pending.pending.size, 0); assert.equal(pending.sockets.length, 1);
  const inert = harness(); inert.receiver.dispose(); inert.receiver.start(); assert.equal(inert.sockets.length, 0);
});

test('transport errors retry once, isolate observer errors, and arguments cannot mutate retained attention', () => {
  const seen = [];
  const h = harness({ onAttention(values) {
    seen.push(values);
    if (values.length) { assert.ok(Object.isFrozen(values[0].waiting[0])); assert.ok(Object.isFrozen(values[0].project)); }
  }, onNotification(data) { assert.ok(Object.isFrozen(data.notification.values)); throw new Error('observer'); } });
  h.receiver.start(); const socket = h.latest(); socket.send(hello(0, [attention('A')]));
  socket.send(event('desktop.notification', 1, notice('A')));
  assert.equal(h.errors[0].message, 'observer'); assert.ok(Object.isFrozen(seen[0]));
  socket.fail(); socket.end(); h.receiver.start();
  assert.equal(h.errors.length, 2); assert.equal(socket.closed, 1); assert.equal(socket.listeners.size, 0);
  assert.equal(h.pending.size, 1); h.tick(); assert.equal(h.sockets.length, 2);
  h.receiver.dispose();
});

test('constructor failure retries, global WebSocket default needs no Electron or renderer', t => {
  const previous = globalThis.WebSocket, urls = [];
  t.after(() => { globalThis.WebSocket = previous; });
  class Socket extends EventEmitter {
    constructor(url) { super(); urls.push(url); }
    close() {}
  }
  globalThis.WebSocket = Socket;
  const receiver = createProjectEventReceiver(); receiver.start(); receiver.start(); receiver.dispose();
  assert.deepEqual(urls, ['ws://127.0.0.1:8765/ws/desktop']);
  const h = harness({ WebSocket: class { constructor() { throw new Error('offline'); } } });
  h.receiver.start(); assert.equal(h.errors.length, 1); assert.equal(h.pending.size, 1);
  h.tick(); assert.equal(h.errors.length, 2); assert.equal(h.pending.size, 1);
  h.receiver.dispose(); assert.equal(h.pending.size, 0);
});

test('node-style socket messages work and are detached, without accumulating notification identity state', () => {
  let socket;
  const received = [];
  class Socket extends EventEmitter {
    constructor() { super(); socket = this; }
    close() {}
  }
  const receiver = createProjectEventReceiver({ WebSocket: Socket, onNotification: (_data, meta) => received.push(meta.seq) });
  receiver.start(); socket.emit('message', Buffer.from(JSON.stringify(hello())));
  for (let seq = 1; seq <= 2000; seq++) socket.emit('message', Buffer.from(JSON.stringify(event('desktop.notification', seq, notice('A')))));
  socket.emit('message', Buffer.from(JSON.stringify(event('desktop.notification', 1, notice('A')))));
  assert.equal(received.length, 2000); assert.equal(received.at(-1), 2000);
  receiver.dispose();
  for (const name of ['message', 'close', 'error']) assert.equal(socket.listenerCount(name), 0);
});
