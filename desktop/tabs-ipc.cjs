'use strict';

const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { randomUUID } = require('node:crypto');
const { ORIGIN } = require('./policy.cjs');

const TABS_CHANNELS = Object.freeze({
  bootstrap: 'studio:tabs-bootstrap',
  open: 'studio:tabs-open',
  activate: 'studio:tabs-activate',
  transferBegin: 'studio:tabs-transfer-begin',
  transfer: 'studio:tabs-transfer',
  detach: 'studio:tabs-detach',
  reorder: 'studio:tabs-reorder',
  dragBegin: 'studio:tabs-drag-begin',
  dragCancel: 'studio:tabs-drag-cancel',
  dragGeometry: 'studio:tabs-drag-geometry',
  state: 'studio:tabs-state',
  prepareClose: 'studio:tabs-prepare-close',
  close: 'studio:tabs-close',
  closeRequest: 'studio:tabs-close-request',
  closeAck: 'studio:tabs-close-ack',
  reportPage: 'studio:tabs-report-page',
  restorePage: 'studio:tabs-restore-page',
  pageRestoreAck: 'studio:tabs-page-restore-ack',
  recentProjects: 'studio:tabs-recent-projects',
  inspectPath: 'studio:tabs-inspect-path',
  browseDirectory: 'studio:tabs-browse-directory',
  pickerStatus: 'studio:tabs-picker-status',
  pickDirectory: 'studio:tabs-pick-directory',
  forgetRecent: 'studio:tabs-forget-recent',
  setMenuOpen: 'studio:tabs-set-menu-open',
  stateChanged: 'studio:tabs-state-changed',
});

function documentURL(address) {
  const url = new URL(address);
  if (url.username || url.password) throw new Error('Credentials are not allowed');
  // The shell and legacy browser SPA share a pathname. Its checked query is
  // part of the document identity, never an ignorable navigation selector.
  url.hash = '';
  return url.href;
}

// Global picker/recent services only (§5.3.3). URLs are constructed here, never
// accepted from a renderer; no legacy open/selection or project REST binding.
function createShellServices({ fetch: request = globalThis.fetch, token } = {}) {
  async function call(endpoint, { method = 'GET', body, waitForPicker = false } = {}) {
    const secret = typeof token === 'function' ? token() : token;
    const response = await request(`${ORIGIN}/api/${endpoint}`, {
      method, redirect: 'error', cache: 'no-store',
      headers: { ...(secret ? { Authorization: `Bearer ${secret}` } : {}),
        ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
      // /fs/pick blocks for the human's answer and has its own backend timeout.
      ...(!waitForPicker ? { signal: AbortSignal.timeout(10000) } : {}),
    });
    if (!response.ok) throw new Error(`Shell service failed (${response.status})`);
    return response.json();
  }
  return Object.freeze({
    recentProjects: () => call('project/recent'),
    inspectPath: root => call(`fs/inspect?path=${encodeURIComponent(root)}`),
    browseDirectory: root => call(`fs/browse?path=${encodeURIComponent(root)}`),
    pickerStatus: () => call('fs/picker'),
    pickDirectory: initial => call('fs/pick', { method: 'POST',
      body: { start_path: initial ?? null }, waitForPicker: true }),
    forgetRecent: async root => { await call(`project/recent?path=${encodeURIComponent(root)}`, { method: 'DELETE' }); },
  });
}

function projectURL(address, binding) {
  try {
    const url = new URL(address);
    const prefix = `/p/${binding.project_id}`;
    return url.origin === ORIGIN && !url.username && !url.password
      && (url.pathname === prefix || url.pathname.startsWith(`${prefix}/`));
  } catch { return false; }
}

const sameBinding = (a, b) => a?.root === b.root && a?.project_id === b.project_id && a?.epoch === b.epoch;
const live = object => !!object && typeof object.isDestroyed === 'function' && !object.isDestroyed();
const revision = value => Number.isSafeInteger(value) && value >= 0;
const identifier = value => typeof value === 'string' && value.length > 0 && value.length <= 128;

function shape(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))
    || required.some(key => !Object.hasOwn(value, key))
    || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) {
    throw new TypeError('Invalid tabs request');
  }
}

const pageStatuses = new Set(['restored', 'invalid', 'unavailable', 'failed', 'stale']);

// Keep the page/path wire schema compatible with session-state.cjs (§5.3.6).
// Decode route URLs once and reject remaining escapes (including double-encoded
// traversal). Explorer selectors are literal filesystem paths, never URL-decoded.
// Neither kind is turned into an arbitrary native loadURL.
function pagePath(value, route) {
  if (typeof value !== 'string' || value.length > (route ? 2048 : 4096)) return false;
  if (!route) {
    return !/[\\?:\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)
      && !value.startsWith('/') && !value.includes('//')
      && !value.split('/').some(part => part === '.' || part === '..');
  }
  try {
    const decoded = decodeURIComponent(value);
    return !/[\\?#%:\u0000-\u001f\u007f\ud800-\udfff]/u.test(decoded)
      && !decoded.includes('//') && !decoded.split('/').some(part => part === '.' || part === '..')
      && decoded.startsWith('/') && !['p', 'api', 'ws'].includes(decoded.split('/')[1]);
  } catch { return false; }
}

function pageShape(value, required, optional = []) {
  shape(value, required, optional);
  // Main-only callers also pass this boundary: no accessors, toJSON, symbols or
  // hidden fields may smuggle drafts, attachments or credentials into a snapshot.
  if (Reflect.ownKeys(value).some(key => typeof key !== 'string'
    || (!required.includes(key) && !optional.includes(key))
    || !Object.getOwnPropertyDescriptor(value, key).enumerable
    || !Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), 'value'))) {
    throw new TypeError('Invalid page fields');
  }
}

