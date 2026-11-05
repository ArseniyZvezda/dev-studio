// Keep native installer text in the same bilingual catalogues as the UI (§6.4).
const fs = require('node:fs');
const path = require('node:path');
function escapeNsis(value) {
  return value.replaceAll('$', () => '$$').replaceAll('"', '$\\"').replace(/\r?\n/g, '$\\r$\\n');
}
function installerMessages(catalogues, displayVersion) {
  if (typeof displayVersion !== 'string' || !displayVersion) throw new Error('Missing Studio installer display version');
  // Show the wheel's tag/commit, independently of Electron's numeric SemVer (§2.7).
  const variables = { version: '$(studio_buildVersion)', installedVersion: '$studioInstalledVersion', directory: '$studioInstallDir' };
  // Compile the label once as a LangString; a tag containing ${...} or $%...%
  // must stay literal through NSIS preprocessing and runtime string expansion.
  const label = escapeNsis(displayVersion).replaceAll('$', () => '${U+24}');
  return Object.entries(catalogues).flatMap(([language, catalogue]) => [
    `LangString studio_buildVersion \${LANG_${language}} "${label}"`,
    ...Object.entries(catalogue).map(([key, value]) => {
      const escaped = escapeNsis(value)
        .replace(/\{(\w+)\}/g, (match, name) => variables[name] || match);
      const line = `LangString studio_${key} \${LANG_${language}} "${escaped}"`;
      return ['appBusy', 'checkFailed', 'closeFailed'].includes(key) ? line : `!ifndef BUILD_UNINSTALLER\n${line}\n!endif`;
    }),
  ]).join('\n') + '\n';
}
function writeInstallerMessages(displayVersion) {
  const root = path.resolve(__dirname, '../..');
  const target = path.join(root, 'desktop/build/installer-messages.nsh');
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, installerMessages({
    ENGLISH: require(path.join(root, 'frontend/src/locales/en.json')).installer,
    RUSSIAN: require(path.join(root, 'frontend/src/locales/ru.json')).installer,
  }, displayVersion));
}
module.exports = { installerMessages, writeInstallerMessages };
