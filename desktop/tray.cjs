// The tray belongs to the resident app, including its zero-window state (§5.3.5).
function registerTray({ app, activate, hideWindow, Tray, Menu, icon, productName, messages, onError = () => {} }) {
  const tray = new Tray(icon);
  tray.setToolTip(productName);
  let disposed = false, activation;
  const show = () => {
    if (disposed || tray.isDestroyed()) return Promise.resolve();
    if (activation) return activation;
    activation = Promise.resolve().then(() => {
      if (!disposed) return activate();
    }).catch(onError).finally(() => { activation = undefined; });
    return activation;
  };
  const hide = () => {
    if (!disposed && !tray.isDestroyed()) hideWindow();
  };
  const setLocale = locale => {
    if (disposed) return;
    const m = messages(locale);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: m.openWindow, click: show },
      { label: m.minimizeToTray, click: hide },
      { type: 'separator' },
      // Explicit Quit is not the UI-only native close path. The app owns its
      // shutdown policy and owned-backend cleanup, even with no windows.
      { label: m.quit, click: () => app.quit() },
    ]));
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    app.removeListener('second-instance', show);
    app.removeListener('will-quit', dispose);
    tray.removeListener('click', show);
    tray.removeListener('double-click', show);
    if (!tray.isDestroyed()) tray.destroy();
  };
  setLocale();
  tray.on('click', show);
  tray.on('double-click', show);
  app.on('second-instance', show);
  app.once('will-quit', dispose);
  return { hide, show, setLocale, dispose };
}
module.exports = { registerTray };
