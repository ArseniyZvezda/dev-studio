// Main-only transport for the single backend-owned manifest (dev spec §5.3.6).
// No renderer/IPC binding or secondary filesystem snapshot.
const path = require('node:path');
const { ORIGIN } = require('./policy.cjs');

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fields = (value, required, optional = []) => object(value)
  && required.every(key => Object.hasOwn(value, key))
  && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
const index = (value, items) => items.length === 0 ? value === null
  : Number.isInteger(value) && value >= 0 && value < items.length;

function routePath(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const decoded = decodeURIComponent(value);
    return !/[\\?#%:\u0000-\u001f\u007f\ud800-\udfff]/u.test(decoded)
      && !decoded.includes('//') && !decoded.split('/').some(part => part === '.' || part === '..')
      && decoded.startsWith('/') && !['p', 'api', 'ws'].includes(decoded.split('/')[1]);
  } catch { return false; }
}

function projectRelativePath(value) {
  // Explorer selectors are literal filesystem paths, not URL components (§5.3.6).
  return typeof value === 'string' && value.length <= 4096
    && !/[\\?:\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)
    && !value.startsWith('/') && !value.includes('//')
    && !value.split('/').some(part => part === '.' || part === '..');
}

function validManifest(value) {
  const roots = new Set();
  let tabs = 0;
  return fields(value, ['version', 'windows', 'active_window']) && value.version === 1
    && Array.isArray(value.windows) && value.windows.length <= 32 && index(value.active_window, value.windows)
    && value.windows.every(window => fields(window, ['tabs', 'active_tab'], ['geometry'])
      && Array.isArray(window.tabs) && (tabs += window.tabs.length) <= 256 && index(window.active_tab, window.tabs)
      && (!Object.hasOwn(window, 'geometry') || validGeometry(window.geometry))
      && window.tabs.every(tab => fields(tab, ['root', 'page'])
        && typeof tab.root === 'string' && !!tab.root.trim() && tab.root.length <= 32768
        && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(tab.root)
        && (path.posix.isAbsolute(tab.root) || path.win32.isAbsolute(tab.root))
        && !roots.has(tab.root) && !!roots.add(tab.root)
        && fields(tab.page, ['path'], ['query'])
        && routePath(tab.page.path)
        && (!Object.hasOwn(tab.page, 'query') || (fields(tab.page.query, [], ['path', 'task', 'chat', 'assign'])
          && Object.entries(tab.page.query).every(([key, item]) => key === 'path' ? projectRelativePath(item)
            : typeof item === 'string' && item.length <= 256 && /^[A-Za-z0-9_.:-]+$/.test(item))))));
}

function validGeometry(value) {
  return fields(value, ['x', 'y', 'width', 'height'], ['maximized'])
    && ['x', 'y', 'width', 'height'].every(key => Number.isSafeInteger(value[key]))
    && Math.abs(value.x) <= 1_000_000 && Math.abs(value.y) <= 1_000_000
    && value.width >= 100 && value.width <= 32768 && value.height >= 100 && value.height <= 32768
    && (!Object.hasOwn(value, 'maximized') || typeof value.maximized === 'boolean');
}

function failure(message, status, code, cause) {
  return Object.assign(new Error(message, cause ? { cause } : undefined), { status, code });
}

