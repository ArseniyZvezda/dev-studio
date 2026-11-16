'use strict';

// App-lifetime transport only (dev spec §5.3.6, VS-61). No Electron, window,
// selected project, project registry, notification UI or backend lifecycle here.
const path = require('node:path');
const { ORIGIN } = require('./policy.cjs');

const PAGE = /^\/(?:explorer|tasks|producer|teams|chats|documents(?:\/graph)?|pipelines(?:\/[A-Za-z0-9_-]+)?|automations|vcs|memory|skills|mcp|plugins|project-settings|statistics)$/;
const SCOPE_KEYS = new Set(['project_id', 'epoch', 'root', 'project']);
const CONTROL = /[\x00-\x1f\x7f]/;
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const sequence = value => Number.isSafeInteger(value) && value >= 0;
const label = value => typeof value === 'string' && !!value.trim() && !CONTROL.test(value);

/** Return the unchanged project-relative page, or null. Keep in sync with
 * backend ws/events.py desktop_target; selectors must never rebind a project.
 * Encoded query VALUES (file paths, chat names, etc.) are data, not URL paths.
 */
function validateDesktopTarget(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')
    || CONTROL.test(value) || value.includes('\\') || value.includes('#')) return null;
  const pathname = value.split('?', 1)[0];
  if (!PAGE.test(pathname)) return null; // Also rejects encoded separators/dot segments.
  try {
    const url = new URL(value, ORIGIN);
    if (url.origin !== ORIGIN || url.pathname !== pathname) return null;
    for (const key of url.searchParams.keys()) if (SCOPE_KEYS.has(key)) return null;
    return value;
  } catch { return null; }
}

function project(value, epoch) {
  if (!record(value) || !label(value.project_id)
    || !/^[A-Za-z0-9_-]+$/.test(value.project_id) || value.epoch !== epoch
    || !label(value.name) || !label(value.root) || value.root !== value.root.trim()) return false;
  // The locator must be absolute, not a route, URL, drive-relative or CWD-relative
  // name. Do not stat/resolve it: locating/opening a project belongs to main.
  const root = value.root;
  if (!path.posix.isAbsolute(root) && !/^[A-Za-z]:[\\/]/.test(root)
    && !/^\\\\[^\\/]+[\\/][^\\/]+/.test(root)) return false;
  return !root.split(/[\\/]/).some(part => part === '.' || part === '..');
}

function source(value, epoch) {
  return record(value) && project(value.project, epoch) && sequence(value.source_seq);
}

function notice(value, epoch) {
  if (!source(value, epoch) || !(value.target === null || validateDesktopTarget(value.target))
    || !record(value.notification)) return false;
  const data = value.notification;
  return ['automation_id', 'automation_name', 'title', 'message', 'trigger']
    .every(key => typeof data[key] === 'string') && record(data.values);
}

function attention(value, epoch) {
  if (!source(value, epoch) || value.target !== '/producer' || !Array.isArray(value.waiting)) return false;
  const chats = new Set();
  for (const entry of value.waiting) {
    if (!record(entry) || !label(entry.chat_id) || !validateDesktopTarget(entry.target)) return false;
    const url = new URL(entry.target, ORIGIN);
    if (url.pathname !== '/producer' || url.searchParams.size !== 1
      || url.searchParams.get('chat') !== entry.chat_id || chats.has(entry.chat_id)) return false;
    chats.add(entry.chat_id);
  }
  return true;
}

function snapshot(values, epoch) {
  if (!Array.isArray(values)) return false;
  const projects = new Set();
  return values.every(value => {
    if (!attention(value, epoch) || projects.has(value.project.project_id)) return false;
    projects.add(value.project.project_id);
    return true;
  });
}

function freeze(value) {
  // Payloads originate in JSON, so cannot contain cycles. Iterative to tolerate
  // deeply nested notification values without consuming the JS call stack.
  const pending = [value];
  while (pending.length) {
    const item = pending.pop();
    if (item && typeof item === 'object') {
      for (const value of Object.values(item)) if (value && typeof value === 'object') pending.push(value);
      Object.freeze(item);
    }
  }
  return value;
}