function copyPage(value) {
  pageShape(value, ['path'], ['query']);
  if (!pagePath(value.path, true)) throw new TypeError('Invalid page path');
  const page = { path: value.path };
  if (Object.hasOwn(value, 'query')) {
    pageShape(value.query, [], ['path', 'task', 'chat', 'assign']);
    page.query = {};
    for (const [key, item] of Object.entries(value.query)) {
      if (!(key === 'path' ? pagePath(item, false)
        : typeof item === 'string' && item.length <= 256 && /^[A-Za-z0-9_.:-]+$/.test(item))) {
        throw new TypeError('Invalid page selector');
      }
      page.query[key] = item;
    }
  }
  return page;
}

// Public projections are deliberately explicit: registry/native implementation
// details and backend credentials must never become bootstrap fields (§5.3.4).
function publicBinding(project) {
  return { root: project.root, project_id: project.project_id, epoch: project.epoch };
}
function publicTab(tab, taskCounts = null) {
  // Copy only the two read-only summary fields; never expose the cache itself.
  const task_counts = taskCounts && Number.isSafeInteger(taskCounts.in_progress) && taskCounts.in_progress >= 0
    && Number.isSafeInteger(taskCounts.needs_review) && taskCounts.needs_review >= 0
    ? { in_progress: taskCounts.in_progress, needs_review: taskCounts.needs_review } : null;
  return { tabId: tab.tabId, project: publicBinding(tab.project), ownerWindowId: tab.ownerWindowId,
    revision: tab.revision, state: tab.state, transferGeneration: tab.transferGeneration, task_counts };
}
function publicWindow(window) {
  return { windowId: window.windowId, revision: window.revision, tabIds: [...window.tabIds],
    activeTabId: window.activeTabId, insets: { top: window.insets.top, right: window.insets.right,
      bottom: window.insets.bottom, left: window.insets.left } };
}

/**
 * App-level IPC boundary (dev spec §5.3.4, VS-47). Register a shell only AFTER
 * registry.registerWindow, and a project from configureProjectView BEFORE its
 * registry tab exists. Never use renderer-provided ids as native identities.
 *
 * Wire protocol: bootstrap/state take no payload. open takes
 * {windowId, windowRevision, root, create?}; activate adds {tabId, tabRevision}
 * instead of root/create. Both are shell-only. All ids/revisions are assertions,
 * not capabilities. State/bootstrap expose only this shell's tabs or this
 * project's immutable binding/tab and current owner/revision.
 * transfer-begin and detach take {windowId, windowRevision, tabId, tabRevision};
 * reorder adds {index}; transfer adds {token, targetWindowId,
 * targetWindowRevision, index?}. These operations are source-shell-only. Core
 * owns capability expiry/consumption and the same-view transaction; the IPC
 * boundary revalidates the original shell/frame and ownership before commit.
 *
 * prepare-close takes {windowId, windowRevision, target:'window'|'tab',
 * tabId?, tabRevision?}; project callers may target only their own tab. This is
 * ONLY a non-disposing close-barrier seam: no surface disposal, quit or backend stop.
 * An optional prepareClose({authorization, request}) callback must explicitly
 * return {prepared:true}; absent/other results fail closed. Identity, ownership
 * and revisions are rechecked after awaiting it. 'prepared' is not permission
 * to destroy a surface: the close controller below owns serialization,
 * dirty editor ACKs, cancellation and the final registry transaction (§5.3.5).
 * close is a shell tab-close request; native windows use requestCloseWindow.
 * close-request/close-ack carry the fixed rendererCloseBarrier identity tuple
 * plus prepare|validate phase. Release uses the same request tuple without ACK.
 * report-page is project-only {tabId, tabRevision, transferGeneration, page};
 * main-only restorePage sends {requestId, tabId, tabRevision,
 * transferGeneration, page}. A project ACK echoes the tuple without page plus
 * status: restored|invalid|unavailable|failed|stale. Neither page snapshots nor
 * restore requests are exposed in publicTab or shell state.
 *
 * Shell comparison is exact including query (hash navigation only is ignored),
 * never an origin-wide or pathname-prefix allowlist. No shell auto-discovery.
 */
