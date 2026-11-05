const { test } = require('node:test');
const assert = require('node:assert/strict');
const { installerMessages } = require('../scripts/installer-messages.cjs');
test('native installer messages escape NSIS metacharacters before substituting runtime values', () => {
  assert.equal(installerMessages({ ENGLISH: { test: '"$value"\n{version} / {installedVersion} / {directory}' } }, 'abcdef0'),
    'LangString studio_buildVersion ${LANG_ENGLISH} "abcdef0"\n!ifndef BUILD_UNINSTALLER\nLangString studio_test ${LANG_ENGLISH} "$\\"$$value$\\"$\\r$\\n$(studio_buildVersion) / $studioInstalledVersion / $studioInstallDir"\n!endif\n');
});

test('build labels are escaped as literal NSIS strings', () => {
  assert.equal(installerMessages({ ENGLISH: {} }, 'release/"${VERSION}"/$%PATH%/$value').trim(),
    'LangString studio_buildVersion ${LANG_ENGLISH} "release/${U+24}\\"${U+24}${U+24}{VERSION}${U+24}\\"/${U+24}${U+24}%PATH%/${U+24}${U+24}value"');
});

test('both languages show the stamped build identity instead of the numeric installer version', () => {
  const catalogues = {
    ENGLISH: require('../../frontend/src/locales/en.json').installer,
    RUSSIAN: require('../../frontend/src/locales/ru.json').installer,
  };
  const output = installerMessages(catalogues, 'abcdef0');
  for (const language of Object.keys(catalogues)) {
    assert.ok(output.includes(`LangString studio_buildVersion \${LANG_${language}} "abcdef0"`));
    const line = output.split('\n').find(value => value.startsWith(`LangString studio_updateVersions \${LANG_${language}}`));
    assert.ok(line?.includes('$(studio_buildVersion)'));
    assert.ok(line.includes('$studioInstalledVersion'));
    assert.ok(!line.includes('${VERSION}'));
  }
});

test('process prompts are present in both installer and uninstaller languages', () => {
  const catalogues = {
    ENGLISH: require('../../frontend/src/locales/en.json').installer,
    RUSSIAN: require('../../frontend/src/locales/ru.json').installer,
  };
  const output = installerMessages(catalogues, 'v5.2.0');
  for (const language of Object.keys(catalogues)) {
    for (const key of ['appBusy', 'checkFailed', 'closeFailed']) {
      const line = output.split('\n').find(value => value.startsWith(`LangString studio_${key} \${LANG_${language}}`));
      assert.ok(line);
      assert.ok(!output.includes(`!ifndef BUILD_UNINSTALLER\n${line}`));
    }
  }
});
