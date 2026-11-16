'use strict';

// Main-owned surfaces, dev spec §5.3.2–5.3.5 (VS-46). This module deliberately
// does not import main.cjs: startup/launcher, IPC authorization, shell UI, dirty
// manifest restore and Quit belong to their controllers. Register only
// BrowserWindows already displaying a shell. Transfer/close share this registry's
// exclusion gate, not an independent renderer-owned lifecycle.
const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { ORIGIN } = require('./policy.cjs');

function rootKey(root) {
  if (typeof root !== 'string' || !root.trim()) throw new TypeError('Invalid project root');
  return process.platform === 'win32' ? path.win32.normalize(root).toLowerCase() : path.normalize(root);
}

// Realpath resolves junctions/symlinks, not just spelling. Missing folders are
// errors; neither this function nor ensureProject creates a folder by default.
async function canonicalProjectRoot(root) {
  if (typeof root !== 'string' || !root.trim()) throw new TypeError('Invalid project root');
  return rootKey(await fs.realpath(root));
}

function bindingFrom(value) {
  if (!value || typeof value.root !== 'string' || !value.root.trim()
    || typeof value.project_id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value.project_id)
    || typeof value.epoch !== 'string' || !value.epoch) throw new Error('Invalid project binding');
  return Object.freeze({ root: value.root, project_id: value.project_id, epoch: value.epoch });
}

// VS-22 ensure/open only. No legacy selection, health/launcher, retry or fallback.
function createBackendAdapter({ fetch: request = globalThis.fetch, token } = {}) {
  if (typeof request !== 'function') throw new TypeError('fetch is required');
  return Object.freeze({
    async ensureProject({ path: root, create = false }) {
      const secret = typeof token === 'function' ? token() : token;
      const response = await request(`${ORIGIN}/api/projects/open`, {
        method: 'POST', redirect: 'error', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...(secret ? { Authorization: `Bearer ${secret}` } : {}) },
        body: JSON.stringify({ path: root, create }),
      });
      if (!response.ok) {
        const error = new Error(`Could not open project (${response.status})`);
        error.status = response.status;
        throw error;
      }
      return response.json();
    },
  });
}

// Inject Electron rather than requiring it in unit tests. configureProjectView
// is the app-level permissions/role allowlist seam; its optional disposer runs
// even on load failure. Do not reuse the legacy single-contents policy closure.
function createElectronAdapter({ WebContentsView, webPreferences = {}, configureProjectView = () => {}, waitForProjectReady }) {
  const cleanups = new WeakMap();
  return Object.freeze({
    createProjectView(binding) {
      binding = bindingFrom(binding);
      const view = new WebContentsView({ webPreferences: { ...webPreferences,
        nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, backgroundThrottling: false } });
      const contents = view.webContents;
      const prefix = `/p/${binding.project_id}`;
      const guard = (event, address) => {
        let allowed = false;
        try {
          const url = new URL(address);
          allowed = url.origin === ORIGIN && !url.username && !url.password
            && (url.pathname === prefix || url.pathname.startsWith(`${prefix}/`));
        } catch {}
        if (!allowed) event.preventDefault();
      };
      contents.on('will-navigate', guard);
      contents.on('will-redirect', guard);
      contents.setWindowOpenHandler(() => ({ action: 'deny' }));
      let release;
      const cleanup = () => {
        contents.removeListener('will-navigate', guard);
        contents.removeListener('will-redirect', guard);
        contents.removeListener('destroyed', cleanup);
        cleanups.delete(view);
        if (typeof release === 'function') release();
      };
      cleanups.set(view, cleanup);
      contents.once('destroyed', cleanup);
      try { release = configureProjectView(view, binding); }
      catch (error) { cleanup(); if (!contents.isDestroyed()) contents.close(); throw error; }
      return view;
    },
    async loadProjectView(view, binding) {
      binding = bindingFrom(binding);
      await view.webContents.loadURL(`${ORIGIN}/p/${binding.project_id}/`);
      // Navigation completion is NOT a SPA bootstrap ACK. The future authorized
      // bootstrap bridge can inject its own ACK/timeout here; no executeJavaScript.
      if (waitForProjectReady) await waitForProjectReady(view, binding);
    },
    attachView(window, view) { window.contentView.addChildView(view); view.setVisible(true); },
    parkView(window, view) { view.setVisible(false); window.contentView.removeChildView(view); },
    setBounds: (view, bounds) => view.setBounds(bounds),
    focusView: view => view.webContents.focus(),
    async waitForTransferAttach(view, window, { signal } = {}) {
      const contents = view.webContents;
      const check = () => {
        if (signal?.aborted || window.isDestroyed() || contents.isDestroyed()
          || view.webContents !== contents || !window.contentView.children.includes(view)) {
          throw new Error('Transfer attachment is no longer available');
        }
      };
      check();
      // VS-21's accepted primitive: native attach is synchronous; a response
      // from the SAME live contents ACKs responsiveness, never a new bootstrap.
      await contents.executeJavaScript('true');
      check();
      return true;
    },
    destroyView(view) {
      cleanups.get(view)?.();
      if (!view.webContents.isDestroyed()) view.webContents.close();
    },
  });
}

