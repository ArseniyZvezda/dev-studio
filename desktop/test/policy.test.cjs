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
