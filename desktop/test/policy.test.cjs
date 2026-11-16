const { test } = require('node:test');
const assert = require('node:assert/strict');
const { ORIGIN, navigationKind, registerPermissions } = require('../policy.cjs');
test('only the Studio origin stays inside Electron', () => {
  assert.equal(navigationKind('http://127.0.0.1:8765/documents?q=1'), 'internal');
  for (const url of ['https://example.com', 'http://127.0.0.1:9999', 'mailto:a@example.com'])
    assert.equal(navigationKind(url), 'external');
  for (const url of ['file:///C:/Windows', 'javascript:alert(1)', 'data:text/html,hello', 'voxarium_studio://run', 'invalid'])
    assert.equal(navigationKind(url), 'blocked');
});
test('native messages have matching translation keys', () => {
  assert.deepEqual(Object.keys(require('../../frontend/src/locales/en.json').desktop), Object.keys(require('../../frontend/src/locales/ru.json').desktop));
});

test('clipboard writes are allowed only for the Studio main frame in both permission handlers', () => {
  let check, request;
  let currentUrl = `${ORIGIN}/explorer`;
  const contents = {
    getURL: () => currentUrl,
    session: {
      setPermissionCheckHandler: handler => { check = handler; },
      setPermissionRequestHandler: handler => { request = handler; },
    },
  };
  registerPermissions(contents);
  function verify(expected, sender = contents, permission = 'clipboard-sanitized-write',
    url = `${ORIGIN}/explorer`, isMainFrame = true) {
    const details = { requestingUrl: url, isMainFrame };
    assert.equal(check(sender, permission, url, details), expected);
    let result;
    request(sender, permission, granted => { result = granted; }, details);
    assert.equal(result, expected);
  }
  verify(true);
  verify(true, contents, 'clipboard-sanitized-write', `${ORIGIN}/documents`);
  for (const permission of ['clipboard-read', 'deprecated-sync-clipboard-read', 'media', 'notifications', 'unknown'])
    verify(false, contents, permission);
  for (const url of ['https://example.com', 'http://127.0.0.1:9999', 'http://127.0.0.1:8765.evil.test', 'invalid', undefined])
    verify(false, contents, 'clipboard-sanitized-write', url === undefined ? '' : url);
  verify(false, contents, 'clipboard-sanitized-write', ORIGIN, false);
  verify(false, null);
  verify(false, { getURL: () => ORIGIN });
  assert.equal(check(contents, 'clipboard-sanitized-write', ORIGIN, undefined), false);
  currentUrl = 'https://example.com';
  verify(false);
});

test('session permissions install once and track multiple live controller roles', () => {
  let check, request, installs = 0;
  const session = { setPermissionCheckHandler: fn => { check = fn; installs++; },
    setPermissionRequestHandler: fn => { request = fn; installs++; } };
  const contents = (url = `${ORIGIN}/p/a`) => ({ url, getURL() { return this.url; }, isDestroyed() { return !!this.destroyed; } });
  const a = contents(), b = contents(`${ORIGIN}/desktop`), foreign = contents();
  const roles = new Map([[a, 'project'], [b, 'shell']]);
  const controller = { checkContents(sender) {
    const role = roles.get(sender);
    if (!role || (role === 'project' && !sender.url.startsWith(`${ORIGIN}/p/a`))) return false;
    return { role, contents: sender };
  } };
  const api = registerPermissions(session, controller);
  assert.equal(registerPermissions(session, controller), api);
  assert.equal(installs, 2);
  const verify = (sender, expected, url = ORIGIN, isMainFrame = true, permission = 'clipboard-sanitized-write') => {
    const details = { requestingUrl: url, isMainFrame };
    assert.equal(check(sender, permission, url, details), expected);
    let granted;
    request(sender, permission, value => { granted = value; }, details);
    assert.equal(granted, expected);
  };
  verify(a, true); verify(b, true); verify(foreign, false);
  verify(a, false, ORIGIN, false); verify(a, false, ORIGIN, true, 'clipboard-read');
  verify(a, false, 'https://example.com');
  verify(a, false, 'http://user:pass@127.0.0.1:8765');
  a.url = `${ORIGIN}/p/foreign`; verify(a, false); // Same origin is not a role registration.
  a.url = `${ORIGIN}/p/a`;
  roles.delete(b); verify(b, false); verify(a, true); // Closing B does not revoke A.
  a.destroyed = true; verify(a, false);
  a.destroyed = false;
  api.dispose(); api.dispose(); verify(a, false);
});

test('session permissions can use controller authorize and fail closed on stale registration', () => {
  let check;
  const session = { setPermissionCheckHandler: fn => { check = fn; }, setPermissionRequestHandler() {} };
  const contents = { mainFrame: { url: `${ORIGIN}/p/a` }, getURL() { return this.mainFrame.url; } };
  let live = true;
  const api = registerPermissions(session, { authorize(event) {
    assert.equal(event.sender, contents); assert.equal(event.senderFrame, contents.mainFrame);
    if (!live) throw new Error('stale');
    return { role: 'project', contents };
  } });
  assert.equal(check(contents, 'clipboard-sanitized-write', ORIGIN, { isMainFrame: true }), true);
  live = false;
  assert.equal(check(contents, 'clipboard-sanitized-write', ORIGIN, { isMainFrame: true }), false);
  api.dispose();
});

test('legacy permission registration is additive within a shared session', () => {
  let check, installs = 0;
  const session = { setPermissionCheckHandler: fn => { check = fn; installs++; }, setPermissionRequestHandler: () => { installs++; } };
  const a = { session, getURL: () => ORIGIN }, b = { session, getURL: () => ORIGIN };
  const api = registerPermissions(a);
  assert.equal(registerPermissions(b), api);
  assert.equal(installs, 2);
  for (const sender of [a, b]) assert.equal(check(sender, 'clipboard-sanitized-write', ORIGIN, { isMainFrame: true }), true);
  api.dispose();
});