function insetsFrom(value = {}) {
  const result = {};
  for (const key of ['top', 'right', 'bottom', 'left']) {
    const number = value[key] ?? 0;
    if (!Number.isSafeInteger(number) || number < 0) throw new TypeError('Insets must be non-negative DIP integers');
    result[key] = number;
  }
  return Object.freeze(result);
}

/**
 * WindowRecord (private): native window/shell, ordered tabIds, activeTabId,
 * insets, revision, listeners. TabRecord (private): immutable project binding,
 * one strong view reference, ownerWindowId, loading/ready/crashed/recovering
 * lifecycle, revision, transferGeneration (never changed by open/focus).
 *
 * Public snapshots contain no native handles or project stores. These are main
 * APIs, NOT renderer IPC handlers: the IPC router must authorize role, sender,
 * main frame, origin, ownership and revision before calling them (§5.3.4).
 * closeTab/unregisterWindow are post-close-barrier disposal primitives, not UX
 * close requests. dispose is explicit surface teardown, not app.quit/backend stop.
 * Transfers retain source ownership until native attachment + same-contents ACK.
 * openProject must never be used as a substitute. Restore resolves roots via
 * openProject; this module does not persist/restore anything or own a tray.
 */
class DesktopTabs {
  #windows = new Map();
  #tabs = new Map();
  #roots = new Map();
  #pending = new Map();
  #backend;
  #electron;
  #canonicalize;
  #disposed = false;
  #tokens = new Map();
  #operation = null;
  #now;
  #transferTTL;
  #transferTimeout;
  #createShell;
  #onWindowEmpty;
  #onCommit;
  #commitPending = false;
  #commitScheduled = false;
  #shellFactories = 0;
  #changing = 0;
  #idleWaiters = new Set();

  // createShell must return a NEW registered, empty, shell-ready window with
  // the app's normal security/services. It must not start a backend or project.
  // The app factory keeps it hidden until the transfer/recovery commits.
  // onCommit(snapshot) is a coalesced microtask notification of stable topology;
  // observers must not depend on one callback per command or dispose teardown.
  constructor({ backend, electron, canonicalizeRoot = canonicalProjectRoot,
    now = Date.now, transferTTL = 5000, transferTimeout = 5000, createShell, onWindowEmpty, onCommit } = {}) {
    if (typeof backend?.ensureProject !== 'function') throw new TypeError('ensureProject adapter is required');
    for (const method of ['createProjectView', 'loadProjectView', 'attachView', 'parkView', 'setBounds', 'focusView', 'destroyView']) {
      if (typeof electron?.[method] !== 'function') throw new TypeError(`Electron adapter requires ${method}`);
    }
    if (typeof now !== 'function' || !Number.isSafeInteger(transferTTL) || transferTTL <= 0 || transferTTL > 30000
      || !Number.isSafeInteger(transferTimeout) || transferTimeout <= 0 || transferTimeout > 30000
      || (createShell !== undefined && typeof createShell !== 'function')
      || (onWindowEmpty !== undefined && typeof onWindowEmpty !== 'function')
      || (onCommit !== undefined && typeof onCommit !== 'function')) throw new TypeError('Invalid transfer options');
    this.#backend = backend; this.#electron = electron; this.#canonicalize = canonicalizeRoot;
    this.#now = now; this.#transferTTL = transferTTL; this.#transferTimeout = transferTimeout; this.#createShell = createShell;
    this.#onWindowEmpty = onWindowEmpty; this.#onCommit = onCommit;
  }

  #idle() {
    if (this.#disposed) throw new Error('Window registry is disposed');
    if (this.#operation) throw Object.assign(new Error('Surface operation is busy'), { code: 'surface_busy' });
  }

  #liveWindow(record) {
    return !!record && this.#windows.get(record.windowId) === record && !record.window.isDestroyed();
  }

  #window(id) {
    const record = this.#windows.get(String(id));
    if (this.#disposed || !record || record.window.isDestroyed()) throw new Error('Window is no longer available');
    return record;
  }

