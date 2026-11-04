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
