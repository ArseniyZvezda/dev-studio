'use strict';
const { randomUUID } = require('node:crypto');
const { NativeTerminal } = require('./native-terminal.cjs');

const { SHELL_HEADER_HEIGHT } = require('./tabs.cjs');
const TTL = 30000, THRESHOLD = 12;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sameRenderer = (a, b) => a.contents === b.contents && a.frame === b.frame && a.generation === b.generation;
function hwnd(native) {
  const handle = native.getNativeWindowHandle();
  if (!Buffer.isBuffer(handle) || ![4, 8].includes(handle.length)) throw new Error('Invalid native window handle');
  return handle.length === 8 ? handle.readBigUInt64LE().toString() : String(handle.readUInt32LE());
}
function rect(value) {
  if (!value || !['x', 'y', 'width', 'height'].every(key => finite(value[key])) || value.width <= 0 || value.height <= 0)
    throw new Error('Invalid native geometry');
  return { x: value.x, y: value.y, width: value.width, height: value.height };
}
function inside(p, r) { return p.x >= r.x && p.x < r.x + r.width && p.y >= r.y && p.y < r.y + r.height; }

// Production native terminal coordinator, dev spec §5.3.4 (VS-52). Renderer
// geometry is advisory hit-test data, never a terminal/cursor/target authority.
// nativeTerminal injection: {request(op,payload), stop(), onTerminal, onFailure}.
// The optional append interval lies between the open button and window controls.
// Clipped visible tab subsets are accepted, in registry order.
// registerWindow returns an idempotent listener disposer; dispose is async.
function createTabDrag({ registry, screen, getWindows, authorizeSource, isMenuOpen = () => false,
  publishState = () => {}, nativeTerminal, log = () => {} }) {
  if (!registry || !screen || typeof getWindows !== 'function' || typeof authorizeSource !== 'function')
    throw new TypeError('Native tab drag dependencies are required');
  const geometry = new Map(), registered = new Map();
  let active = null, disposed = false, terminalBusy = false, disposePromise;
  const input = nativeTerminal || new NativeTerminal(log);
  const natives = () => getWindows().filter(w => !w.isDestroyed());
  const find = id => natives().find(w => String(w.id) === String(id));
  const notify = () => { try { publishState(); } catch (error) { log('drag-publish-error', { error: error.message }); } };
  function cancelActive(reason) {
    const d = active;
    if (!d) return;
    active = null; clearTimeout(d.timer);
    // Queue disarm NOW, before a subsequent begin can queue its arm. Deferring
    // this call by a microtask races a rapid cancel/new-pointerdown on readiness.
    try { Promise.resolve(input.request('cancel', { token: d.token }))
      .catch(error => log('drag-cancel-error', { error: error.message })); }
    catch (error) { log('drag-cancel-error', { error: error.message }); }
    log('drag-cancel', { reason, token: d.token }); notify();
  }
  input.onFailure = error => cancelActive(`native-helper:${error.message}`);
  input.onTerminal = record => {
    // Capture synchronously before any await, so duplicate terminals cannot run.
    if (!active || active.token !== record?.token || terminalBusy || active.terminal) return;
    active.terminal = true; terminalBusy = true;
    return terminal(active, record).catch(error => {
      log('drag-rejected', { error: error.message });
      if (active?.token === record.token) cancelActive(error.message);
      return false;
    }).finally(() => { terminalBusy = false; });
  };
  // VS-55, dev spec §5.3.4: a native HWND/window revision is not a shell
  // document identity. Reload can even reuse WebFrameMain, so pin both its
  // identity and the main-owned lifecycle generation through the last guard.
  function renderer(native) {
    const contents = native.webContents, registration = registered.get(native);
    if (!registration || contents.isDestroyed() || contents.isCrashed?.()) throw new Error('Drag shell renderer unavailable');
    const frame = contents.mainFrame;
    if (!frame || frame.isDestroyed?.()) throw new Error('Drag shell frame unavailable');
    return { contents, frame, generation: registration.generation };
  }
  function snapshot(native, requireGeometry = true) {
    const id = String(native.id), window = registry.getWindow(id), g = geometry.get(id);
    if (!window || native.isDestroyed() || !native.isVisible() || native.isMinimized() || isMenuOpen(id))
      throw new Error('Drag shell is unavailable or menu is open');
    const document = renderer(native);
    if (requireGeometry && (!g || g.native !== native || g.windowRevision !== window.revision || !sameRenderer(g.document, document)))
      throw new Error('Drag shell geometry is stale');
    const zoom = native.webContents.getZoomFactor();
    if (!finite(zoom) || zoom <= 0) throw new Error('Invalid shell zoom');
    return { id, native, document, windowRevision: window.revision, tabIds: [...window.tabIds],
      bounds: rect(native.getBounds()), content: rect(native.getContentBounds()), zoom, hwnd: hwnd(native),
      tabs: g ? g.tabs.map(t => ({ ...t })) : null, appendStart: g?.appendStart, appendEnd: g?.appendEnd };
  }
  function check(d, target) {
    if (disposed || active !== d || Date.now() >= d.expiresAt) throw new Error('Stale or cancelled drag');
    authorizeSource(d.event, d.request);
    if (d.prepared) {
      const p = d.prepared, n = p.native;
      if (n.isDestroyed() || n.isVisible() || isMenuOpen(String(n.id)) || hwnd(n) !== p.hwnd
        || !sameRenderer(renderer(n), p.document)
        || !same(rect(n.getBounds()), p.bounds) || !same(rect(n.getContentBounds()), p.content)
        || n.webContents.getZoomFactor() !== p.zoom) throw new Error('Prepared detach shell changed');
    }
    const tab = registry.getTab(d.request.tabId);
    if (!tab || tab.state !== 'ready' || tab.ownerWindowId !== d.source.id || tab.revision !== d.request.tabRevision)
      throw new Error('Drag source tab changed');
    for (const saved of new Set([d.source, target].filter(Boolean))) {
      if (find(saved.id) !== saved.native) throw new Error('Drag shell generation changed');
      const current = snapshot(saved.native);
      if (!sameRenderer(current.document, saved.document)) throw new Error('Drag shell document changed');
      for (const key of ['windowRevision', 'tabIds', 'bounds', 'content', 'zoom', 'hwnd', 'tabs', 'appendStart', 'appendEnd'])
        if (!same(current[key], saved[key])) throw new Error(`Drag shell ${key} changed`);
    }
  }
  function hit(shell, point) {
    const p = { x: point.x - shell.content.x, y: point.y - shell.content.y };
    // The native header inset is in DIP; reported button geometry is in CSS pixels.
    if (p.y < 0 || p.y >= SHELL_HEADER_HEIGHT || p.x < 0 || p.x >= shell.content.width) return null;
    if (!shell.tabs) return null;
    const boxes = shell.tabs.map(t => ({ ...t, x: t.x * shell.zoom, y: t.y * shell.zoom,
      width: t.width * shell.zoom, height: t.height * shell.zoom }));
    const tab = boxes.find(t => inside(p, t));
    if (tab) return { index: shell.tabIds.indexOf(tab.tabId) + (p.x >= tab.x + tab.width / 2 ? 1 : 0) };
    // Geometry must explicitly prove the empty trailing zone. Tab rectangles
    // alone cannot locate inline detach/+ controls, pending items or error text.
    return finite(shell.appendStart) && p.x >= shell.appendStart * shell.zoom && p.x < shell.appendEnd * shell.zoom
      ? { index: shell.tabIds.length } : null;
  }
  async function terminal(d, record) {
    if (record.kind !== 'up' || record.provenance !== 'native-hook' || !record.point
      || !Number.isSafeInteger(record.point.x) || !Number.isSafeInteger(record.point.y) || !record.target
      || !/^[1-9][0-9]*$/.test(record.target.hwnd) || !Number.isSafeInteger(record.target.pid) || record.target.pid <= 0)
      throw new Error('Unsupported native terminal');
    check(d);
    const point = screen.screenToDipPoint(record.point);
    if (!point || !finite(point.x) || !finite(point.y)) throw new Error('Invalid terminal DIP point');
    if (Math.hypot(point.x - d.point.x, point.y - d.point.y) < THRESHOLD) throw new Error('Click without drag');
    const shell = d.shells.find(w => w.hwnd === record.target.hwnd);
    // A registered but unsupported/hidden/menu shell is NOT foreign backdrop.
    const known = natives().find(w => hwnd(w) === record.target.hwnd);
    let target, index;
    if (shell) {
      const zone = hit(shell, point);
      if (!zone) throw new Error('Unsupported shell surface');
      target = shell; index = zone.index; check(d, target);
    } else if (known || record.target.pid === process.pid
      || /^(Shell_TrayWnd|Shell_SecondaryTrayWnd|#32768|#32770)$/.test(record.target.className || '')
      || typeof record.target.className !== 'string' || !record.target.className)
      throw new Error('Unsupported native target');
    const guard = () => check(d, target);
    const commit = async () => {
      guard();
      const valid = await input.request('validate', { token: d.token });
      if (valid.valid !== true) throw new Error(`Native terminal invalidated: ${valid.reason}`);
      guard();
      const finish = await input.request('finish', { token: d.token });
      if (finish.valid !== true || finish.consumed !== true) throw new Error(`Native terminal finish invalidated: ${finish.reason}`);
      guard();
      d.finished = true;
    };
    const options = { ...d.request, sender: d.event.sender, senderFrame: d.event.senderFrame,
      validateSender: guard, beforeCommit: commit };
    if (target?.id === d.source.id) {
      const before = d.source.tabIds.indexOf(d.request.tabId);
      const reorderIndex = Math.max(0, Math.min(d.source.tabIds.length - 1, index > before ? index - 1 : index));
      await registry.reorderTab(d.request.tabId, { ...options, index: reorderIndex });
    } else if (target) {
      const lease = registry.beginTransfer(d.request.tabId, options);
      await registry.transferTab(lease.token, { ...options, targetWindowId: target.id,
        targetWindowRevision: target.windowRevision, index });
    } else {
      const work = rect(screen.getDisplayNearestPoint(point).workArea);
      const width = Math.min(d.source.bounds.width, work.width), height = Math.min(d.source.bounds.height, work.height);
      const bounds = { x: Math.round(Math.max(work.x, Math.min(point.x - 80, work.x + work.width - width))),
        y: Math.round(Math.max(work.y, Math.min(point.y - SHELL_HEADER_HEIGHT / 2, work.y + work.height - height))),
        width: Math.floor(width), height: Math.floor(height) };
      await registry.detachTab(d.request.tabId, { ...options, prepareTarget: async native => {
        guard();
        if (native.isDestroyed() || native.isVisible()) throw new Error('Detach shell must be hidden');
        const document = renderer(native);
        // The normal shell minimum can exceed a small/portrait workArea.
        // Relax only the new hidden shell, never move/resize an existing target.
        if (native.getMinimumSize && native.setMinimumSize) {
          const minimum = native.getMinimumSize();
          native.setMinimumSize(Math.min(minimum[0], bounds.width), Math.min(minimum[1], bounds.height));
        }
        native.setBounds(bounds);
        d.prepared = { native, document, bounds, content: rect(native.getContentBounds()),
          zoom: native.webContents.getZoomFactor(), hwnd: hwnd(native) };
        await input.request('prepare', { token: d.token, hwnd: d.prepared.hwnd, pid: process.pid });
        guard();
        if (native.isDestroyed() || native.isVisible() || !same(rect(native.getBounds()), bounds))
          throw new Error('Prepared detach shell changed');
      } });
    }
    if (active === d) { active = null; clearTimeout(d.timer); }
    log('drag-commit', { token: d.token, target: registry.getTab(d.request.tabId)?.ownerWindowId }); notify();
    return true;
  }
  return Object.freeze({
    setGeometry(event, request) {
      if (disposed) throw new Error('Drag coordinator disposed');
      authorizeSource(event, request);
      const w = registry.getWindow(request?.windowId), native = find(request?.windowId);
      if (!w || !native || w.revision !== request.windowRevision || !Array.isArray(request.tabs)
        || request.tabs.length > w.tabIds.length || request.tabs.length > 1000) throw new Error('Invalid or stale tab geometry');
      let previousIndex = -1;
      const tabs = request.tabs.map(t => {
        const index = w.tabIds.indexOf(t?.tabId);
        if (!t || index < 0 || index <= previousIndex || !['x', 'y', 'width', 'height'].every(k => finite(t[k]))
          || t.width <= 0 || t.height <= 0 || [t.x, t.y, t.width, t.height].some(v => Math.abs(v) > 1e7))
          throw new Error('Invalid tab rectangle');
        previousIndex = index;
        return { tabId: t.tabId, x: t.x, y: t.y, width: t.width, height: t.height };
      });
      for (let i = 1; i < tabs.length; i++)
        if (tabs[i].x < tabs[i - 1].x + tabs[i - 1].width - 1) throw new Error('Overlapping tab geometry');
      const { appendStart, appendEnd } = request;
      if ((appendStart !== undefined || appendEnd !== undefined) && (!finite(appendStart) || !finite(appendEnd)
        || appendStart < 0 || appendEnd <= appendStart || appendEnd > 1e7
        || tabs.some(t => appendStart < t.x + t.width))) throw new Error('Invalid trailing strip boundary');
      geometry.set(w.windowId, { native, document: renderer(native), windowRevision: w.revision, tabs, appendStart, appendEnd });
      return { ok: true };
    },
    async begin(event, request) {
      if (disposed || active || terminalBusy) throw new Error('Native tab drag is busy or disposed');
      authorizeSource(event, request);
      if (!request?.point || !finite(request.point.x) || !finite(request.point.y)) throw new Error('Invalid drag point');
      const native = find(request.windowId), source = native && snapshot(native);
      const tab = registry.getTab(request.tabId);
      if (!source || source.windowRevision !== request.windowRevision || !tab || tab.ownerWindowId !== source.id
        || tab.state !== 'ready' || tab.revision !== request.tabRevision || !native.isFocused()) throw new Error('Stale drag source');
      const point = { x: source.content.x + request.point.x * source.zoom, y: source.content.y + request.point.y * source.zoom };
      const tabBox = source.tabs.find(t => t.tabId === request.tabId);
      if (!tabBox || !inside(request.point, tabBox) || point.y < source.content.y || point.y >= source.content.y + SHELL_HEADER_HEIGHT)
        throw new Error('Drag must begin on a tab');
      const shells = natives().flatMap(w => { try { return [snapshot(w)]; } catch { return []; } });
      const d = { source, shells, event, request: { windowId: source.id, windowRevision: request.windowRevision,
        tabId: request.tabId, tabRevision: request.tabRevision }, point, token: randomUUID(), expiresAt: Date.now() + TTL };
      active = d;
      d.timer = setTimeout(() => { if (active === d) cancelActive('expired'); }, TTL); d.timer.unref?.();
      try {
        await input.request('arm', { token: d.token, synthetic: false });
        check(d); notify(); return { token: d.token, expiresAt: d.expiresAt };
      } catch (error) { if (active === d) cancelActive(error.message); throw error; }
    },
    cancel(event, request) {
      if (disposed) return { cancelled: false };
      // Role/origin/main-frame authorization still applies. Revisions do NOT:
      // geometry/activation may already have invalidated this source gesture.
      authorizeSource(event, request);
      if (!active || request?.token !== active.token || String(request.windowId) !== active.source.id
        || event.sender !== active.event.sender || event.senderFrame !== active.event.senderFrame) return { cancelled: false };
      cancelActive('source-cancel'); return { cancelled: true };
    },
    registerWindow(native) {
      const id = String(native.id);
      if (disposed || native.isDestroyed() || registered.has(native)) throw new Error('Drag window already registered or disposed');
      const registration = { generation: 0, release: null };
      const listeners = [];
      const listen = (object, name, fn) => { object.on(name, fn); listeners.push([object, name, fn]); };
      const invalidate = () => {
        if (active && !active.finished && (active.shells.some(w => w.native === native) || active.prepared?.native === native))
          cancelActive('native-window-changed');
      };
      const invalidateDocument = () => {
        registration.generation++;
        if (geometry.get(id)?.native === native) geometry.delete(id);
        invalidate();
      };
      for (const event of ['move', 'resize', 'maximize', 'unmaximize', 'minimize', 'hide']) listen(native, event, invalidate);
      listen(native, 'blur', () => { if (active?.source.native === native && !active.finished) cancelActive('source-blur'); });
      listen(native.webContents, 'before-input-event', (_event, key) => { if (key.key === 'Escape' && active?.source.native === native) cancelActive('Escape'); });
      listen(native.webContents, 'will-navigate', invalidateDocument);
      // Unlike will-navigate, this also covers programmatic loadURL/reload.
      // The initial hidden detach shell load precedes prepareTarget; it must
      // not cancel the gesture until that shell is actually participating.
      listen(native.webContents, 'did-start-navigation', (_event, _url, inPlace, mainFrame) => {
        if (mainFrame && !inPlace) invalidateDocument();
      });
      listen(native.webContents, 'render-process-gone', invalidateDocument);
      listen(native.webContents, 'destroyed', invalidateDocument);
      const release = () => {
        if (!registered.delete(native)) return;
        invalidate(); if (geometry.get(id)?.native === native) geometry.delete(id);
        for (const [object, name, fn] of listeners) object.removeListener(name, fn);
      };
      registration.release = release;
      registered.set(native, registration); listen(native, 'closed', release); return release;
    },
    dispose() {
      if (disposePromise) return disposePromise;
      disposed = true; cancelActive('disposed');
      for (const { release } of [...registered.values()]) release(); geometry.clear();
      return disposePromise = Promise.resolve().then(() => input.stop()).catch(error => log('drag-stop-error', { error: error.message }));
    },
  });
}
module.exports = { createTabDrag };