/**
 * createProjectEventReceiver({ WebSocket = globalThis.WebSocket, token,
 *   timers = { setTimeout, clearTimeout }, reconnectDelayMs = 1000,
 *   onNotification, onAttention, onGap, onError })
 *
 * Construction is inert; start() connects once, dispose() is terminal. Neither
 * closes windows, clears external UI state, shuts down contexts nor stops jobs.
 * token may be a string or a synchronous getter (read on every connection).
 *
 * onNotification(NOTICE, {epoch, seq}); onGap(droppedData, {epoch, seq});
 * onAttention(ATTENTION[], {epoch, seq, snapshot}) supplies the COMPLETE current
 * set of projects with waiting chats after each replacement/clear or snapshot.
 * The receiver only retains this attention set plus scalar app cursor/watermark;
 * IDs/source_seq are NOT event identity and notifications are never retained.
 * Callback arguments are immutable. onError(Error) isolates transport/callback
 * failures; malformed/foreign frames are silently ignored, without cursor moves.
 */
function createProjectEventReceiver(options = {}) {
  const Socket = options.WebSocket ?? globalThis.WebSocket;
  const timers = options.timers ?? { setTimeout, clearTimeout };
  const delay = options.reconnectDelayMs ?? 1000;
  if (!Number.isFinite(delay) || delay < 0) throw new TypeError('Invalid reconnect delay');
  if (typeof timers.setTimeout !== 'function' || typeof timers.clearTimeout !== 'function') {
    throw new TypeError('Invalid timers');
  }
  let started = false, disposed = false, socket = null, timer = null, generation = 0;
  let epoch = null, cursor = null, attentionWatermark = 0;
  let waiting = new Map(), detach = () => {};

  function error(value) {
    if (disposed) return;
    try { options.onError?.(value instanceof Error ? value : new Error('Desktop event transport failed')); }
    catch { /* An observer cannot break reconnect/cleanup. */ }
  }
  function emit(name, data, meta) {
    if (disposed) return;
    try { options[name]?.(data, Object.freeze(meta)); } catch (value) { error(value); }
  }
  function emitAttention(seq, isSnapshot) {
    emit('onAttention', Object.freeze([...waiting.values()]), { epoch, seq, snapshot: isSnapshot });
  }
  function replaceAttention(values, seq) {
    waiting = new Map(values.filter(value => value.waiting.length)
      .map(value => [value.project.project_id, value]));
    attentionWatermark = seq;
    emitAttention(seq, true);
  }
  function retire() {
    ++generation; // Invalidates even already-queued callbacks from this socket.
    const previous = socket;
    socket = null;
    detach();
    detach = () => {};
    if (previous) {
      try { previous.close(); } catch { /* Already closed/connecting transports. */ }
    }
  }
  function reconnect() {
    if (disposed || timer !== null) return;
    timer = timers.setTimeout(() => {
      timer = null;
      if (!disposed) connect();
    }, delay);
  }
  function connect() {
    if (disposed || socket) return;
    const turn = ++generation;
    const requestedEpoch = epoch, requestedCursor = cursor;
    let hello = false;
    const current = () => !disposed && generation === turn;
    function failed(value) {
      if (!current()) return;
      retire();
      reconnect();
      if (value) error(value);
    }
    function receive(event) {
      if (!current()) return;
      let frame;
      try {
        const raw = typeof event === 'string' || Buffer.isBuffer(event) ? event : event?.data;
        if (typeof raw !== 'string' && !Buffer.isBuffer(raw)) return;
        frame = JSON.parse(raw.toString());
      } catch { return; }
      if (!record(frame) || !record(frame.data) || 'project_id' in frame) return;
      const data = frame.data;
      if (frame.type === 'system.hello') {
        // Only the first valid hello on this connection may establish its scope.
        // An unsolicited second hello can never rotate/reset the cursor.
        if (hello || !label(frame.epoch) || data.epoch !== frame.epoch || !sequence(frame.seq)
          || !(data.resumed_from === null || sequence(data.resumed_from))) return;
        const fresh = data.resumed_from === null;
        if (fresh ? !snapshot(data.attention, frame.epoch)
          : data.resumed_from > frame.seq || requestedEpoch !== frame.epoch
            || requestedCursor !== data.resumed_from || 'attention' in data) return;
        freeze(data);
        hello = true;
        if (epoch !== frame.epoch) {
          epoch = frame.epoch;
          cursor = null;
          attentionWatermark = 0;
        }
        if (fresh) {
          cursor = frame.seq;
          replaceAttention(data.attention, frame.seq);
        }
        // A resumed hello's seq is the HEAD, not the delivered cursor. Replay
        // follows hello and must still deliver notifications up to that head.
        return;
      }
      if (!hello || frame.epoch !== epoch) return;
      if (frame.type === 'system.pong') return;
      if (frame.type === 'system.dropped') {
        if (data.resync !== true || !label(data.reason) || !sequence(data.count)
          || !sequence(data.snapshot_seq) || data.snapshot_seq < attentionWatermark
          || ('seq' in frame && (!sequence(frame.seq) || frame.seq === 0
            || frame.seq <= cursor || frame.seq > data.snapshot_seq))
          || !snapshot(data.attention, epoch)) return;
        freeze(data);
        // A source-overflow marker may itself be sequenced on the app bus.
        // Dedup/remember that marker, never its newer snapshot watermark.
        if ('seq' in frame) cursor = frame.seq;
        replaceAttention(data.attention, data.snapshot_seq);
        emit('onGap', data, { epoch, seq: data.snapshot_seq });
        // Do NOT advance cursor to snapshot_seq: remaining queued notifications
        // before that watermark still matter. Only older attention is suppressed.
        return;
      }
      const notification = frame.type === 'desktop.notification';
      if (!notification && frame.type !== 'desktop.producer_attention') return;
      if (!sequence(frame.seq) || frame.seq === 0 || frame.seq <= cursor
        || !(notification ? notice(data, epoch) : attention(data, epoch))) return;
      freeze(data);
      cursor = frame.seq;
      if (notification) emit('onNotification', data, { epoch, seq: frame.seq });
      else if (frame.seq > attentionWatermark) {
        if (data.waiting.length) waiting.set(data.project.project_id, data);
        else waiting.delete(data.project.project_id);
        attentionWatermark = frame.seq;
        emitAttention(frame.seq, false);
      }
    }
    try {
      const url = new URL('/ws/desktop', ORIGIN);
      url.protocol = 'ws:';
      const secret = typeof options.token === 'function' ? options.token() : options.token;
      if (secret) url.searchParams.set('token', secret);
      if (requestedEpoch !== null && requestedCursor !== null) {
        url.searchParams.set('epoch', requestedEpoch);
        url.searchParams.set('since_seq', String(requestedCursor));
      }
      if (typeof Socket !== 'function') throw new TypeError('WebSocket is unavailable');
      const next = new Socket(url.href);
      socket = next;
      const handlers = { message: receive, close: () => failed(), error: () => failed(new Error('Desktop event transport failed')) };
      const removers = [];
      // Set detach first so even partial listener installation is reversible.
      detach = () => { for (const remove of removers) remove(); };
      for (const [name, handler] of Object.entries(handlers)) {
        if (typeof next.addEventListener === 'function' && typeof next.removeEventListener === 'function') {
          next.addEventListener(name, handler);
          removers.push(() => next.removeEventListener(name, handler));
        } else if (typeof next.on === 'function' && typeof next.removeListener === 'function') {
          next.on(name, handler);
          removers.push(() => next.removeListener(name, handler));
        } else throw new TypeError('WebSocket must support removable event listeners');
      }
    } catch (value) { failed(value); }
  }
  const api = Object.freeze({
    start() {
      if (!started && !disposed) { started = true; connect(); }
      return api;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
      retire();
      waiting.clear();
    },
  });
  return api;
}

module.exports = { createProjectEventReceiver, validateDesktopTarget };
