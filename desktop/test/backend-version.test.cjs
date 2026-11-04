const { test } = require('node:test');
const assert = require('node:assert/strict');
const { backendStartPlan, compareVersions } = require('../backend-version.cjs');

const health = version => ({ service: 'voxarium_studio', status: 'ok', version });
test('matching server is attached without reading or changing its project', async () => {
  assert.deepEqual(await backendStartPlan(health('4.6.0'), '4.6.0', () => assert.fail()), { reuse: true });
});
test('an old backend is replaced through the normal launcher with its current project', async () => {
  const plan = await backendStartPlan(health('4.5.0'), '4.6.0', async () => ({ root: 'D:\\Games\\SampleGame' }));
  assert.equal(plan.replacement, true);
  assert.deepEqual(plan.args, ['-NoOpen', '-Project', 'D:\\Games\\SampleGame']);
  assert.equal(plan.args.includes('-Desktop'), false);
});
test('first launch retains the non-evicting desktop mode', async () => {
  assert.deepEqual((await backendStartPlan(null, '4.6.0')).args, ['-NoOpen', '-Desktop']);
});
test('a newer or unidentified backend, another service and an unknown project are not killed', async () => {
  await assert.rejects(backendStartPlan(health('4.10.0'), '4.6.0'), /desktopTooOld/);
  await assert.rejects(backendStartPlan(health(undefined), '4.6.0'), /unknownBackendVersion/);
  await assert.rejects(backendStartPlan({ ...health('4.5.0'), service: 'other' }, '4.6.0'), /portBusy/);
  await assert.rejects(backendStartPlan(health('4.5.0'), '4.6.0', async () => ({ root: 'relative' })), /restartProjectUnknown/);
  assert.equal(compareVersions('4.10.0', '4.9.0'), 1);
});

test('prerelease tags follow SemVer precedence when deciding which backend is newer', () => {
  const versions = ['5.2.0-alpha', '5.2.0-alpha.1', '5.2.0-alpha.beta', '5.2.0-beta',
    '5.2.0-beta.2', '5.2.0-beta.10', '5.2.0-rc.1', '5.2.0', '5.2.1-alpha'];
  for (let i = 1; i < versions.length; i++) {
    assert.equal(compareVersions(versions[i - 1], versions[i]), -1);
    assert.equal(compareVersions(versions[i], versions[i - 1]), 1);
  }
  assert.equal(compareVersions('5.2.0-beta.2', '5.2.0-beta.2'), 0);
  for (const invalid of ['5.2.0-', '5.2.0-beta..1', '5.2.0-beta.01', '05.2.0']) {
    assert.throws(() => compareVersions(invalid, '5.2.0'), /unknownBackendVersion/);
  }
});

test('a stable backend is not downgraded to a prerelease, while older prereleases can be replaced', async () => {
  await assert.rejects(backendStartPlan(health('5.2.0'), '5.2.0-rc.1'), /desktopTooOld/);
  await assert.rejects(backendStartPlan(health('5.2.0-beta.10'), '5.2.0-beta.2'), /desktopTooOld/);
  const plan = await backendStartPlan(health('5.2.0-beta.2'), '5.2.0-beta.10',
    async () => ({ root: 'D:\\Games\\SampleGame' }));
  assert.equal(plan.replacement, true);
  assert.deepEqual(await backendStartPlan(health('5.2.0-rc.1'), '5.2.0-rc.1'), { reuse: true });
});

test('commit identities require an exact match and cannot be ordered for automatic replacement', async () => {
  assert.deepEqual(await backendStartPlan(health('abcdef0'), 'abcdef0'), { reuse: true });
  await assert.rejects(backendStartPlan(health('abcdef0'), '1234567'), /unknownBackendVersion/);
  await assert.rejects(backendStartPlan(health('5.2.0'), 'abcdef0'), /unknownBackendVersion/);
  await assert.rejects(backendStartPlan(health('abcdef0'), '5.2.0'), /unknownBackendVersion/);
});
