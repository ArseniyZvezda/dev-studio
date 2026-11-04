// Use the wheel's build identity, exactly as the UI does (dev spec §2.7).
function releaseInfo(stamp) {
  const display = stamp.tag || stamp.short_commit || stamp.commit?.slice(0, 7) || stamp.version;
  if (!display || !/^[a-zA-Z0-9][a-zA-Z0-9._+-]*$/.test(display)) {
    throw new Error(`Invalid Studio build label: ${display}`);
  }
  // Windows/Electron require SemVer; the filename retains the original UI label.
  const tag = stamp.tag?.match(/^v?(\d+)\.(\d+)(?:\.(\d+))?(-[0-9A-Za-z.-]+)?$/);
  const version = tag ? `${tag[1]}.${tag[2]}.${tag[3] || '0'}${tag[4] || ''}` : stamp.version;
  if (!version || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error(`Invalid Studio package version: ${version}`);
  }
  return { display, version };
}
module.exports = { releaseInfo };