function registerTabsIpc({ ipcMain, registry,
  shellURL = pathToFileURL(path.join(__dirname, 'shell.html')).href, prepareClose,
  shellServices, setMenuOpen, tabDrag, beforePublish = () => {}, getTaskCounts = () => null,
  onWindowEmpty = () => {}, onPageChanged = () => {},
  closeTimeout = 30000, pageRestoreTimeout = 30000 } = {}) {
  if (!ipcMain || !registry) throw new TypeError('ipcMain and registry are required');
  if (prepareClose !== undefined && typeof prepareClose !== 'function') throw new TypeError('Invalid close callback');
  if (typeof onPageChanged !== 'function') throw new TypeError('Invalid page callback');
  if (typeof getTaskCounts !== 'function') throw new TypeError('Invalid task counts callback');
  if (typeof onWindowEmpty !== 'function' || !Number.isSafeInteger(closeTimeout) || closeTimeout <= 0) {
    throw new TypeError('Invalid close options');
  }
  if (!Number.isSafeInteger(pageRestoreTimeout) || pageRestoreTimeout <= 0) throw new TypeError('Invalid page restore timeout');
  const shellDocument = documentURL(shellURL);
  const windows = new Map();
  const identities = new Map();
  const handlers = [];
  const closeAcks = new Map(), closing = new Map();
  const pages = new Map(), pageRestores = new Map();
  let disposed = false;
  let transferPublications = 0;

  function unregister(record) {
    if (identities.get(record.contents) !== record) return;
    identities.delete(record.contents);
    for (const pending of closeAcks.values()) if (pending.record === record) pending.finish(false);
    for (const pending of pageRestores.values()) if (pending.record === record) pending.finish('unavailable');
    for (const [event, listener] of record.stateListeners || []) record.contents.removeListener(event, listener);
    record.contents.removeListener('destroyed', record.release);
    if (record.role === 'shell') {
      registry.revokeTransferSender?.(record.contents);
      if (windows.get(record.windowId) === record) windows.delete(record.windowId);
      record.window.removeListener('closed', record.release);
    }
  }

  function authorize(event) {
    const contents = event?.sender;
    const record = identities.get(contents);
    if (disposed || !record || !live(contents) || !event.senderFrame
      || event.senderFrame !== contents.mainFrame
      || event.senderFrame.isDestroyed?.()) throw new Error('Untrusted tabs sender');
    const address = contents.getURL();
    let tab, owner;
    if (record.role === 'shell') {
      let valid = false;
      try { valid = documentURL(address) === shellDocument && documentURL(event.senderFrame.url) === shellDocument; } catch {}
      if (!valid) throw new Error('Untrusted shell document');
      owner = record;
    } else {
      if (!projectURL(address, record.binding) || !projectURL(event.senderFrame.url, record.binding)) {
        throw new Error('Untrusted project document');
      }
      // The configure callback precedes tab insertion. Resolve once lazily, then
      // pin that tab identity so a replacement tab cannot resurrect old rights.
      const matches = registry.snapshot().tabs.filter(item => sameBinding(item.project, record.binding));
      if (matches.length !== 1 || (record.tabId && matches[0].tabId !== record.tabId)) {
        throw new Error('Project tab is no longer available');
      }
      tab = registry.getTab(matches[0].tabId);
      if (!tab || !sameBinding(tab.project, record.binding) || tab.state === 'closing') {
        throw new Error('Project binding is no longer available');
      }
      record.tabId = tab.tabId;
      owner = windows.get(tab.ownerWindowId);
    }
    if (!owner || !live(owner.window) || !live(owner.contents)
      || owner.window.webContents !== owner.contents || !registry.getWindow(owner.windowId)) {
      throw new Error('Owner window is no longer available');
    }
    return { role: record.role, window: owner.window, windowId: owner.windowId, contents, ...(tab ? { tab } : {}) };
  }

  function registerWindow(window) {
    const contents = window?.webContents, windowId = String(window?.id);
    if (disposed || !live(window) || !live(contents) || !registry.getWindow(windowId)) {
      throw new Error('Register the live registry window first');
    }
    if (windows.has(windowId) || identities.has(contents)) throw new Error('Window is already registered');
    const record = { role: 'shell', window, windowId, contents };
    record.release = () => unregister(record);
    windows.set(windowId, record); identities.set(contents, record);
    contents.once('destroyed', record.release); window.once('closed', record.release);
    return record.release;
  }

  function registerProjectView(view, binding) {
    const contents = view?.webContents;
    if (disposed || !live(contents) || !binding || typeof binding.root !== 'string' || !binding.root.trim()
      || typeof binding.project_id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(binding.project_id)
      || typeof binding.epoch !== 'string' || !binding.epoch) throw new Error('Invalid project view binding');
    if (identities.has(contents) || [...identities.values()].some(item => item.role === 'project'
      && sameBinding(item.binding, binding))) throw new Error('Project view is already registered');
    const record = { role: 'project', contents, binding: Object.freeze(publicBinding(binding)), tabId: null };
    record.release = () => { unregister(record); queueMicrotask(publishState); };
    // Registry's native listeners are installed after configureProjectView.
    // Publish in a microtask so crash/removal revisions are already committed.
    record.stateListeners = ['did-start-loading', 'did-stop-loading', 'did-finish-load', 'render-process-gone']
      .map(event => { const listener = () => queueMicrotask(publishState); contents.on(event, listener); return [event, listener]; });
    identities.set(contents, record); contents.once('destroyed', record.release);
    return record.release;
  }

  function stateFor(authorization) {
    const window = registry.getWindow(authorization.windowId);
    if (authorization.role === 'project') {
      return { role: 'project', windowId: window.windowId, windowRevision: window.revision,
        binding: publicBinding(authorization.tab.project), tab: publicTab(authorization.tab) };
    }
    const error = windows.get(authorization.windowId)?.error;
    return { role: 'shell', window: publicWindow(window), ...(error ? { error } : {}),
      tabs: window.tabIds.map(id => registry.getTab(id)).filter(tab => tab?.ownerWindowId === window.windowId)
        .map(tab => publicTab(tab, getTaskCounts(tab))) };
  }

  function authorizedContents() {
    const result = [];
    for (const contents of identities.keys()) {
      try { result.push(authorize({ sender: contents, senderFrame: contents.mainFrame })); } catch {}
    }
    return result;
  }

  function publishState() {
    // Native load/crash events and outside callers can publish during an await.
    // Never broadcast tentative transfer ownership/order (dev spec §5.3.4).
    if (disposed || transferPublications) return;
    for (const pending of pageRestores.values()) {
      try { currentPageRestore(pending); } catch { pending.finish('stale'); }
    }
    beforePublish();
    for (const authorization of authorizedContents()) {
      // Closing one renderer during delivery must not interrupt other windows.
      try { authorization.contents.send(TABS_CHANNELS.stateChanged, stateFor(authorization)); } catch {}
    }
  }

  function ownWindow(authorization, request) {
    if (!identifier(request.windowId) || !revision(request.windowRevision)
      || request.windowId !== authorization.windowId) throw new Error('Foreign window request');
    const window = registry.getWindow(authorization.windowId);
    if (!window || window.revision !== request.windowRevision) throw new Error('Stale window revision');
    return window;
  }

  function ownTab(authorization, request) {
    if (!identifier(request.tabId) || !revision(request.tabRevision)) throw new TypeError('Invalid tab request');
    const tab = registry.getTab(request.tabId);
    if (!tab || tab.ownerWindowId !== authorization.windowId
      || (authorization.role === 'project' && tab.tabId !== authorization.tab.tabId)) throw new Error('Foreign tab request');
    if (tab.revision !== request.tabRevision) throw new Error('Stale tab revision');
    return tab;
  }

  function ownPage(authorization, request) {
    if (authorization.role !== 'project') throw new Error('Project page sender required');
    const tab = ownTab(authorization, request);
    if (!revision(request.transferGeneration) || tab.transferGeneration !== request.transferGeneration) {
      throw new Error('Stale page generation');
    }
    if (tab.state !== 'ready') throw new Error('Project page is unavailable');
    return tab;
  }

  function currentPageRestore(pending) {
    const { record, frame, tab, request } = pending;
    if (identities.get(record.contents) !== record || record.contents.mainFrame !== frame) {
      throw new Error('Stale page renderer');
    }
    const current = ownPage(authorize({ sender: record.contents, senderFrame: frame }), request);
    if (current.ownerWindowId !== tab.ownerWindowId || !sameBinding(current.project, tab.project)) {
      throw new Error('Stale page identity');
    }
  }

  function snapshotPages() {
    const result = [];
    for (const [tabId, saved] of pages) {
      const tab = registry.getTab(tabId);
      if (!tab || !sameBinding(tab.project, saved.binding)) { pages.delete(tabId); continue; }
      result.push({ tabId, page: copyPage(saved.page) });
    }
    return result;
  }

  // Persistence observes only committed, validated snapshots (§5.3.6), and
  // cannot turn an accepted renderer operation into an IPC failure.
  function pageChanged() {
    try { onPageChanged(); } catch {}
  }

  // Main-only seam for manifest/notification orchestration (§5.3.6). Transfer
  // never calls it: a moved renderer keeps its live router/editor unchanged.
  function restorePage(tabId, value) {
    let page;
    try { page = copyPage(value); } catch { return Promise.resolve({ status: 'invalid' }); }
    const tab = identifier(tabId) ? registry.getTab(tabId) : null;
    const record = tab && [...identities.values()].find(item => item.role === 'project' && sameBinding(item.binding, tab.project));
    if (disposed || !tab || !record || tab.state !== 'ready') return Promise.resolve({ status: 'unavailable' });
    const frame = record.contents.mainFrame;
    const request = Object.freeze({ requestId: randomUUID(), tabId, tabRevision: tab.revision,
      transferGeneration: tab.transferGeneration });
    const target = { record, frame, tab: publicTab(tab), request };
    try { currentPageRestore(target); } catch { return Promise.resolve({ status: 'unavailable' }); }
    // An older request can no longer overwrite a newer requested page.
    for (const pending of pageRestores.values()) if (pending.request.tabId === tabId) pending.finish('stale');
    const previousPage = pages.get(tabId);
    return new Promise(resolve => {
      const pending = { ...target, finish(status) {
        if (pageRestores.get(request.requestId) !== pending) return;
        pageRestores.delete(request.requestId); clearTimeout(timer);
        // A report during restore is the renderer's actual selection, including
        // consumed selectors. Do not overwrite it with the original request.
        if (status === 'restored' && pages.get(tabId) === previousPage) {
          pages.set(tabId, { binding: publicBinding(tab.project), page: copyPage(page) });
          pageChanged();
        }
        resolve({ status });
      } };
      const timer = setTimeout(() => {
        try { currentPageRestore(pending); pending.finish('failed'); } catch { pending.finish('stale'); }
      }, pageRestoreTimeout);
      pageRestores.set(request.requestId, pending);
      try { record.contents.send(TABS_CHANNELS.restorePage, { ...request, page: copyPage(page) }); }
      catch { pending.finish('failed'); }
    });
  }

  function sourceShell(event, request, authorization = authorize(event)) {
    if (authorization.role !== 'shell') throw new Error('Shell request required');
    ownWindow(authorization, request); ownTab(authorization, request);
    return authorization;
  }

  // Native gestures retain the original frame and assertions in main. Cancellation
  // may be late (after state delivery); it can only revoke its own opaque token.
  function authorizeDrag(event, request) {
    const authorization = authorize(event);
    if (authorization.role !== 'shell') throw new Error('Shell request required');
    if (Object.hasOwn(request, 'token')) {
      if (request.windowId !== authorization.windowId) throw new Error('Foreign window request');
    } else {
      ownWindow(authorization, request);
      if (Object.hasOwn(request, 'tabId')) ownTab(authorization, request);
    }
    return authorization;
  }

  function transferIndex(request) {
    if (Object.hasOwn(request, 'index') && !revision(request.index)) throw new TypeError('Invalid tab index');
  }

  function transferTarget(request) {
    if (!identifier(request.targetWindowId) || !revision(request.targetWindowRevision)) {
      throw new TypeError('Invalid transfer target');
    }
    const target = registry.getWindow(request.targetWindowId);
    if (!target || target.revision !== request.targetWindowRevision) throw new Error('Stale transfer target');
    const record = windows.get(request.targetWindowId);
    if (!record) throw new Error('Untrusted transfer target');
    const targetEvent = Object.freeze({ sender: record.contents, senderFrame: record.contents.mainFrame });
    const validateTarget = () => {
      // Capture the registration as well as native contents/frame: unregistering
      // and registering the same contents again cannot revive a pending drop.
      if (windows.get(request.targetWindowId) !== record) throw new Error('Untrusted transfer target');
      const authorization = authorize(targetEvent);
      if (authorization.role !== 'shell' || authorization.windowId !== request.targetWindowId) {
        throw new Error('Untrusted transfer target');
      }
    };
    validateTarget();
    return validateTarget;
  }

  async function publishTransfer(operation) {
    transferPublications++;
    try { return publicTab(await operation()); }
    finally { transferPublications--; publishState(); }
  }

  function validateClose(authorization, request) {
    shape(request, ['windowId', 'windowRevision', 'target'], ['tabId', 'tabRevision']);
    ownWindow(authorization, request);
    if (request.target === 'tab') {
      ownTab(authorization, request);
    } else if (request.target !== 'window' || authorization.role !== 'shell'
      || Object.hasOwn(request, 'tabId') || Object.hasOwn(request, 'tabRevision')) {
      throw new Error('Invalid close target');
    }
  }

  function currentCloseTab(tab, record, frame) {
    const current = registry.getTab(tab.tabId);
    if (!current || current.ownerWindowId !== tab.ownerWindowId || current.revision !== tab.revision
      || current.transferGeneration !== tab.transferGeneration || !sameBinding(current.project, tab.project)
      || identities.get(record.contents) !== record || record.contents.mainFrame !== frame) {
      throw new Error('Stale close renderer');
    }
    const authorization = authorize({ sender: record.contents, senderFrame: frame });
    if (authorization.role !== 'project' || authorization.tab.tabId !== tab.tabId) throw new Error('Foreign close renderer');
  }

  // Each ACK is one-shot and bound to the original native contents, frame,
  // registration, project epoch and tab revision, never just a renderer id.
  function closeRenderer(tab) {
    const record = [...identities.values()].find(item => item.role === 'project' && sameBinding(item.binding, tab.project));
    if (!record || tab.state !== 'ready') return null;
    const frame = record.contents.mainFrame;
    try { currentCloseTab(tab, record, frame); } catch { return null; }
    const request = Object.freeze({ requestId: randomUUID(), tabId: tab.tabId, tabRevision: tab.revision,
      project_id: tab.project.project_id, epoch: tab.project.epoch });
    return { record, frame, tab, request };
  }

  function prepareRenderer(session, phase) {
    if (!session) return Promise.resolve(false);
    const { record, frame, tab } = session;
    try { currentCloseTab(tab, record, frame); } catch { return Promise.resolve(false); }
    const request = Object.freeze({ ...session.request, phase });
    return new Promise(resolve => {
      const pending = { record, frame, tab, request, finish(prepared) {
        if (closeAcks.get(request.requestId) !== pending) return;
        closeAcks.delete(request.requestId); clearTimeout(timer); resolve(prepared);
      } };
      const timer = setTimeout(() => pending.finish(false), closeTimeout);
      closeAcks.set(request.requestId, pending);
      try { record.contents.send(TABS_CHANNELS.closeRequest, request); }
      catch { pending.finish(false); }
    });
  }

  function closeSurface(event, request) {
    const key = `${request.windowId}:${request.target === 'tab' ? request.tabId : 'window'}`;
    if (closing.has(key)) return closing.get(key);
    const work = registry.withCloseBarrier(async transaction => {
      const authorization = authorize(event);
      validateClose(authorization, request);
      const window = registry.getWindow(request.windowId);
      const tabs = (request.target === 'tab' ? [request.tabId] : window.tabIds).map(id => registry.getTab(id));
      const sessions = tabs.map(closeRenderer);
      try {
        const approvals = await Promise.all(sessions.map(session => prepareRenderer(session, 'prepare')));
        validateClose(authorize(event), request);
        if (!approvals.every(Boolean)) return false;
        // An early prepare ACK does not cover edits made while another tab
        // saves. Final validation holds a renderer input fence through disposal
        // (dev spec §5.3.5); merely polling dirty state would move the race.
        const sealed = await Promise.all(sessions.map(session => prepareRenderer(session, 'validate')));
        validateClose(authorize(event), request);
        if (!sealed.every(Boolean)) return false;
        for (const session of sessions) currentCloseTab(session.tab, session.record, session.frame);
        // No await between the last identity check and disposal of ALL views.
        for (const tab of tabs) transaction.closeTab(tab.tabId);
        return true;
      } finally {
        // Also cancel late prepare/validation work after timeout or an error.
        // Native close can complete asynchronously: removed tabs MUST keep
        // their fence until destruction. Only registry-owned survivors resume.
        for (const session of sessions) if (session && registry.getTab(session.tab.tabId) && live(session.record.contents)) {
          try { session.record.contents.send(TABS_CHANNELS.closeRequest, { ...session.request, phase: 'release' }); } catch {}
        }
      }
    }).then(closed => {
      if (closed && registry.getWindow(request.windowId)?.tabIds.length === 0) onWindowEmpty(request.windowId);
      return closed;
    }).finally(() => { closing.delete(key); publishState(); });
    closing.set(key, work);
    return work;
  }

  function requestCloseWindow(windowId) {
    const record = windows.get(String(windowId)), window = registry.getWindow(windowId);
    if (!record || !window) return Promise.resolve(false);
    const event = Object.freeze({ sender: record.contents, senderFrame: record.contents.mainFrame });
    return closeSurface(event, Object.freeze({ windowId: window.windowId, windowRevision: window.revision, target: 'window' }));
  }

  // Main-only quit/updater seam (dev spec §5.3.5): preserve every surface and
  // hold the same exclusion gate and renderer fences across the awaited work.
  // A veto returns false; otherwise preserve the callback's result/rejection.
  async function withAppCloseBarrier(callback) {
    if (typeof callback !== 'function') throw new TypeError('Close callback is required');
    if (disposed) return false;
    return registry.withCloseBarrier(async () => {
      if (disposed) return false;
      const sessions = registry.snapshot().tabs.map(closeRenderer);
      try {
        const approvals = await Promise.all(sessions.map(session => prepareRenderer(session, 'prepare')));
        if (disposed || !approvals.every(Boolean)) return false;
        const sealed = await Promise.all(sessions.map(session => prepareRenderer(session, 'validate')));
        if (disposed || !sealed.every(Boolean)) return false;
        try {
          for (const session of sessions) currentCloseTab(session.tab, session.record, session.frame);
        } catch { return false; }
        // No await between checking ALL identities and entering protected work.
          // Main rechecks after its summary/dialog awaits, before stopping work.
          const validate = () => {
            if (disposed) throw new Error('Close transaction expired');
            for (const session of sessions) currentCloseTab(session.tab, session.record, session.frame);
          };
          return await callback(validate);
      } finally {
        // The callback can dispose the bridge/registry or reject. Cleanup must
        // neither revive removed surfaces nor mask its result/rejection.
        for (const session of sessions) if (session) {
          try {
            if (registry.getTab(session.tab.tabId) && live(session.record.contents)) {
              session.record.contents.send(TABS_CHANNELS.closeRequest, { ...session.request, phase: 'release' });
            }
          } catch {}
        }
      }
    });
  }

  function addHandler(channel, handler) {
    ipcMain.handle(channel, handler); handlers.push(channel);
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    for (const pending of closeAcks.values()) pending.finish(false);
    for (const pending of pageRestores.values()) pending.finish('unavailable');
    pages.clear();
    for (const record of [...identities.values()]) unregister(record);
    for (const channel of handlers) ipcMain.removeHandler(channel);
    handlers.length = 0;
  }

  try {
    addHandler(TABS_CHANNELS.reportPage, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length) throw new TypeError('Invalid page report');
      pageShape(request, ['tabId', 'tabRevision', 'transferGeneration', 'page']);
      const tab = ownPage(authorization, request);
      const page = copyPage(request.page);
      pages.set(tab.tabId, { binding: publicBinding(tab.project), page });
      pageChanged();
    });
    addHandler(TABS_CHANNELS.pageRestoreAck, (event, ack, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'project') throw new Error('Project page ACK required');
      pageShape(ack, ['requestId', 'tabId', 'tabRevision', 'transferGeneration', 'status']);
      if (!identifier(ack.requestId) || !pageStatuses.has(ack.status)) throw new TypeError('Invalid page restore ACK');
      const pending = pageRestores.get(ack.requestId);
      if (!pending || pending.record.contents !== event.sender || pending.frame !== event.senderFrame
        || Object.keys(pending.request).some(key => pending.request[key] !== ack[key])) throw new Error('Stale page restore ACK');
      try { currentPageRestore(pending); }
      catch (error) { pending.finish('stale'); throw error; }
      pending.finish(ack.status);
    });
    addHandler(TABS_CHANNELS.dragBegin, (event, request, ...extra) => {
      if (authorize(event).role !== 'shell') throw new Error('Shell request required');
      if (extra.length) throw new TypeError('Invalid drag request');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision', 'point']);
      authorizeDrag(event, request);
      shape(request.point, ['x', 'y']);
      if (![request.point.x, request.point.y].every(Number.isFinite)) throw new TypeError('Invalid drag point');
      if (!tabDrag) throw new Error('Native tab drag is unavailable');
      return tabDrag.begin(event, request);
    });
    addHandler(TABS_CHANNELS.dragCancel, (event, request, ...extra) => {
      if (authorize(event).role !== 'shell') throw new Error('Shell request required');
      if (extra.length) throw new TypeError('Invalid drag request');
      shape(request, ['windowId', 'windowRevision', 'token']);
      authorizeDrag(event, request);
      if (!revision(request.windowRevision) || !identifier(request.token)) throw new TypeError('Invalid drag token');
      return tabDrag?.cancel(event, request);
    });
    addHandler(TABS_CHANNELS.dragGeometry, (event, request, ...extra) => {
      if (authorize(event).role !== 'shell') throw new Error('Shell request required');
      if (extra.length) throw new TypeError('Invalid drag geometry');
      shape(request, ['windowId', 'windowRevision', 'tabs'], ['appendStart', 'appendEnd']);
      authorizeDrag(event, request);
      if ((Object.hasOwn(request, 'appendStart') || Object.hasOwn(request, 'appendEnd'))
        && (!Number.isFinite(request.appendStart) || !Number.isFinite(request.appendEnd)
          || request.appendStart < 0 || request.appendEnd <= request.appendStart || request.appendEnd > 1e7)) throw new TypeError('Invalid trailing strip boundary');
      if (!Array.isArray(request.tabs) || request.tabs.length > 512) throw new TypeError('Invalid drag geometry');
      for (const rect of request.tabs) {
        shape(rect, ['tabId', 'x', 'y', 'width', 'height']);
        if (!identifier(rect.tabId) || ![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)
          || rect.width <= 0 || rect.height <= 0) throw new TypeError('Invalid drag rectangle');
      }
      if (!tabDrag) throw new Error('Native tab drag is unavailable');
      return tabDrag.setGeometry(event, request);
    });
    // Every service has its own fixed argument contract and is shell-only.
    for (const [name, kind] of [['recentProjects', 'none'], ['pickerStatus', 'none'],
      ['inspectPath', 'path'], ['browseDirectory', 'browse'], ['pickDirectory', 'optional'], ['forgetRecent', 'path']]) {
      addHandler(TABS_CHANNELS[name], async (event, ...args) => {
        if (authorize(event).role !== 'shell') throw new Error('Shell request required');
        const value = args[0];
        if (kind === 'none' ? args.length !== 0 : kind === 'optional' ? args.length > 1 : args.length !== 1) {
          throw new TypeError('Invalid shell service arguments');
        }
        if (kind !== 'none' && !(kind === 'optional' && value === undefined)
          && (typeof value !== 'string' || value.length > 32768 || value.includes('\0')
            || (kind !== 'browse' && !value.trim()))) throw new TypeError('Invalid path');
        if (typeof shellServices?.[name] !== 'function') throw new Error('Shell service is unavailable');
        const call = () => shellServices[name](...(kind === 'none' ? [] : [value]));
        // The OS picker is modal; close and transfer cannot pass its await.
        const result = await (name === 'pickDirectory' && typeof registry.withCloseBarrier === 'function'
          ? registry.withCloseBarrier(call) : call());
        authorize(event); // Do not deliver a late result to a navigated/closed shell.
        return result;
      });
    }
    addHandler(TABS_CHANNELS.setMenuOpen, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'open']);
      ownWindow(authorization, request);
      if (typeof request.open !== 'boolean') throw new TypeError('Invalid menu visibility');
      if (typeof setMenuOpen !== 'function') throw new Error('Shell menu is unavailable');
      setMenuOpen(authorization.windowId, request.open);
      publishState();
    });
    for (const channel of [TABS_CHANNELS.bootstrap, TABS_CHANNELS.state]) {
      addHandler(channel, (event, ...args) => {
        const authorization = authorize(event);
        if (args.length) throw new TypeError('This request takes no payload');
        return stateFor(authorization);
      });
    }
    addHandler(TABS_CHANNELS.open, async (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'root'], ['create']);
      ownWindow(authorization, request);
      if (typeof request.root !== 'string' || !request.root.trim() || request.root.length > 32768
        || request.root.includes('\0') || (Object.hasOwn(request, 'create') && typeof request.create !== 'boolean')) {
        throw new TypeError('Invalid project open request');
      }
      // Only open may focus an existing tab in another window (§5.3.1 D1).
      try {
        const tab = await registry.openProject(request.root, { windowId: authorization.windowId, create: request.create ?? false });
        authorize(event);
        const owner = windows.get(authorization.windowId); if (owner) delete owner.error;
        return publicTab(tab);
      } finally { publishState(); }
    });
    addHandler(TABS_CHANNELS.activate, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision']);
      ownWindow(authorization, request); ownTab(authorization, request);
      const tab = registry.activateTab(request.tabId);
      publishState();
      // Shell activation is also the keyboard strip operation. Keep the native
      // input target in the shell so repeated Arrow/Home/End keep working; a
      // pointer click in project content can focus that view normally (§5.3.4).
      authorization.contents.focus();
      return publicTab(tab);
    });
    addHandler(TABS_CHANNELS.transferBegin, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision']);
      sourceShell(event, request, authorization);
      const result = registry.beginTransfer(request.tabId, { windowId: request.windowId,
        windowRevision: request.windowRevision, tabRevision: request.tabRevision, sender: authorization.contents,
        senderFrame: event.senderFrame });
      // The opaque, short-lived capability is the only private registry value
      // intentionally returned to its initiating shell. No native handles.
      return { token: result.token, expiresAt: result.expiresAt };
    });
    addHandler(TABS_CHANNELS.transfer, async (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision', 'token',
        'targetWindowId', 'targetWindowRevision'], ['index']);
      sourceShell(event, request, authorization);
      if (!identifier(request.token)) throw new TypeError('Invalid transfer token');
      const source = Object.freeze({ ...request });
      const validateTarget = transferTarget(source); transferIndex(source);
      return publishTransfer(async () => {
        const tab = await registry.transferTab(source.token, { targetWindowId: source.targetWindowId,
          targetWindowRevision: source.targetWindowRevision,
          ...(Object.hasOwn(source, 'index') ? { index: source.index } : {}), sender: authorization.contents,
          senderFrame: event.senderFrame,
          // Core calls this before commit, while the tab still belongs to the
          // source. Recheck the original frame, document and current revisions.
          validateSender: () => { sourceShell(event, source); validateTarget(); } });
        // Core already revalidated the source immediately before commit. Its
        // empty source shell may now have been disposed by the app controller.
        if (registry.getWindow(source.windowId)) authorize(event);
        return tab;
      });
    });
    addHandler(TABS_CHANNELS.detach, async (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision']);
      sourceShell(event, request, authorization);
      const source = Object.freeze({ ...request });
      return publishTransfer(async () => {
        const tab = await registry.detachTab(source.tabId, { windowId: source.windowId,
          windowRevision: source.windowRevision, tabRevision: source.tabRevision, sender: authorization.contents,
          senderFrame: event.senderFrame,
          validateSender: () => { sourceShell(event, source); } });
        if (registry.getWindow(source.windowId)) authorize(event);
        return tab;
      });
    });
    addHandler(TABS_CHANNELS.reorder, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision', 'index']);
      sourceShell(event, request, authorization); transferIndex(request);
      try {
        return publicTab(registry.reorderTab(request.tabId, { windowId: request.windowId,
          windowRevision: request.windowRevision, tabRevision: request.tabRevision, index: request.index }));
      } finally { publishState(); }
    });
    addHandler(TABS_CHANNELS.prepareClose, async (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length) throw new TypeError('Invalid close request');
      validateClose(authorization, request);
      const source = Object.freeze({ ...request });
      const prepare = async () => {
        validateClose(authorize(event), source);
        const result = prepareClose ? await prepareClose({ authorization, request: source }) : null;
        validateClose(authorize(event), source);
        return { prepared: result?.prepared === true };
      };
      // Hold core's transfer exclusion across the callback AND late validation
      // (dev spec §5.3.5). Older read-only registries/mocks retain the seam.
      return typeof registry.withCloseBarrier === 'function' ? registry.withCloseBarrier(prepare) : prepare();
    });
    addHandler(TABS_CHANNELS.close, (event, request, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'shell') throw new Error('Shell request required');
      shape(request, ['windowId', 'windowRevision', 'tabId', 'tabRevision']);
      ownWindow(authorization, request); ownTab(authorization, request);
      return closeSurface(event, Object.freeze({ ...request, target: 'tab' })).then(closed => ({ closed }));
    });
    addHandler(TABS_CHANNELS.closeAck, (event, ack, ...extra) => {
      const authorization = authorize(event);
      if (extra.length || authorization.role !== 'project') throw new Error('Project ACK required');
      shape(ack, ['requestId', 'tabId', 'tabRevision', 'project_id', 'epoch', 'phase', 'prepared']);
      if (!identifier(ack.requestId) || typeof ack.prepared !== 'boolean') throw new TypeError('Invalid close ACK');
      const pending = closeAcks.get(ack.requestId);
      if (!pending || pending.record.contents !== event.sender || pending.frame !== event.senderFrame
        || Object.keys(pending.request).some(key => pending.request[key] !== ack[key])) throw new Error('Stale close ACK');
      try { currentCloseTab(pending.tab, pending.record, pending.frame); }
      catch (error) { pending.finish(false); throw error; }
      pending.finish(ack.prepared);
    });
  } catch (error) { dispose(); throw error; }

  return Object.freeze({ registerWindow, registerProjectView, authorize, authorizeDrag,
    setWindowError(windowId, error) {
      const record = windows.get(String(windowId));
      if (record) { record.error = typeof error === 'string' ? error : undefined; publishState(); }
    },
    contentsForWindow: windowId => authorizedContents().filter(item => item.windowId === String(windowId)).map(item => item.contents),
    allContents: () => authorizedContents().map(item => item.contents),
    // Main calls this after registry mutations outside IPC (boot/open/transfer).
    publishState, requestCloseWindow, withAppCloseBarrier, snapshotPages, restorePage, dispose });
}

module.exports = { registerTabsIpc, TABS_CHANNELS, createShellServices, documentURL };
