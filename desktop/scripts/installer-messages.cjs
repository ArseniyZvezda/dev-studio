// Keep native installer text in the same bilingual catalogues as the UI (§6.4).
const fs = require('node:fs');
const path = require('node:path');
function installerMessages(catalogues) {
  const variables = { version: '${VERSION}', installedVersion: '$studioInstalledVersion', directory: '$studioInstallDir' };
  return Object.entries(catalogues).flatMap(([language, catalogue]) =>
    Object.entries(catalogue).map(([key, value]) => {
      const escaped = value.replaceAll('$', () => '$$').replaceAll('"', '$\\"').replace(/\r?\n/g, '$\\r$\\n')
        .replace(/\{(\w+)\}/g, (match, name) => variables[name] || match);
      const line = `LangString studio_${key} \${LANG_${language}} "${escaped}"`;
      return ['appBusy', 'checkFailed', 'closeFailed'].includes(key) ? line : `!ifndef BUILD_UNINSTALLER\n${line}\n!endif`;
    })).join('\n') + '\n';
}
function writeInstallerMessages() {
  const root = path.resolve(__dirname, '../..');
  const target = path.join(root, 'desktop/build/installer-messages.nsh');
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, installerMessages({
    ENGLISH: require(path.join(root, 'frontend/src/locales/en.json')).installer,
    RUSSIAN: require(path.join(root, 'frontend/src/locales/ru.json')).installer,
  }));
}
module.exports = { installerMessages, writeInstallerMessages };