  registerWindow(window, { windowId = String(window.id), insets } = {}) {
    windowId = String(windowId);
    if (this.#disposed || !windowId || window.isDestroyed()) throw new Error('Window is no longer available');
    if (this.#windows.has(windowId) || [...this.#windows.values()].some(item => item.window === window
      || item.shell === window.webContents)) throw new Error('Window is already registered');
    const record = { windowId, window, shell: window.webContents, tabIds: [], activeTabId: null,
      insets: insetsFrom(insets), revision: 0, listeners: [], speculative: false };
    this.#windows.set(windowId, record);
    const listen = (event, callback) => { window.on(event, callback); record.listeners.push([event, callback]); };
    listen('closed', () => this.unregisterWindow(windowId));
    listen('resize', () => { if (!window.isDestroyed()) this.#layout(record); });
    return this.getWindow(windowId);
  }

  getWindow(windowId) {
    const record = this.#windows.get(String(windowId));
    return record ? Object.freeze({ windowId: record.windowId, tabIds: Object.freeze([...record.tabIds]),
      activeTabId: record.activeTabId, revision: record.revision, insets: record.insets }) : null;
  }

  getTab(tabId) {
    const record = this.#tabs.get(tabId);
    return record ? Object.freeze({ tabId, project: record.project, ownerWindowId: record.ownerWindowId,
      state: record.state, revision: record.revision, transferGeneration: record.transferGeneration }) : null;
  }

  snapshot() {
    return Object.freeze({ windows: Object.freeze([...this.#windows.keys()].map(id => this.getWindow(id))),
      tabs: Object.freeze([...this.#tabs.keys()].map(id => this.getTab(id))) });
  }

  // Manifest seam (dev spec §5.3.6): never persist a tentative topology.
  // snapshot() remains the diagnostic/IPC view, including in-flight surfaces.
  committedSnapshot({ closeBarrier = false } = {}) {
    // Main's validated Quit barrier can read settled topology while retaining
    // its gate, but cannot bypass pending opens or abandoned transfer factories.
    if (this.#disposed || (this.#operation && !(closeBarrier && this.#operation.kind === 'close'))
      || this.#changing || this.#pending.size || this.#shellFactories) return null;
    for (const window of this.#windows.values()) {
      if (window.speculative || !this.#liveWindow(window)) return null;
    }
    for (const tab of this.#tabs.values()) {
      const owner = this.#windows.get(tab.ownerWindowId);
      if (!tab.committed || !['ready', 'crashed'].includes(tab.state) || !owner?.tabIds.includes(tab.tabId)) return null;
    }
    return this.snapshot();
  }

  #change(callback) {
    this.#changing++;
    try { return callback(); }
    finally { this.#changing--; this.#flushCommit(); }
  }

  #committed() {
    if (this.#disposed || !this.#onCommit) return;
    this.#commitPending = true;
    this.#flushCommit();
  }

  #flushCommit() {
    if (!this.#operation || this.#disposed) {
      for (const finish of this.#idleWaiters) finish();
      this.#idleWaiters.clear();
    }
    if (this.#disposed || !this.#onCommit || !this.#commitPending || this.#commitScheduled) return;
    this.#commitScheduled = true;
    queueMicrotask(() => {
      this.#commitScheduled = false;
      if (!this.#commitPending) return;
      const snapshot = this.committedSnapshot();
      if (!snapshot) return; // the last gate to settle retries publication
      this.#commitPending = false;
      // Observers run outside registry transactions; neither sync throws nor
      // async rejections may roll back ownership or escape as unhandled errors.
      try { Promise.resolve(this.#onCommit(snapshot)).catch(() => {}); } catch {}
    });
  }

  async openProject(root, { windowId, create = false } = {}) {
    this.#idle();
    const owner = this.#window(windowId); // capture the *record*, not a reusable id
    if (typeof create !== 'boolean') throw new TypeError('create must be a boolean');
    const key = rootKey(await this.#canonicalize(root));
    this.#idle();
    if (this.#window(owner.windowId) !== owner) throw new Error('Opening window was replaced');
    let operation = this.#pending.get(key);
    if (!operation) {
      operation = this.#open(root, key, create, owner);
      this.#pending.set(key, operation);
      // Remove only our own generation; a failed open must be retryable.
      operation.finally(() => {
        if (this.#pending.get(key) === operation) this.#pending.delete(key);
        this.#flushCommit();
      }).catch(() => {});
    }
    const tabId = await operation;
    // Never assign the calling window here: a late open activates the owner.
    return this.activateTab(tabId);
  }

  async #open(root, key, create, owner) {
    const known = this.#tabs.get(this.#roots.get(key));
    if (known) { await known.ready; return known.tabId; }
    const project = bindingFrom(await this.#backend.ensureProject({ path: root, create }));
    const confirmed = rootKey(await this.#canonicalize(project.root));
    this.#idle();
    if (this.#window(owner.windowId) !== owner) throw new Error('Opening window was replaced');
    const existing = this.#tabs.get(this.#roots.get(confirmed));
    if (existing) {
      if (existing.project.project_id !== project.project_id || existing.project.epoch !== project.epoch) {
        throw new Error('Project binding changed; explicit recovery is required');
      }
      await existing.ready;
      return existing.tabId;
    }
    // No await between uniqueness check, native creation and root reservation.
    const tabId = randomUUID();
    const view = this.#electron.createProjectView(project);
    if (!view?.webContents || view.webContents.isDestroyed()
      || [...this.#tabs.values()].some(tab => tab.view === view || tab.view.webContents === view.webContents)
      || [...this.#windows.values()].some(window => window.shell === view.webContents)) {
      // Never destroy a reused view: it may belong to an incumbent tab/shell.
      throw new Error('Adapter must create a unique live project view');
    }
    const record = { tabId, project, view, ownerWindowId: owner.windowId, state: 'loading',
      revision: 0, transferGeneration: 0, attached: false, listeners: [], ready: null, committed: false };
    this.#tabs.set(tabId, record); this.#roots.set(confirmed, tabId);
    owner.tabIds.push(tabId); owner.revision++;
    const listen = (event, callback) => { view.webContents.on(event, callback); record.listeners.push([event, callback]); };
    listen('destroyed', () => this.#removeTab(record, false));
    listen('render-process-gone', () => {
      if (this.#tabs.get(tabId) !== record) return;
      record.state = 'crashed'; record.revision++;
      if (this.#operation?.kind === 'transfer' && this.#operation.tab === record) {
        this.#operation.abort.abort(new Error('Transfer renderer crashed'));
      }
    });
    record.ready = (async () => {
      try {
        await this.#electron.loadProjectView(view, project);
        if (this.#tabs.get(tabId) !== record || this.#window(owner.windowId) !== owner) {
          throw new Error('Tab was closed while opening');
        }
        if (record.state === 'crashed') throw new Error('Project renderer crashed while opening');
        record.state = 'ready'; record.revision++;
      } catch (error) { this.#removeTab(record, true); throw error; }
    })();
    try {
      await record.ready;
      this.#idle();
      this.#selectTab(record); // first attach is part of the open transaction
    } catch (error) { this.#removeTab(record, true); throw error; }
    return tabId;
  }

  #bounds(record) {
    const [width, height] = record.window.getContentSize();
    const { top, right, bottom, left } = record.insets;
    return { x: Math.min(left, width), y: Math.min(top, height),
      width: Math.max(0, width - left - right), height: Math.max(0, height - top - bottom) };
  }

  #layout(window) {
    const tab = this.#tabs.get(window.activeTabId);
    if (tab?.attached) this.#electron.setBounds(tab.view, this.#bounds(window));
  }

  setContentInsets(windowId, insets) {
    const window = this.#window(windowId);
    window.insets = insetsFrom(insets); window.revision++;
    this.#layout(window);
    return this.getWindow(window.windowId);
  }

  #selectTab(tab) {
    const owner = this.#window(tab.ownerWindowId);
    const previous = this.#tabs.get(owner.activeTabId);
    if (previous !== tab || !tab.attached) {
      try {
        this.#electron.setBounds(tab.view, this.#bounds(owner));
        this.#electron.attachView(owner.window, tab.view);
        if (previous !== tab && previous?.attached) this.#electron.parkView(owner.window, previous.view);
      } catch (error) {
        // Native attach/bounds failure cannot steal active ownership.
        try { this.#electron.parkView(owner.window, tab.view); } catch {}
        if (previous?.attached) {
          try { this.#electron.attachView(owner.window, previous.view); } catch {}
        }
        throw error;
      }
      if (previous !== tab && previous) previous.attached = false;
      tab.attached = true; owner.activeTabId = tab.tabId; owner.revision++;
    } else this.#layout(owner);
    return owner;
  }

  activateTab(tabId) {
    this.#idle();
    return this.#change(() => {
      const result = this.#activateTab(tabId);
      this.#tabs.get(tabId).committed = true;
      this.#committed();
      return result;
    });
  }

  // Main-only notification activation (§5.3.6). Hold the same exclusion gate as
  // transfer/close through the project-relative page ACK, never a loadURL/rebind.
  async whenIdle() {
    if (this.#operation && !this.#disposed) await new Promise(resolve => this.#idleWaiters.add(resolve));
    this.#idle();
  }

  async withTabActivation(tabId, callback) {
    this.#idle();
    const operation = { kind: 'activation' };
    this.#operation = operation;
    try {
      const tab = this.#change(() => {
        const result = this.#activateTab(tabId);
        this.#tabs.get(tabId).committed = true;
        this.#committed();
        return result;
      });
      return await callback(tab);
    } finally {
      if (this.#operation === operation) this.#operation = null;
      this.#flushCommit();
    }
  }

  #activateTab(tabId) {
    const tab = this.#tabs.get(tabId);
    if (!tab || tab.state === 'loading') throw new Error('Tab is not available');
    const owner = this.#selectTab(tab);
    if (owner.window.isMinimized()) owner.window.restore();
    owner.window.show(); owner.window.focus(); this.#electron.focusView(tab.view);
    return this.getTab(tabId);
  }

  #removeTab(tab, destroy) {
    return this.#change(() => this.#removeTabNow(tab, destroy));
  }

  #removeTabNow(tab, destroy) {
    if (this.#tabs.get(tab.tabId) !== tab) return false;
    if (this.#operation?.kind === 'transfer' && this.#operation.tab === tab) {
      this.#operation.abort.abort(new Error('Transfer contents were destroyed'));
    }
    this.#tabs.delete(tab.tabId);
    for (const [key, lease] of this.#tokens) if (lease.tab === tab) this.#tokens.delete(key);
    for (const [root, id] of this.#roots) if (id === tab.tabId) this.#roots.delete(root);
    for (const [event, listener] of tab.listeners) tab.view.webContents.removeListener(event, listener);
    const owner = this.#windows.get(tab.ownerWindowId);
    if (owner) {
      owner.tabIds = owner.tabIds.filter(id => id !== tab.tabId); owner.revision++;
      if (owner.activeTabId === tab.tabId) owner.activeTabId = null;
      if (tab.attached && !owner.window.isDestroyed()) {
        try { this.#electron.parkView(owner.window, tab.view); } catch {}
      }
    }
    tab.attached = false; tab.state = 'closing';
    if (destroy) this.#electron.destroyView(tab.view);
    if (owner && !owner.activeTabId && !owner.window.isDestroyed()) {
      const next = owner.tabIds.find(id => this.#tabs.get(id)?.state === 'ready');
      if (next) this.#activateTab(next);
    }
    if (tab.committed) this.#committed();
    return true;
  }

  closeTab(tabId) {
    this.#idle();
    const tab = this.#tabs.get(tabId);
    return tab ? this.#removeTab(tab, true) : false;
  }

  // A conservative app-wide gate also covers native-modal/dirty-close barriers.
  // Fail busy rather than queue a stale renderer command across a transfer.
  async withCloseBarrier(callback) {
    this.#idle();
    if (typeof callback !== 'function') throw new TypeError('Close callback is required');
    const operation = { kind: 'close' };
    this.#operation = operation;
    // Disposal is available only inside this exact operation. A saved callback
    // cannot later bypass transfer/modal exclusion (dev spec §5.3.5).
    const closeTab = tabId => {
      if (this.#disposed || this.#operation !== operation) throw new Error('Close transaction expired');
      const tab = this.#tabs.get(tabId);
      return tab ? this.#removeTab(tab, true) : false;
    };
    try { return await callback({ closeTab }); }
    finally {
      if (this.#operation === operation) this.#operation = null;
      this.#flushCommit();
    }
  }

  #source(tabId, { windowId, windowRevision, tabRevision }) {
    const source = this.#window(windowId), tab = this.#tabs.get(tabId);
    if (!tab || tab.state !== 'ready' || tab.view.webContents.isDestroyed()
      || tab.ownerWindowId !== source.windowId) throw new Error('Transfer tab is no longer available');
    if (source.revision !== windowRevision || tab.revision !== tabRevision) throw new Error('Stale transfer revision');
    return { source, tab };
  }

  beginTransfer(tabId, options = {}) {
    this.#idle();
    const { source, tab } = this.#source(tabId, options);
    if (!options.sender) throw new TypeError('Transfer sender is required');
    const now = this.#now();
    for (const [key, value] of this.#tokens) {
      if (value.expiresAt <= now || value.tab === tab) this.#tokens.delete(key);
    }
    const token = randomUUID(), expiresAt = now + this.#transferTTL;
    this.#tokens.set(token, { source, tab, contents: tab.view.webContents,
      sender: options.sender, senderFrame: options.senderFrame, expiresAt,
      windowRevision: source.revision, tabRevision: tab.revision, generation: tab.transferGeneration });
    return Object.freeze({ token, expiresAt });
  }

  #consume(token, sender, senderFrame) {
    const lease = this.#tokens.get(token);
    // Every attempted use burns the capability, including wrong sender/failure.
    this.#tokens.delete(token);
    if (!lease || lease.sender !== sender || (lease.senderFrame && lease.senderFrame !== senderFrame)
      || lease.expiresAt <= this.#now()) throw new Error('Invalid or expired transfer token');
    this.#idle();
    const { source, tab } = this.#source(lease.tab.tabId, { windowId: lease.source.windowId,
      windowRevision: lease.windowRevision, tabRevision: lease.tabRevision });
    if (tab !== lease.tab || source !== lease.source || tab.transferGeneration !== lease.generation) {
      throw new Error('Stale transfer generation');
    }
    return lease;
  }

  // IPC role registration is a capability generation too: removing a shell's
  // role cannot leave its pending gesture valid if that same native contents is
  // later registered again without navigating.
  revokeTransferSender(sender) {
    for (const [key, lease] of this.#tokens) if (lease.sender === sender) this.#tokens.delete(key);
    if (this.#operation?.kind === 'transfer' && this.#operation.sender === sender) {
      this.#operation.abort.abort(new Error('Transfer sender was revoked'));
    }
  }

  #startOperation(lease) {
    const operation = { kind: 'transfer', contents: lease.tab.view.webContents, ...lease, target: null, abort: new AbortController() };
    this.#operation = operation;
    return operation;
  }

  async #wait(operation, promise) {
    const signal = operation.abort.signal;
    if (signal.aborted) {
      // Native/factory work can synchronously destroy a window before returning
      // its promise. Observe its eventual rejection even though cancellation won.
      Promise.resolve(promise).catch(() => {});
      throw signal.reason;
    }
    let timer, cancel;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        cancel = () => reject(signal.reason || new Error('Transfer cancelled'));
        signal.addEventListener('abort', cancel, { once: true });
        timer = setTimeout(() => { operation.abort.abort(new Error('Transfer ACK timeout')); }, this.#transferTimeout);
      })]);
    } finally { clearTimeout(timer); signal.removeEventListener('abort', cancel); }
  }

  #checkOperation(operation) {
    if (this.#disposed || operation.abort.signal.aborted) throw operation.abort.signal.reason || new Error('Transfer cancelled');
    if (this.#tabs.get(operation.tab.tabId) !== operation.tab || operation.tab.view.webContents !== operation.contents
      || operation.contents.isDestroyed()) {
      throw new Error('Transfer contents are no longer available');
    }
    if (!this.#liveWindow(operation.target)) throw new Error('Transfer target is no longer available');
  }

  #focus(window, tab) {
    if (window.window.isMinimized()) window.window.restore();
    window.window.show(); window.window.focus(); this.#electron.focusView(tab.view);
  }

  #closeEmpty(window) {
    if (this.#liveWindow(window) && !window.tabIds.length) {
      if (this.#onWindowEmpty) this.#onWindowEmpty(window.windowId);
      else window.window.close();
    }
  }

  async #newShell(reason, operation) {
    if (!this.#createShell) throw new Error('Shell factory is unavailable');
    const existing = new Set(this.#windows.values());
    let abandoned = false;
    this.#shellFactories++;
    const pending = Promise.resolve().then(() => this.#createShell({ reason }));
    const recordFor = native => {
      const record = this.#windows.get(String(native?.id));
      if (!this.#liveWindow(record) || record.window !== native || existing.has(record) || record.tabIds.length) {
        throw new Error('Shell factory must return a new registered empty shell');
      }
      record.speculative = true;
      return record;
    };
    // A factory that finishes after cancellation may only clean up its own empty
    // shell. It cannot attach the view or resurrect a consumed transaction.
    pending.then(native => { if (abandoned) { try { this.#closeEmpty(recordFor(native)); } catch {} } }, () => {})
      .finally(() => { this.#shellFactories--; this.#flushCommit(); });
    try { return recordFor(await this.#wait(operation, pending)); }
    catch (error) { abandoned = true; throw error; }
  }

  async transferTab(token, { targetWindowId, targetWindowRevision, index, sender, senderFrame, validateSender, beforeCommit } = {}) {
    const lease = this.#consume(token, sender, senderFrame);
    const target = this.#window(targetWindowId);
    if (target.revision !== targetWindowRevision) throw new Error('Stale target revision');
    if (target === lease.source) throw new Error('Use reorder for the same window');
    const operation = this.#startOperation(lease);
    operation.target = target;
    try { return await this.#move(operation, index, validateSender, beforeCommit); }
    finally {
      if (this.#operation === operation) this.#operation = null;
      // Ownership is committed (or rolled back) before source disposal. The
      // transferred view is no longer in this window's tab list (§5.3.4–5).
      if (this.#onWindowEmpty) this.#closeEmpty(lease.source);
      this.#flushCommit();
    }
  }

  async detachTab(tabId, options = {}) {
    const { token } = this.beginTransfer(tabId, options);
    const lease = this.#consume(token, options.sender, options.senderFrame), operation = this.#startOperation(lease);
    let target;
    try {
      options.validateSender?.();
      target = await this.#newShell('detach', operation);
      operation.target = target;
      if (options.prepareTarget) await this.#wait(operation, options.prepareTarget(target.window));
      this.#checkOperation(operation);
      return await this.#move(operation, 0, options.validateSender, options.beforeCommit);
    } catch (error) {
      // prepareTarget can fail before #move has a chance to recover an owner
      // lost while the hidden shell/native helper was being prepared.
      if ((!target || options.prepareTarget) && !this.#liveWindow(lease.source) && this.#tabs.get(tabId) === lease.tab
        && lease.tab.ownerWindowId === lease.source.windowId
        && !lease.tab.view.webContents.isDestroyed()) await this.#recover(operation);
      throw error;
    } finally {
      if (this.#operation === operation) this.#operation = null;
      if (target && this.#tabs.get(tabId)?.ownerWindowId !== target.windowId) this.#closeEmpty(target);
      if (this.#onWindowEmpty) this.#closeEmpty(lease.source);
      this.#flushCommit();
    }
  }

  reorderTab(tabId, options = {}) {
    if (options.beforeCommit) return this.#reorderWithCommit(tabId, options);
    this.#idle();
    const { source, tab } = this.#source(tabId, options);
    if (!Number.isSafeInteger(options.index) || options.index < 0 || options.index >= source.tabIds.length) {
      throw new TypeError('Invalid reorder index');
    }
    const before = source.tabIds.indexOf(tabId);
    if (before !== options.index) {
      source.tabIds.splice(before, 1); source.tabIds.splice(options.index, 0, tabId);
      source.revision++; tab.revision++; tab.transferGeneration++;
      this.#committed();
    }
    return this.getTab(tabId);
  }

  // Preserve the synchronous API for existing command callers. Native gestures
  // opt into the same exclusion gate as transfer/close while awaiting finish.
  async #reorderWithCommit(tabId, options) {
    this.#idle();
    const { source, tab } = this.#source(tabId, options);
    if (!Number.isSafeInteger(options.index) || options.index < 0 || options.index >= source.tabIds.length)
      throw new TypeError('Invalid reorder index');
    const operation = this.#startOperation({ source, tab, sender: options.sender,
      contents: tab.view.webContents, tabRevision: tab.revision });
    operation.target = source;
    try {
      await this.#wait(operation, options.beforeCommit());
      options.validateSender?.();
      this.#checkOperation(operation);
      this.#source(tabId, options);
      const before = source.tabIds.indexOf(tabId);
      if (before !== options.index) {
        source.tabIds.splice(before, 1); source.tabIds.splice(options.index, 0, tabId);
        source.revision++; tab.revision++; tab.transferGeneration++;
        this.#committed();
      }
      return this.getTab(tabId);
    } catch (error) {
      if (!this.#liveWindow(source) && this.#tabs.get(tabId) === tab
        && !tab.view.webContents.isDestroyed()) await this.#recover(operation);
      throw error;
    } finally {
      if (this.#operation === operation) this.#operation = null;
      this.#flushCommit();
    }
  }

  async #ack(operation) {
    this.#checkOperation(operation);
    if (typeof this.#electron.waitForTransferAttach !== 'function') throw new Error('Transfer attach ACK is unavailable');
    const { tab, target } = operation, contents = tab.view.webContents;
    const ack = await this.#wait(operation,
      this.#electron.waitForTransferAttach(tab.view, target.window, { signal: operation.abort.signal }));
    this.#checkOperation(operation);
    if (ack !== true || tab.view.webContents !== contents) throw new Error('Invalid transfer attach ACK');
  }

  async #move(operation, index, validateSender, beforeCommit) {
    const { tab, source, target } = operation;
    if (index === undefined) index = target.tabIds.length;
    if (!Number.isSafeInteger(index) || index < 0 || index > target.tabIds.length) throw new TypeError('Invalid transfer index');
    const previous = this.#tabs.get(target.activeTabId);
    const sourceActive = source.activeTabId, targetActive = target.activeTabId;
    const sourceIds = [...source.tabIds], targetIds = [...target.tabIds];
    const sourceRevision = source.revision, targetRevision = target.revision;
    const wasAttached = tab.attached;
    let next, logicalChanged = false;
    try {
      validateSender?.();
      this.#checkOperation(operation);
      if (!this.#liveWindow(source)) throw new Error('Transfer source is no longer available');
      if (previous?.attached) { this.#electron.parkView(target.window, previous.view); previous.attached = false; }
      if (wasAttached) this.#electron.parkView(source.window, tab.view);
      tab.attached = false;
      this.#electron.setBounds(tab.view, this.#bounds(target));
      this.#electron.attachView(target.window, tab.view);
      await this.#ack(operation);
      validateSender?.();
      this.#checkOperation(operation);
      if (!this.#liveWindow(source) || source.revision !== sourceRevision || target.revision !== targetRevision
        || tab.revision !== operation.tabRevision) throw new Error('Stale transfer before commit');
      // A native resize need not change registry revision. Use current target
      // geometry, not the dimensions from before the asynchronous ACK.
      this.#electron.setBounds(tab.view, this.#bounds(target));
      this.#checkOperation(operation);
      // Prepare source's next visible surface before publishing any new owner.
      if (sourceActive === tab.tabId) {
        next = sourceIds.map(id => this.#tabs.get(id)).find(item => item !== tab && item?.state === 'ready');
        if (next) {
          this.#electron.setBounds(next.view, this.#bounds(source));
          // Track the attempt: addChildView may take effect and then throw.
          next.attached = true;
          this.#electron.attachView(source.window, next.view);
        }
      }
      // VS-52: terminal validation/one-shot native finish runs after attach ACK
      // and speculative native preparation, but BEFORE ownership/focus/publication.
      if (beforeCommit) {
        await this.#wait(operation, beforeCommit());
        validateSender?.();
      }
      this.#checkOperation(operation);
      if (!this.#liveWindow(source) || source.revision !== sourceRevision || target.revision !== targetRevision
        || tab.revision !== operation.tabRevision) throw new Error('Stale transfer before commit');
      // No await, notification or manifest write inside this commit. The focus
      // adapter may resolve the owner dynamically, so set ownership first and
      // roll back these fields if native focus fails (dev spec §5.3.4).
      logicalChanged = true;
      source.tabIds = sourceIds.filter(id => id !== tab.tabId);
      target.tabIds = [...targetIds]; target.tabIds.splice(index, 0, tab.tabId);
      if (sourceActive === tab.tabId) source.activeTabId = next?.tabId ?? null;
      target.activeTabId = tab.tabId; tab.ownerWindowId = target.windowId; tab.attached = true;
      this.#focus(target, tab);
      this.#checkOperation(operation);
      if (!this.#liveWindow(source)) throw new Error('Transfer source was lost during focus');
      source.revision++; target.revision++; tab.revision++; tab.transferGeneration++;
      target.speculative = false;
      this.#committed();
      return this.getTab(tab.tabId);
    } catch (error) {
      // Preserve exact logical snapshots on ordinary failure, including an
      // exception after addChildView/focus. Window loss is recovered separately.
      tab.ownerWindowId = source.windowId; tab.attached = false;
      if (logicalChanged) {
        for (const [window, ids, active] of [[source, sourceIds, sourceActive], [target, targetIds, targetActive]]) {
          if (!this.#liveWindow(window)) continue;
          window.tabIds = ids.filter(id => this.#tabs.has(id));
          window.activeTabId = window.tabIds.includes(active) ? active : null;
        }
      }
      if (this.#liveWindow(target)) { try { this.#electron.parkView(target.window, tab.view); } catch {} }
      if (next?.attached && this.#liveWindow(source)) {
        try { this.#electron.parkView(source.window, next.view); } catch {} next.attached = false;
      }
      if (previous && this.#tabs.get(previous.tabId) === previous && this.#liveWindow(target)) {
        previous.attached = false;
        try { this.#electron.setBounds(previous.view, this.#bounds(target)); this.#electron.attachView(target.window, previous.view); previous.attached = true; } catch {}
      }
      if (this.#tabs.get(tab.tabId) === tab && !tab.view.webContents.isDestroyed()) {
        let restored = false;
        if (this.#liveWindow(source)) {
          try {
            if (wasAttached) {
              this.#electron.setBounds(tab.view, this.#bounds(source)); this.#electron.attachView(source.window, tab.view); tab.attached = true;
              this.#focus(source, tab);
            }
            // Focus is native and can synchronously emit closed/destroyed.
            restored = this.#liveWindow(source);
          } catch {}
        }
        if (!restored && this.#tabs.get(tab.tabId) === tab && !tab.view.webContents.isDestroyed()) {
          await this.#recover(operation);
        }
      }
      // A contents destruction during the synchronous focus commit may have
      // removed the old active tab. Do not strand its surviving source tabs
      // after undoing the speculative native survivor attachment.
      for (const window of [source, target]) {
        if (!this.#liveWindow(window) || window.activeTabId) continue;
        const survivor = window.tabIds.map(id => this.#tabs.get(id)).find(item => item?.state === 'ready');
        if (survivor) { try { this.#selectTab(survivor); } catch {} }
      }
      throw error;
    }
  }

  async #recover(operation) {
    const { tab } = operation;
    // Detach from every surviving native host before retaining an orphan. Never
    // release root uniqueness or the strong view reference to fake recovery.
    for (const window of this.#windows.values()) {
      try { this.#electron.parkView(window.window, tab.view); } catch {}
      if (window.tabIds.includes(tab.tabId)) {
        window.tabIds = window.tabIds.filter(id => id !== tab.tabId);
        if (window.activeTabId === tab.tabId) window.activeTabId = null;
        window.revision++;
        const next = window.tabIds.map(id => this.#tabs.get(id)).find(item => item?.state === 'ready');
        if (!window.activeTabId && next) { try { this.#selectTab(next); } catch {} }
      }
    }
    tab.attached = false; tab.ownerWindowId = null; tab.state = 'recovering'; tab.revision++; tab.transferGeneration++;
    operation.source = null; operation.target = null; operation.abort = new AbortController();
    let target;
    try {
      target = await this.#newShell('recovery', operation);
      operation.target = target;
      await this.#attachRecovery(operation);
    } catch {
      // Explicit recoverTab can retry when shell creation/ACK fails. Failure
      // never destroys/remounts the retained project renderer.
      if (target) {
        try { this.#electron.parkView(target.window, tab.view); } catch {}
        this.#closeEmpty(target);
      }
    }
  }

  async #attachRecovery(operation) {
    const { tab, target } = operation;
    this.#electron.setBounds(tab.view, this.#bounds(target)); this.#electron.attachView(target.window, tab.view);
    await this.#ack(operation);
    this.#electron.setBounds(tab.view, this.#bounds(target)); this.#checkOperation(operation);
    const revision = target.revision;
    target.tabIds.push(tab.tabId); target.activeTabId = tab.tabId;
    tab.ownerWindowId = target.windowId; tab.attached = true;
    try { this.#focus(target, tab); this.#checkOperation(operation); }
    catch (error) {
      if (this.#liveWindow(target)) {
        target.tabIds = target.tabIds.filter(id => id !== tab.tabId); target.activeTabId = null; target.revision = revision;
      }
      tab.ownerWindowId = null; tab.attached = false;
      throw error;
    }
    target.revision++; tab.state = 'ready'; tab.revision++; tab.transferGeneration++;
    target.speculative = false;
    this.#committed();
    return this.getTab(tab.tabId);
  }

  async recoverTab(tabId, { windowId, windowRevision } = {}) {
    this.#idle();
    const tab = this.#tabs.get(tabId), target = this.#window(windowId);
    if (!tab || tab.state !== 'recovering' || tab.ownerWindowId !== null) throw new Error('Tab is not awaiting recovery');
    if (target.revision !== windowRevision || target.tabIds.length) throw new Error('Recovery requires a current empty shell');
    const operation = this.#startOperation({ tab, source: null }); operation.target = target;
    try { return await this.#attachRecovery(operation); }
    catch (error) { try { this.#electron.parkView(target.window, tab.view); } catch {} throw error; }
    finally {
      if (this.#operation === operation) this.#operation = null;
      this.#flushCommit();
    }
  }

  unregisterWindow(windowId) {
    const record = this.#windows.get(String(windowId));
    if (!record) return false;
    this.#windows.delete(record.windowId); // invalidate in-flight opens first
    for (const [key, lease] of this.#tokens) if (lease.source === record) this.#tokens.delete(key);
    const operation = this.#operation;
    if (operation?.kind === 'transfer' && (operation.source === record || operation.target === record)) {
      operation.abort.abort(new Error('Transfer window was lost'));
    }
    for (const [event, listener] of record.listeners) record.window.removeListener(event, listener);
    for (const tabId of [...record.tabIds]) {
      const tab = this.#tabs.get(tabId);
      // Native window loss is NOT an authorized close of the in-flight view.
      // BaseWindow does not own its contents lifetime; retain it for rollback.
      if (operation?.kind === 'transfer' && tab === operation.tab) continue;
      if (tab?.attached && !record.window.isDestroyed()) {
        try { this.#electron.parkView(record.window, tab.view); } catch {}
        tab.attached = false;
      }
      if (tab) this.#removeTab(tab, true);
    }
    if (!record.speculative) this.#committed();
    this.#flushCommit();
    return true;
  }

  dispose() {
    this.#disposed = true;
    this.#flushCommit();
    if (this.#operation?.kind === 'transfer') this.#operation.abort.abort(new Error('Desktop tabs are disposed'));
    for (const id of [...this.#windows.keys()]) this.unregisterWindow(id);
    for (const tab of [...this.#tabs.values()]) this.#removeTab(tab, true);
    this.#pending.clear();
    this.#tokens.clear();
  }
}

// The project view begins below the shared title bar (dev spec §5.3.4).
const SHELL_HEADER_HEIGHT = 44;

module.exports = { SHELL_HEADER_HEIGHT, DesktopTabs, createBackendAdapter, createElectronAdapter, canonicalProjectRoot };
