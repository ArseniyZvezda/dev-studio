const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createSessionStateAdapter } = require('../session-state.cjs');
const { ORIGIN } = require('../policy.cjs');

const manifest = () => ({ version: 1, windows: [{ tabs: [{ root: 'D:\\Games\\A',
  page: { path: '/tasks', query: { task: 'same-id' } } }], active_tab: 0,
geometry: { x: 0, y: -20, width: 800, height: 600 } }], active_window: 0 });
const response = (value, status = 200) => ({ status, ok: status >= 200 && status < 300,
  async json() { return value; } });

test('load/save use fixed backend JSON endpoint, dynamic authorization and safe fetch policy', async () => {
  const calls = [];
  let token = 'first';
  const adapter = createSessionStateAdapter({ token: () => token, fetch: async (url, options) => {
    calls.push({ url, ...options });
    return response(manifest());
  } });
  assert.deepEqual(await adapter.load(), manifest());
  token = 'second';
  assert.deepEqual(await adapter.save(manifest()), manifest());
  for (const call of calls) {
    assert.equal(call.url, `${ORIGIN}/api/desktop/session`);
    assert.equal(call.redirect, 'error');
    assert.equal(call.cache, 'no-store');
    assert.equal(call.headers.Accept, 'application/json');
  }
  assert.equal(calls[0].method, 'GET');
  assert.equal(Object.hasOwn(calls[0], 'body'), false);
  assert.equal(calls[0].headers.Authorization, 'Bearer first');
  assert.equal(calls[1].method, 'PUT');
  assert.equal(calls[1].headers.Authorization, 'Bearer second');
  assert.equal(calls[1].headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[1].body), manifest());
});

test('missing manifest and backend failures reject explicitly without retries or fallback', async () => {
  for (const [status, code] of [[404, 'desktop_session_missing'], [422, 'desktop_session_invalid'],
    [500, 'desktop_session_write_failed']]) {
    let calls = 0;
    const adapter = createSessionStateAdapter({ fetch: async () => {
      calls++;
      return response({ detail: { code } }, status);
    } });
    await assert.rejects(adapter.load(), error => error.status === status && error.code === code);
    await assert.rejects(adapter.save(manifest()), error => error.status === status && error.code === code);
    assert.equal(calls, 2);
  }
  const adapter = createSessionStateAdapter({ fetch: async () => response({ detail: 'Not Found' }, 404) });
  await assert.rejects(adapter.load(), { status: 404, code: 'desktop_session_http_error' });
});

test('Explorer literal hash and percent paths survive save/load without URL decoding', async () => {
  const value = manifest();
  value.windows[0].tabs[0].page = { path: '/explorer',
    query: { path: 'assets/#draft/100%/%2e%2e%2fmesh%23.vox' } };
  let stored = value;
  const adapter = createSessionStateAdapter({ fetch: async (_url, options) => {
    if (options.method === 'PUT') {
      assert.deepEqual(JSON.parse(options.body), value);
      stored = JSON.parse(options.body);
    }
    return response(stored);
  } });
  assert.deepEqual(await adapter.save(value), value);
  assert.deepEqual(await adapter.load(), value);
});

test('literal Explorer paths retain safety checks independently of route URLs', async () => {
  for (const [field, path] of [
    ['query', '/absolute'], ['query', '../outside'], ['query', 'assets/../outside'],
    ['query', 'C:\\outside'], ['query', 'https://example.com'], ['query', 'assets/\u0000mesh'],
    ['route', '/explorer#draft'], ['route', '/explorer%'], ['route', '/%2e%2e/tasks'],
  ]) {
    const value = manifest();
    const page = value.windows[0].tabs[0].page;
    if (field === 'query') page.query = { path };
    else page.path = path;
    const adapter = createSessionStateAdapter({ fetch: async () => response(value) });
    await assert.rejects(adapter.save(value), TypeError);
    await assert.rejects(adapter.load(), { code: 'desktop_session_invalid_response' });
  }
});

