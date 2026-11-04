const { test } = require('node:test');
const assert = require('node:assert/strict');
const { releaseInfo } = require('../scripts/release.cjs');
test('installer takes the visible release tag rather than the Python package version', () => {
  assert.deepEqual(releaseInfo({tag:'v4.1',version:'0.1.0',short_commit:'abcdef0'}), {display:'v4.1',version:'4.1.0'});
  assert.deepEqual(releaseInfo({tag:'v4.2.3-beta.1',version:'0.1.0'}), {display:'v4.2.3-beta.1',version:'4.2.3-beta.1'});
});
test('untagged builds retain the same commit label as the application', () => {
  assert.deepEqual(releaseInfo({short_commit:'abcdef0',version:'0.1.0'}), {display:'abcdef0',version:'0.1.0'});
  assert.deepEqual(releaseInfo({version:'4.2.0'}), {display:'4.2.0',version:'4.2.0'});
  assert.throws(() => releaseInfo({tag:'../bad',version:'0.1.0'}));
});
