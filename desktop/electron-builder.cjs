const path = require('node:path');
const { releaseInfo } = require('./scripts/release.cjs');
const { updateFeedOptions } = require('./update-feed.cjs');
const metadata = require('./package.json');
const fs = require('node:fs');
require('./scripts/installer-messages.cjs').writeInstallerMessages();
const stampFile = path.join(__dirname, 'build/runtime/python/Lib/site-packages/voxarium_studio/_build_stamp.json');
const stamp = fs.existsSync(stampFile) ? JSON.parse(fs.readFileSync(stampFile, 'utf8')) : { version: metadata.version };
const release = releaseInfo(stamp);
const feed = updateFeedOptions(process.env);
module.exports = {
  ...metadata.build,
  publish: feed ? [feed] : null,
  extraMetadata: { version: release.version },
  // GitHub normalizes spaces in asset names; keep the local file and latest.yml identical.
  artifactName: `Voxarium-Studio-Setup-${release.display}.\${ext}`,
  // Resolve from this file so a different shell directory cannot select another build.
  directories: { ...metadata.build.directories, app: path.resolve(__dirname) },
};