function createSessionStateAdapter({ fetch = globalThis.fetch, token } = {}) {
  let pending = Promise.resolve();
  async function request(method, body) {
    const secret = typeof token === 'function' ? await token() : token;
    const headers = { Accept: 'application/json' };
    if (secret) headers.Authorization = `Bearer ${secret}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(`${ORIGIN}/api/desktop/session`, {
      method, headers, redirect: 'error', cache: 'no-store',
      ...(body === undefined ? {} : { body }),
    });
    let result;
    try { result = await response.json(); }
    catch (cause) {
      throw failure(`Desktop session HTTP ${response.status}: invalid JSON`, response.status,
        response.ok ? 'desktop_session_invalid_response' : 'desktop_session_http_error', cause);
    }
    if (!response.ok) {
      const code = typeof result?.detail?.code === 'string' ? result.detail.code : 'desktop_session_http_error';
      throw failure(`Desktop session HTTP ${response.status}: ${code}`, response.status, code);
    }
    if (!validManifest(result)) {
      throw failure('Invalid desktop session manifest response', response.status, 'desktop_session_invalid_response');
    }
    return result;
  }
  return {
    load() { return request('GET'); },
    save(manifest) {
      // Capture before any await/queue: callers may immediately mutate their state.
      let body;
      try {
        if (!validManifest(manifest)) throw new TypeError('Invalid desktop session manifest');
        body = JSON.stringify(manifest);
        if (body === undefined) throw new TypeError('Manifest is not JSON');
      } catch (error) { return Promise.reject(error); }
      const operation = pending.then(() => request('PUT', body));
      // A failed write rejects its own caller, without poisoning later writes.
      pending = operation.catch(() => {});
      return operation;
    },
  };
}

// Geometry is only a hint. Use current DIP work areas, never a vanished monitor
// (dev spec §5.3.6). Keep the whole restored window reachable on one display.
function restoreGeometry(value, displays = []) {
  if (!validGeometry(value)) return undefined;
  const areas = displays.map(display => display.workArea).filter(area => area
    && ['x', 'y', 'width', 'height'].every(key => Number.isSafeInteger(area[key]))
    && area.width > 0 && area.height > 0);
  if (!areas.length) return undefined;
  const overlap = area => Math.max(0, Math.min(value.x + value.width, area.x + area.width) - Math.max(value.x, area.x))
    * Math.max(0, Math.min(value.y + value.height, area.y + area.height) - Math.max(value.y, area.y));
  const area = areas.reduce((best, item) => overlap(item) > overlap(best) ? item : best);
  const width = Math.min(value.width, area.width), height = Math.min(value.height, area.height);
  return { x: Math.max(area.x, Math.min(value.x, area.x + area.width - width)),
    y: Math.max(area.y, Math.min(value.y, area.y + area.height - height)), width, height,
    maximized: value.maximized === true };
}

// Main-owned orchestration over the SAME registry/open path as interactive open.
// Writes are suspended during restore and frozen before app teardown (§5.3.6).
function createSessionController({ adapter, registry, bridge, createWindow, getWindow,
  getActiveWindow, getDisplays = () => [], reportError = () => {}, pageReadyTimeout = 30000 }) {
  let initialized = false, restoring = false, frozen = false, blocked = false, disposed = false;
  let initialization, scheduled = false, pending = Promise.resolve(), lastOperation = pending, lastBody;
  const pageWaiters = new Set();
  const errors = (error, details = {}) => { try { reportError(error, details); } catch {} };
  function snapshot(closing = false) {
    // The close barrier already validated all identities and excludes transfers.
    const state = registry.committedSnapshot({ closeBarrier: closing });
    if (!state) return null;
    const pages = new Map(bridge.snapshotPages().map(item => [item.tabId, item.page]));
    const tabs = new Map(state.tabs.map(tab => [tab.tabId, tab]));
    const live = state.windows.filter(record => !getWindow(record.windowId)?.isDestroyed());
    const active = getActiveWindow();
    const windows = live.map(record => {
      const native = getWindow(record.windowId);
      const windowTabs = record.tabIds.map(id => tabs.get(id)).filter(Boolean);
      const geometry = native?.getNormalBounds?.();
      return { tabs: windowTabs.map(tab => ({ root: tab.project.root, page: pages.get(tab.tabId) ?? { path: '/' } })),
        active_tab: windowTabs.length ? Math.max(0, windowTabs.findIndex(tab => tab.tabId === record.activeTabId)) : null,
        ...(validGeometry(geometry) ? { geometry: { ...geometry, maximized: native.isMaximized() } } : {}) };
    });
    return { version: 1, windows, active_window: windows.length
      ? Math.max(0, live.findIndex(record => getWindow(record.windowId) === active)) : null };
  }
  function write(value) {
    if (!value) return pending;
    const body = JSON.stringify(value);
    if (body === lastBody) return lastOperation;
    lastBody = body;
    const operation = pending.then(() => adapter.save(JSON.parse(body)));
    lastOperation = operation;
    pending = operation.catch(error => { if (lastBody === body) lastBody = undefined; errors(error); });
    return operation;
  }
  function wakePages() {
    const pages = new Set(bridge.snapshotPages().map(item => item.tabId));
    for (const waiter of pageWaiters) {
      const tab = registry.getTab(waiter.tabId);
      if (!tab || tab.state === 'crashed') waiter.finish(failure('Project page is unavailable', undefined, 'sessionPageUnavailable'));
      else if (pages.has(waiter.tabId)) waiter.finish();
    }
  }
  function changed() {
    wakePages();
    if (!initialized || restoring || frozen || blocked || disposed || scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      if (!restoring && !frozen && !blocked && !disposed) void write(snapshot()).catch(() => {});
    });
  }
  function waitForPage(tabId) {
    if (bridge.snapshotPages().some(item => item.tabId === tabId)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiter = { tabId, finish(error) {
        pageWaiters.delete(waiter); clearTimeout(timer); error ? reject(error) : resolve();
      } };
      const timer = setTimeout(() => waiter.finish(failure('Project page bootstrap timed out', undefined, 'sessionPageTimeout')), pageReadyTimeout);
      pageWaiters.add(waiter);
    });
  }
  async function initialize(seed) {
    if (initialization) return initialization;
    restoring = true;
    initialization = (async () => {
      let manifest;
      try {
        manifest = await adapter.load();
        if (!validManifest(manifest)) throw new Error('Invalid desktop session manifest');
      }
      catch (error) {
        if (error.code === 'desktop_session_missing') {
          const native = await createWindow();
          await seed?.(native);
          return;
        }
        // Do not reinterpret corruption/auth/unsupported versions as first run,
        // seed another project, or silently overwrite the damaged source.
        blocked = true;
        const native = await createWindow();
        errors(error, { windowId: String(native.id) });
        return;
      }
      const restored = [];
      for (const saved of manifest.windows) {
        const native = await createWindow({ geometry: restoreGeometry(saved.geometry, getDisplays()) });
        const windowId = String(native.id), ids = [];
        restored.push({ native, ids });
        for (const entry of saved.tabs) {
          const index = ids.length;
          ids.push(null);
          try {
            const tab = await registry.openProject(entry.root, { windowId, create: false });
            ids[index] = tab.tabId;
            // The snapshot fallback ({path:'/'}) records "no settled page yet",
            // not a target: '/' is the redirect to the default section, so the
            // fresh renderer lands there on its own and a restore could only
            // fail as unavailable. Restore only real saved pages.
            if (entry.page.path !== '/' || (entry.page.query && Object.keys(entry.page.query).length)) {
              // A real SPA listener/state report, not loadURL completion, makes
              // restore delivery safe across async i18n/router/plugin bootstrap.
              bridge.publishState();
              await waitForPage(tab.tabId);
              const result = await bridge.restorePage(tab.tabId, entry.page);
              if (result.status !== 'restored') throw failure(`Project page restore: ${result.status}`, undefined, 'sessionPageFailed');
            }
          } catch (error) {
            errors(error, { windowId, root: entry.root });
          }
        }
        // Concurrent opens use the same root reservations; restore the saved
        // order for records owned by this window without moving/reloading any
        // incumbent that won an open in another window.
        const ordered = [...new Set(ids.filter(id => id && registry.getTab(id)?.ownerWindowId === windowId))];
        try {
          for (let index = 0; index < ordered.length; index++) {
            const tab = registry.getTab(ordered[index]), window = registry.getWindow(windowId);
            registry.reorderTab(tab.tabId, { windowId, windowRevision: window.revision,
              tabRevision: tab.revision, index });
          }
          const active = ids[saved.active_tab];
          if (active && registry.getTab(active)) registry.activateTab(active);
        } catch (error) {
          errors(error, { windowId });
        }
      }
      // An empty manifest means intentionally closed UI, not a legacy seed.
      const preferred = restored[manifest.active_window]?.native;
      const active = (preferred && !preferred.isDestroyed() ? preferred : null)
        ?? restored.find(item => !item.native.isDestroyed())?.native ?? await createWindow();
      active.show(); active.focus();
      bridge.publishState();
    })().finally(() => { restoring = false; initialized = true; changed(); });
    return initialization;
  }
  return {
    initialize, changed, snapshot,
    async ready() { await initialization; },
    async flush() { if (initialized && !restoring && !frozen && !blocked && !disposed) await write(snapshot()); await pending; },
    async freeze() {
      await initialization;
      frozen = true;
      try {
        if (!blocked && !disposed) {
          const value = snapshot(true);
          if (!value) throw new Error('Desktop session topology is not committed');
          await write(value);
        }
        await pending;
      }
      catch (error) { frozen = false; throw error; }
    },
    resume() { frozen = false; changed(); },
    dispose() { disposed = true; frozen = true; for (const waiter of [...pageWaiters]) waiter.finish(new Error('Desktop session disposed')); },
  };
}

module.exports = { createSessionStateAdapter, createSessionController, restoreGeometry };
