const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { releaseInfo } = require('../scripts/release.cjs');
test('installer takes the visible release tag rather than the Python package version', () => {
  assert.deepEqual(releaseInfo({tag:'v4.1',version:'0.1.0',short_commit:'abcdef0'}), {display:'v4.1',version:'4.1.0',installerVersion:'4.1.0'});
  assert.deepEqual(releaseInfo({tag:'v4.2.3-beta.1',version:'0.1.0'}), {display:'v4.2.3-beta.1',version:'4.2.3-beta.1',installerVersion:'4.2.3-beta.1'});
});
test('untagged builds retain the same commit label as the application', () => {
  assert.deepEqual(releaseInfo({short_commit:'abcdef0',version:'0.1.0'}), {display:'abcdef0',version:'abcdef0',installerVersion:'0.1.0'});
  assert.deepEqual(releaseInfo({version:'4.2.0'}), {display:'4.2.0',version:'4.2.0',installerVersion:'4.2.0'});
  assert.throws(() => releaseInfo({}), /Invalid Studio build label/);
});

test('non-SemVer tags remain the exact runtime identity', () => {
  assert.deepEqual(releaseInfo({tag:'release/v5.2.0',version:'5.0.0'}),
    {display:'release/v5.2.0',version:'release/v5.2.0',installerVersion:'5.0.0'});
});

test('a stamped commit identity retains a numeric fallback only for the installer format', () => {
  assert.deepEqual(releaseInfo({short_commit:'abcdef0',version:'abcdef0'}, '5.0.0'),
    {display:'abcdef0',version:'abcdef0',installerVersion:'5.0.0'});
  assert.throws(() => releaseInfo({short_commit:'abcdef0',version:'abcdef0'}), /Invalid Studio installer version/);
});

function installerConfig(stamp) {
  const desktop = path.resolve(__dirname, '..');
  const installerLabels = [];
  const context = {
    __dirname: desktop, module: { exports: {} }, process: { env: {} },
    require(name) {
      if (name === 'node:fs') return {
        existsSync: () => stamp !== null,
        readFileSync: () => JSON.stringify(stamp),
      };
      if (name === './scripts/installer-messages.cjs') return {
        writeInstallerMessages(displayVersion) { installerLabels.push(displayVersion); },
      };
      return require(name.startsWith('.') ? path.resolve(desktop, name) : name);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(desktop, 'electron-builder.cjs'), 'utf8'), context);
  return { config: context.module.exports, installerLabels };
}

test('installer metadata requires the same tag version as the runtime', () => {
  const { config, installerLabels } = installerConfig({ tag: 'v5.2.0', version: '5.2.0' });
  assert.equal(config.extraMetadata.version, '5.2.0');
  assert.equal(config.extraMetadata.studioVersion, '5.2.0');
  assert.equal(config.extraMetadata.backendVersion, undefined);
  assert.equal(config.artifactName, 'Voxarium-Studio-Setup-v5.2.0.${ext}');
  assert.deepEqual(installerLabels, ['v5.2.0']);
});

test('artifact names sanitize tag path separators without changing the runtime version', () => {
  const { config, installerLabels } = installerConfig({ tag: 'release/v5.2.0', version: 'release/v5.2.0' });
  assert.equal(config.extraMetadata.studioVersion, 'release/v5.2.0');
  assert.equal(config.artifactName, 'Voxarium-Studio-Setup-release-v5.2.0.${ext}');
  assert.deepEqual(installerLabels, ['release/v5.2.0']);
});

test('commit builds show the stamped commit while retaining valid technical installer metadata', () => {
  const { config, installerLabels } = installerConfig({ tag: null, short_commit: 'abcdef0', version: 'abcdef0' });
  assert.equal(config.extraMetadata.version, require('../package.json').version);
  assert.equal(config.extraMetadata.studioVersion, 'abcdef0');
  assert.equal(config.artifactName, 'Voxarium-Studio-Setup-abcdef0.${ext}');
  assert.deepEqual(installerLabels, ['abcdef0']);
});

test('a build without a wheel stamp uses the package fallback consistently', () => {
  const { config, installerLabels } = installerConfig(null);
  const fallback = require('../package.json').version;
  assert.equal(config.extraMetadata.version, fallback);
  assert.equal(config.extraMetadata.studioVersion, fallback);
  assert.equal(config.artifactName, `Voxarium-Studio-Setup-${fallback}.\${ext}`);
  assert.deepEqual(installerLabels, [fallback]);
});
