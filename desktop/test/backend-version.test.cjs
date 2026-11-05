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

test('matching commit identities attach without asking or reading the project', async () => {
  assert.deepEqual(await backendStartPlan(health('abcdef0'), 'abcdef0',
    () => assert.fail('Matching builds do not read the project'),
    () => assert.fail('Matching builds do not ask for replacement')), { reuse: true });
});

test('different commits require consent in both directions without ordering their hashes', async () => {
  for (const [running, expected] of [['abcdef0', '1234567'], ['1234567', 'abcdef0'],
    ['ABCDEF0', '7654321'], ['a'.repeat(40), 'b'.repeat(40)]]) {
    const calls = [];
    const plan = await backendStartPlan(health(running), expected, async () => {
      calls.push('project');
      return { root: 'D:\\Games\\SampleGame' };
    }, async () => {
      calls.push('consent');
      await Promise.resolve();
      calls.push('confirmed');
      return true;
    });
    assert.deepEqual(calls, ['consent', 'confirmed', 'project']);
    assert.deepEqual(plan, { reuse: false, replacement: true, args: ['-NoOpen', '-Project', 'D:\\Games\\SampleGame'] });
  }
});

test('switching between a commit and a release or prerelease requires consent', async () => {
  for (const [running, expected] of [['abcdef0', '5.2.0'], ['5.2.0', 'abcdef0'],
    ['abcdef0', '5.2.0-rc.1'], ['5.2.0-rc.1', 'abcdef0']]) {
    let confirmations = 0;
    const plan = await backendStartPlan(health(running), expected,
      async () => ({ root: 'D:\\Games\\SampleGame' }),
      async () => { confirmations++; return true; });
    assert.equal(confirmations, 1);
    assert.equal(plan.replacement, true);
  }
});

test('declined or unavailable consent cancels without reading the project', async () => {
  for (const confirm of [undefined, async () => false]) {
    assert.deepEqual(await backendStartPlan(health('abcdef0'), '1234567',
      () => assert.fail('Cancelled replacement must not read the project'), confirm), { cancelled: true });
  }
});

test('commit replacement still rejects unidentified builds and invalid projects', async () => {
  for (const invalid of [undefined, null, '', ' ', 1234567, 'abcdef', 'gabcdef', 'a'.repeat(41),
    '5.2.0-', '05.2.0', '5.2.0-beta.01', 'unknown']) {
    for (const [running, expected] of [[invalid, 'abcdef0'], ['abcdef0', invalid]]) {
      await assert.rejects(backendStartPlan(health(running), expected,
        () => assert.fail('Invalid versions must not read the project'),
        () => assert.fail('Invalid versions must not ask for replacement')), /unknownBackendVersion/);
    }
  }
  for (const project of [undefined, {}, { root: 'relative' }, { root: 123 }]) {
    await assert.rejects(backendStartPlan(health('abcdef0'), '1234567', async () => project,
      async () => true), /restartProjectUnknown/);
  }
});

test('consent cannot bypass newer-release or foreign-service protection', async () => {
  await assert.rejects(backendStartPlan(health('5.3.0'), '5.2.0',
    () => assert.fail(), () => assert.fail('Ordered releases must not ask for a downgrade')), /desktopTooOld/);
  await assert.rejects(backendStartPlan({ ...health('abcdef0'), service: 'other' }, '1234567',
    () => assert.fail(), () => assert.fail('A foreign service must not ask for replacement')), /portBusy/);
  await assert.rejects(backendStartPlan({ ...health('abcdef0'), status: 'starting' }, '1234567',
    () => assert.fail(), () => assert.fail('An unhealthy server must not ask for replacement')), /portBusy/);
});
