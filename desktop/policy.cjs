const ORIGIN = 'http://127.0.0.1:8765';
function navigationKind(value) {
  try {
    const url = new URL(value);
    if (url.origin === ORIGIN && !url.username && !url.password) return 'internal';
    if (['https:', 'http:', 'mailto:'].includes(url.protocol)) return 'external';
  } catch {}
  return 'blocked';
}
// Dev spec §2.7: allow copying from the Studio, while keeping other permissions denied.
function registerPermissions(contents) {
  const allowed = (sender, permission, url, details) =>
    sender === contents &&
    permission === 'clipboard-sanitized-write' &&
    details?.isMainFrame === true &&
    navigationKind(url) === 'internal' &&
    navigationKind(contents.getURL()) === 'internal';

  contents.session.setPermissionRequestHandler((sender, permission, callback, details) => {
    callback(allowed(sender, permission, details?.requestingUrl, details));
  });
  contents.session.setPermissionCheckHandler((sender, permission, origin, details) =>
    allowed(sender, permission, origin, details));
}
module.exports = { ORIGIN, navigationKind, registerPermissions };
