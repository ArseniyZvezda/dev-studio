// Native taskbar overlay is durable attention; focusing only stops the flash (§10.11).
function registerProducerAttention({ window, icon, description, platform = process.platform, appOwned = false }) {
  // 'closed' runs after the native window is destroyed (§5.3.5). Keep the
  // emitter while live so cleanup never reads BrowserWindow.webContents then.
  const contents = window.webContents;
  let waiting = false;
  let disposed = false;
  const refresh = () => {
    if (disposed || platform !== 'win32' || window.isDestroyed()) return;
    window.setOverlayIcon(waiting ? icon : null, waiting ? description() : '');
  };
  const set = value => {
    if (disposed) return;
    const changed = value !== waiting;
    waiting = value;
    refresh();
    if (platform !== 'win32' || window.isDestroyed()) return;
    if (!waiting) window.flashFrame(false);
    else if (changed && !window.isFocused()) window.flashFrame(true);
  };
  const focus = () => {
    if (platform === 'win32' && !window.isDestroyed()) window.flashFrame(false);
  };
  const navigate = (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) set(false);
  };
  const crashed = () => set(false);
  const dispose = () => {
    if (disposed) return;
    set(false);
    disposed = true;
    window.removeListener('focus', focus);
    window.removeListener('closed', dispose);
    contents.removeListener('did-start-navigation', navigate);
    contents.removeListener('render-process-gone', crashed);
  };
  window.on('focus', focus);
  window.once('closed', dispose);
  // Legacy renderer-owned state resets on replacement. App-owned attention is
  // independent of the shell renderer's reload/crash lifecycle (§5.3).
  if (!appOwned) {
    contents.on('did-start-navigation', navigate);
    contents.on('render-process-gone', crashed);
  }
  return { set, refresh, dispose };
}

// The app receiver supplies full snapshots; registry ownership decides which
// native window displays them, including inactive tabs and live transfers (§5.3).
function createProjectAttention({ registry, getWindowAttention }) {
  let waitingProjects = new Set();
  const bindingKey = project => {
    if (!project || !['root', 'project_id', 'epoch'].every(key =>
      typeof project[key] === 'string' && project[key].length > 0)) return null;
    return JSON.stringify([project.root, project.project_id, project.epoch]);
  };
  const refresh = () => {
    const snapshot = registry.snapshot();
    const tabs = new Map(snapshot.tabs.map(tab => [tab.tabId, tab]));
    for (const window of snapshot.windows) {
      const waiting = window.tabIds.some(id => waitingProjects.has(bindingKey(tabs.get(id)?.project)));
      getWindowAttention(window.windowId)?.set(waiting);
    }
  };
  const replace = values => {
    waitingProjects = new Set(values.filter(value => Array.isArray(value.waiting) && value.waiting.length > 0)
      .map(value => bindingKey(value.project)).filter(key => key !== null));
    refresh();
  };
  return { replace, refresh };
}
module.exports = { registerProducerAttention, createProjectAttention };
