const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

// Shared by the desktop, Vite and build cache (dev spec §2.5, §2.7).
function releaseInfo(stamp, fallbackVersion = stamp.version) {
  const display = stamp.tag || stamp.short_commit || stamp.commit?.slice(0, 7) || stamp.version;
  if (typeof display !== 'string' || !display) {
    throw new Error(`Invalid Studio build label: ${display}`);
  }
  // Runtime equality uses the tag, or short commit when HEAD is untagged.
  const tag = stamp.tag?.match(/^v?(\d+)\.(\d+)(?:\.(\d+))?(-[0-9A-Za-z.-]+)?$/);
  const normalizedTag = tag ? `${tag[1]}.${tag[2]}.${tag[3] || '0'}${tag[4] || ''}` : null;
  const version = normalizedTag || display;
  // Electron/NSIS require SemVer for installer metadata; a commit hash is not SemVer.
  // This field never substitutes for the Studio version compared at startup.
  const installerVersion = normalizedTag || fallbackVersion;
  if (!installerVersion || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(installerVersion)) {
    throw new Error(`Invalid Studio installer version: ${installerVersion}`);
  }
  return { display, version, installerVersion };
}

function sourceReleaseInfo(root, fallbackVersion) {
  const git = (...args) => {
    try {
      return execFileSync('git', ['-C', root, ...args], {
        encoding: 'utf8', windowsHide: true, timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'],
      }).trim() || undefined;
    } catch { return undefined; }
  };
  // An exported source folder inside another repository must not inherit its identity.
  if (!fs.existsSync(path.join(root, '.git'))) return releaseInfo({ version: fallbackVersion });
  return releaseInfo({
    version: fallbackVersion,
    tag: git('describe', '--tags', '--exact-match', 'HEAD'),
    short_commit: git('rev-parse', '--short', 'HEAD'),
  });
}
module.exports = { releaseInfo, sourceReleaseInfo };

if (require.main === module) {
  const root = path.resolve(__dirname, '../..');
  process.stdout.write(JSON.stringify(sourceReleaseInfo(root, require('../package.json').version)));
}
