const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { updateFeedOptions, configureUpdateFeed } = require('../update-feed.cjs');

const repository = 'example-org/studio-releases';
const expectedPublic = { provider: 'github', owner: 'example-org', repo: 'studio-releases', private: false };

function noCredentials(env) {
  return Object.defineProperties(env, {
    GH_TOKEN: { get() { throw new Error('Public update read GH_TOKEN'); } },
    GITHUB_TOKEN: { get() { throw new Error('Public update read GITHUB_TOKEN'); } },
  });
}

test('public feeds never read personal tokens or invoke GitHub CLI', async () => {
  for (const privateFlag of [undefined, '', '0', 'false', 'true']) {
    const calls = [];
    await configureUpdateFeed({
      env: noCredentials({ VOXARIUM_STUDIO_UPDATE_REPO: repository, VOXARIUM_STUDIO_UPDATE_PRIVATE: privateFlag }),
      updater: { setFeedURL: feed => calls.push(feed) },
      readCliToken() { throw new Error('Public update invoked gh'); },
    });
    assert.deepEqual(calls, [expectedPublic]);
  }
});

test('an explicitly private feed uses an environment token, then an existing CLI login', async () => {
  for (const key of ['GH_TOKEN', 'GITHUB_TOKEN']) {
    let received;
    const env = { VOXARIUM_STUDIO_UPDATE_REPO: repository, VOXARIUM_STUDIO_UPDATE_PRIVATE: '1', [key]: 'test-token' };
    await configureUpdateFeed({ env, updater: { setFeedURL: feed => { received = feed; } },
      readCliToken() { throw new Error('Should use the supplied token'); } });
    assert.deepEqual(received, { ...expectedPublic, private: true, token: 'test-token' });
    assert.equal(Object.hasOwn(updateFeedOptions(env), 'token'), false);
  }
  const calls = [];
  await configureUpdateFeed({
    env: { VOXARIUM_STUDIO_UPDATE_REPO: repository, VOXARIUM_STUDIO_UPDATE_PRIVATE: '1' },
    updater: { setFeedURL: feed => calls.push(feed) },
    readCliToken: async () => { calls.push('gh'); return 'test-cli-token'; },
  });
  assert.deepEqual(calls, ['gh', { ...expectedPublic, private: true, token: 'test-cli-token' }]);
});

test('private credentials are required before configuring a private feed', async () => {
  await assert.rejects(configureUpdateFeed({
    env: { VOXARIUM_STUDIO_UPDATE_REPO: repository, VOXARIUM_STUDIO_UPDATE_PRIVATE: '1' },
    updater: { setFeedURL() { assert.fail('An unauthenticated private feed must not be configured'); } },
    readCliToken: async () => '', accessError: 'Sign in to the private repository',
  }), /Sign in to the private repository/);
});

function buildFeed(env) {
  // Exercise the real build configuration without generating installer files.
  const context = { module: { exports: {} }, process: { env }, __dirname: path.resolve(__dirname, '..'),
    require(name) {
      if (name === './scripts/installer-messages.cjs') return { writeInstallerMessages() {} };
      if (name === 'node:fs') return { existsSync: () => false };
      return name.startsWith('./') ? require(path.join(__dirname, '..', name)) : require(name);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../electron-builder.cjs'), 'utf8'), context);
  return context.module.exports.publish;
}

test('build and runtime share exact owner/repository validation and never embed credentials', async () => {
  for (const privateFlag of [undefined, '1']) {
    const env = noCredentials({ VOXARIUM_STUDIO_UPDATE_REPO: repository, VOXARIUM_STUDIO_UPDATE_PRIVATE: privateFlag });
    assert.deepEqual(Array.from(buildFeed(env)), [updateFeedOptions(env)]);
  }
  for (const value of ['owner/repository/extra', 'https://github.com/owner/repository',
    'owner/repo?token=secret', 'owner/repo#release', 'user:password/repository',
    '../repository', 'owner/..', '-owner/repository', 'owner/repo\n', ' owner/repository', 'owner/repo name']) {
    const env = noCredentials({ VOXARIUM_STUDIO_UPDATE_REPO: value, VOXARIUM_STUDIO_UPDATE_PRIVATE: '1' });
    assert.throws(() => buildFeed(env), /Expected owner\/repository/);
    await assert.rejects(configureUpdateFeed({ env,
      updater: { setFeedURL() { assert.fail('Malformed feed must not reach the updater'); } },
      readCliToken() { assert.fail('Malformed feed must not read credentials'); },
    }), /Expected owner\/repository/);
  }
});

test('build and runtime default to the public Studio repository without reading credentials', async () => {
  const env = noCredentials({});
  const expected = { provider: 'github', owner: 'voxarium-studio', repo: 'dev-studio', private: false };
  assert.deepEqual(Array.from(buildFeed(env)), [expected]);
  const calls = [];
  await configureUpdateFeed({ env,
    updater: { setFeedURL(feed) { calls.push(feed); } },
    readCliToken() { assert.fail('The default public feed must not invoke gh'); },
  });
  assert.deepEqual(calls, [expected]);
});
