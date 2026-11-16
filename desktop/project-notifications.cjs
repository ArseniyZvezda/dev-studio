'use strict';

// App-owned native surface over VS-61's transport (dev spec §5.3.6).
// No launcher, legacy selection, task-start API or renderer-supplied locator.
const path = require('node:path');
const { createHash } = require('node:crypto');
const { validateDesktopTarget } = require('./project-events.cjs');

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex').slice(0, 16);
function notificationTag(source, meta) {
  return digest([source.project.root, source.project.project_id, source.project.epoch, source.source_seq, meta.epoch, meta.seq]);
}
function pageFromTarget(target) {
  if (!validateDesktopTarget(target)) throw new TypeError('Invalid notification target');
  const url = new URL(target, 'http://127.0.0.1:8765');
  return { path: url.pathname, ...(url.search ? { query: Object.fromEntries(url.searchParams) } : {}) };
}

// The same EN/RU vocabulary as lib/automations.notificationText; authored text
// remains data. The full locale files are already shipped with the desktop.
function notificationText(notification, messages, locale) {
  const values = notification.values, copy = messages.automations.notification;
  const format = (value, params = {}) => {
    if (typeof value !== 'string') value = value[new Intl.PluralRules(locale).select(params.count)] ?? value.other;
    return value.replace(/\{(\w+)\}/g, (whole, key) => key in params
      ? key === 'count' ? new Intl.NumberFormat(locale).format(params[key]) : String(params[key]) : whole);
  };
  let title, body;
  switch (notification.trigger) {
    case 'task.status_changed': {
      const status = values.status ?? '';
      title = format(copy.statusTitle, { task: values.task ?? '',
        status: messages.status[status] ?? status.charAt(0).toUpperCase() + status.slice(1) });
      body = values.title ?? ''; break;
    }
    case 'task.created': title = format(copy.createdTitle, { task: values.task ?? '' }); body = values.title ?? ''; break;
    case 'path.changed': {
      const count = Number(values.count ?? '1') || 1;
      title = format(copy.pathTitle, { folder: values.folder ?? './' });
      body = count === 1 ? values.path ?? '' : format(copy.pathCount, { count }); break;
    }
    case 'run.finished':
      title = format(values.outcome === 'success' ? copy.runSucceeded
        : values.outcome === 'failed' ? copy.runFailed : copy.runCancelled, { pipeline: values.pipeline ?? '' });
      body = values.error ?? ''; break;
    default: title = notification.automation_name || copy.testTitle; body = copy.testMessage;
  }
  return { title: notification.title || title, body: notification.message || body };
}

function createProjectNotifications({ controller, Notification, ipcMain, preference,
  messages, locale, onError = () => {} }) {
  let disposed = false, enabled = false;
  const raised = new Map(), seen = new Set(), activations = new Map();
  try { enabled = preference?.read() === true; } catch {}
  const supported = () => !!Notification?.isSupported?.();
  const state = () => ({ supported: supported(), enabled });
  const error = (value, source, activation = false) => { if (!disposed) onError(value, source, { activation }); };
  async function available(callback) {
    for (;;) {
      try {
        await controller.registry.whenIdle();
        if (disposed) throw new Error('Notifications disposed');
        return await callback();
      }
      catch (error) { if (error.code !== 'surface_busy') throw error; }
    }
  }
  function activate(source) {
    const project = source?.project, root = project?.root;
    // Never guess from project_id, the visible window, CWD or legacy selection.
    if (typeof root !== 'string' || !root.trim() || (!path.posix.isAbsolute(root)
      && !path.win32.isAbsolute(root)) || /^[A-Za-z]:[^\\/]/.test(root)) {
      return Promise.reject(new TypeError('Missing notification project root'));
    }
    let page;
    try { page = source.target === null ? null : pageFromTarget(source.target); }
    catch (error) { return Promise.reject(error); }
    const key = JSON.stringify([root, source.target]);
    if (activations.has(key)) return activations.get(key);
    const pending = (async () => {
      await controller.sessionReady(); // Restore completes before target activation.
      // Canonicalize BEFORE creating/focusing a shell: missing root is not B.
      const canonical = await controller.resolveRoot(root);
      if (disposed) throw new Error('Notifications disposed');
      const tab = await available(async () => {
        // Re-resolve after any transfer/close gate: its source shell may have
        // vanished while the click waited, but the unique tab has a new owner.
        const native = controller.getActiveWindow() || await controller.ensureWindow();
        return controller.openProject(canonical, String(native.id), { create: false });
      });
      await controller.waitForProjectPage(tab.tabId);
      return available(() => controller.registry.withTabActivation(tab.tabId, async current => {
        controller.bridge.publishState();
        if (page) {
          const result = await controller.bridge.restorePage(current.tabId, page);
          if (result.status !== 'restored') throw new Error(`Notification page: ${result.status}`);
        }
        return controller.registry.getTab(current.tabId);
      }));
    })().finally(() => { if (activations.get(key) === pending) activations.delete(key); });
    activations.set(key, pending);
    return pending;
  }
  function notify(source, meta) {
    if (disposed || !enabled || !supported()) return;
    const tag = notificationTag(source, meta);
    if (seen.has(tag)) return;
    seen.add(tag);
    if (seen.size > 512) {
      const oldest = seen.values().next().value;
      seen.delete(oldest);
      raised.get(oldest)?.close(); raised.delete(oldest);
    }
    // Foreground B must not suppress A. Only the visible ACTIVE source tab can.
    const tab = controller.registry.snapshot().tabs.find(tab => tab.project.root === source.project.root
      && tab.project.project_id === source.project.project_id && tab.project.epoch === source.project.epoch);
    const owner = tab && controller.getWindow(tab.ownerWindowId);
    if (owner?.isFocused() && owner.isVisible() && !owner.isMinimized()
      && controller.registry.getWindow(tab.ownerWindowId)?.activeTabId === tab.tabId) return;
    try {
      const text = notificationText(source.notification, messages(), locale());
      const native = new Notification({ id: tag, title: `[${source.project.name}] ${text.title}`, body: text.body });
      raised.set(tag, native);
      let clicked = false;
      native.on('click', () => {
        if (disposed || clicked) return;
        clicked = true;
        activate(source).catch(value => error(value, source, true)).finally(() => { native.close(); raised.delete(tag); });
      });
      native.on('failed', (_event, value) => { raised.delete(tag); error(new Error(String(value)), source); });
      // A timed-out Windows banner may still be clicked in Action Center. Keep
      // its native object/locator alive (bounded above) until click/eviction/quit.
      native.show();
    } catch (value) { error(value, source); }
  }
  ipcMain.handle('studio:notifications', (event, action, value, ...extra) => {
    controller.bridge.authorize(event);
    if (disposed || extra.length || (action === 'state' ? value !== undefined
      : action !== 'set-enabled' || typeof value !== 'boolean')) throw new TypeError('Invalid notification preference');
    if (action === 'set-enabled') {
      // Persist first; a failed write must not report a durable preference.
      preference?.write(value);
      enabled = value;
    }
    return state();
  });
  return { notify, activate, state,
    dispose() {
      if (disposed) return;
      disposed = true;
      ipcMain.removeHandler('studio:notifications');
      for (const native of raised.values()) native.close();
      raised.clear(); seen.clear();
    },
  };
}

module.exports = { createProjectNotifications, notificationTag, notificationText, pageFromTarget };