test('malformed JSON, false success, envelopes, extras and unsupported versions are rejected', async () => {
  const invalid = [null, false, {}, { manifest: manifest() }, { ...manifest(), version: 2 },
    { ...manifest(), runtime_id: 'secret' }, { ...manifest(), active_window: null }];
  for (const mutate of [m => { m.windows[0].tabs[0].runtime_id = 'id'; },
    m => { m.windows[0].tabs[0].page.query.unknown = 'value'; },
    m => { m.windows[0].tabs[0].page.path = 'https://example.com'; },
    m => { m.windows[0].tabs[0].page.path = '/p/old-id/tasks'; },
    m => { m.windows[0].active_tab = 2; },
    m => { m.windows[0].geometry.width = 1; }]) {
    const value = manifest(); mutate(value); invalid.push(value);
  }
  for (const value of invalid) {
    const adapter = createSessionStateAdapter({ fetch: async () => response(value) });
    await assert.rejects(adapter.load(), { status: 200, code: 'desktop_session_invalid_response' });
    await assert.rejects(adapter.save(manifest()), { status: 200, code: 'desktop_session_invalid_response' });
  }
  for (const status of [200, 503]) {
    const adapter = createSessionStateAdapter({ fetch: async () => ({ status, ok: status === 200,
      json: async () => { throw new SyntaxError('bad JSON'); } }) });
    await assert.rejects(adapter.load(), error => error.status === status && !!error.code);
  }
});

test('empty topology is a valid success and optional authorization can be omitted', async () => {
  const empty = { version: 1, windows: [], active_window: null };
  const adapter = createSessionStateAdapter({ fetch: async (_url, options) => {
    assert.equal(Object.hasOwn(options.headers, 'Authorization'), false);
    return response(empty);
  } });
  assert.deepEqual(await adapter.load(), empty);
});

test('parallel saves snapshot immediately, serialize in call order and wait for backend success', async () => {
  const calls = [], releases = [];
  let started;
  const firstStarted = new Promise(resolve => { started = resolve; });
  const adapter = createSessionStateAdapter({ fetch: async (_url, options) => {
    calls.push(JSON.parse(options.body));
    started();
    return new Promise(resolve => releases.push(() => resolve(response(calls.at(-1)))));
  } });
  const first = manifest(), second = manifest();
  second.windows[0].tabs[0].page.path = '/plugins/foo';
  const a = adapter.save(first), b = adapter.save(second);
  first.windows.length = 0;
  second.windows[0].tabs[0].page.path = '/mutated';
  let settled = false;
  a.then(() => { settled = true; });
  await firstStarted;
  assert.equal(calls.length, 1);
  assert.equal(settled, false);
  releases.shift()();
  assert.deepEqual(await a, manifest());
  // Let the next queued request start, without a timer/sleep.
  await Promise.resolve(); await Promise.resolve();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].windows[0].tabs[0].page.path, '/plugins/foo');
  releases.shift()();
  assert.equal((await b).windows[0].tabs[0].page.path, '/plugins/foo');
});

test('a rejected queued write or serialization error does not poison future writes', async () => {
  const offline = new Error('offline');
  let calls = 0;
  const adapter = createSessionStateAdapter({ fetch: async () => {
    if (++calls === 1) throw offline;
    return response(manifest());
  } });
  const a = adapter.save(manifest()), b = adapter.save(manifest());
  await assert.rejects(a, error => error === offline);
  assert.deepEqual(await b, manifest());
  const cycle = {}; cycle.self = cycle;
  await assert.rejects(adapter.save(cycle), TypeError);
  await assert.rejects(adapter.save({ ...manifest(), draft: undefined }), TypeError);
  assert.deepEqual(await adapter.save(manifest()), manifest());
  assert.equal(calls, 3);
});
