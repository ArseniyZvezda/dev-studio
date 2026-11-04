// Hiding the window keeps its backend and running tasks alive (dev spec §2.7).
function registerTray({ app, window, Tray, Menu, icon, productName, messages }) {
  const tray = new Tray(icon);
  tray.setToolTip(productName);
  const show = () => {
    if (window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  };
  const hide = () => {
    if (!window.isDestroyed() && !tray.isDestroyed()) window.hide();
  };
  const setLocale = locale => {
    const m = messages(locale);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: m.openWindow, click: show },
      { label: m.minimizeToTray, click: hide },
      { type: 'separator' },
      // Restore before closing so the existing task-stop confirmation is visible.
      { label: m.quit, click: () => { show(); if (!window.isDestroyed()) window.close(); } },
    ]));
  };
  setLocale();
  tray.on('click', show);
  tray.on('double-click', show);
  app.on('second-instance', show);
  const destroy = () => { if (!tray.isDestroyed()) tray.destroy(); };
  app.once('will-quit', destroy);
  window.once('closed', () => {
    destroy();
    app.removeListener('second-instance', show);
    app.removeListener('will-quit', destroy);
  });
  return { hide, show, setLocale };
}
module.exports = { registerTray };
