const ORIGIN = 'http://127.0.0.1:8765';
function navigationKind(value) {
  try {
    const url = new URL(value);
    if (url.origin === ORIGIN && !url.username && !url.password) return 'internal';
    if (['https:', 'http:', 'mailto:'].includes(url.protocol)) return 'external';
  } catch {}
  return 'blocked';
}
const permissions = new WeakMap();
// One installation per session, never per window (dev spec §5.3.4).
// checkContents(contents) attests live registration AND its role URL; returns a
// shell/project record or true, and false/throws for stale or foreign contents.
// Alternatively authorize(event) receives the contents' native main frame.
// The existing HTTP Studio-origin restriction remains; a local data/file shell
// does not gain clipboard or other permissions merely by being registered.
// dispose() is app shutdown only. Legacy registerPermissions(contents) is additive.
function registerPermissions(target, controller) {
  const session = controller ? target : target.session;
  let entry = permissions.get(session);
  if (entry) {
    if (!controller) entry.legacy.add(target);
    return entry.api;
  }
  const legacy = new Set(controller ? [] : [target]);
  let disposed = false;
  const allowed = (sender, permission, url, details) => {
    try {
      if (disposed || !sender || sender.isDestroyed?.()
        || permission !== 'clipboard-sanitized-write' || details?.isMainFrame !== true
        || navigationKind(url) !== 'internal' || navigationKind(sender.getURL()) !== 'internal') return false;
      if (!controller) return legacy.has(sender);
      const owner = controller.checkContents
        ? controller.checkContents(sender)
        : controller.authorize({ sender, senderFrame: sender.mainFrame });
      return owner === true || !!(owner && ['shell', 'project'].includes(owner.role)
        && (!owner.contents || owner.contents === sender) && !owner.window?.isDestroyed?.());
    } catch { return false; }
  };
  session.setPermissionRequestHandler((sender, permission, callback, details) => {
    callback(allowed(sender, permission, details?.requestingUrl, details));
  });
  session.setPermissionCheckHandler((sender, permission, origin, details) =>
    allowed(sender, permission, origin, details));
  const api = { dispose() {
    if (disposed) return;
    disposed = true;
    legacy.clear();
    // Keep default-deny rather than restoring Electron's permissive defaults.
    session.setPermissionRequestHandler((_sender, _permission, callback) => callback(false));
    session.setPermissionCheckHandler(() => false);
    permissions.delete(session);
  } };
  entry = { legacy, api };
  permissions.set(session, entry);
  return api;
}
module.exports = { ORIGIN, navigationKind, registerPermissions };
