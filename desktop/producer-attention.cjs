// Native taskbar overlay is durable attention; focusing only stops the flash (§10.11).
function registerProducerAttention({ window, icon, description, platform = process.platform }) {
  let waiting = false;
  const refresh = () => {
    if (platform !== 'win32' || window.isDestroyed()) return;
    window.setOverlayIcon(waiting ? icon : null, waiting ? description() : '');
  };
  const set = value => {
    const changed = value !== waiting;
    waiting = value;
    refresh();
    if (platform !== 'win32' || window.isDestroyed()) return;
    if (!waiting) window.flashFrame(false);
    else if (changed && !window.isFocused()) window.flashFrame(true);
  };
  window.on('focus', () => {
    if (platform === 'win32' && !window.isDestroyed()) window.flashFrame(false);
  });
  // A new project or renderer must publish its own state; never inherit an old badge.
  window.webContents.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) set(false);
  });
  window.webContents.on('render-process-gone', () => set(false));
  return { set, refresh };
}
module.exports = { registerProducerAttention };
