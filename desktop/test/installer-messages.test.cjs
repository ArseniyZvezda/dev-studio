const { test } = require('node:test');
const assert = require('node:assert/strict');
const { installerMessages } = require('../scripts/installer-messages.cjs');
test('native installer messages escape NSIS metacharacters before substituting runtime values', () => {
  assert.equal(installerMessages({ ENGLISH: { test: '"$value"\n{version} / {installedVersion} / {directory}' } }),
    '!ifndef BUILD_UNINSTALLER\nLangString studio_test ${LANG_ENGLISH} "$\\"$$value$\\"$\\r$\\n${VERSION} / $studioInstalledVersion / $studioInstallDir"\n!endif\n');
});

test('process prompts are present in both installer and uninstaller languages', () => {
  const catalogues = {
    ENGLISH: require('../../frontend/src/locales/en.json').installer,
    RUSSIAN: require('../../frontend/src/locales/ru.json').installer,
  };
  const output = installerMessages(catalogues);
  for (const language of Object.keys(catalogues)) {
    for (const key of ['appBusy', 'checkFailed', 'closeFailed']) {
      const line = output.split('\n').find(value => value.startsWith(`LangString studio_${key} \${LANG_${language}}`));
      assert.ok(line);
      assert.ok(!output.includes(`!ifndef BUILD_UNINSTALLER\n${line}`));
    }
  }
});
