'use strict';

const { ORIGIN } = require('./policy.cjs');

const sameBinding = (a, b) => a?.root === b?.root && a?.project_id === b?.project_id && a?.epoch === b?.epoch;
const validCount = value => Number.isSafeInteger(value) && value >= 0;
function copyCounts(value) {
  return value && validCount(value.in_progress) && validCount(value.needs_review)
    ? Object.freeze({ in_progress: value.in_progress, needs_review: value.needs_review }) : null;
}
const sameCounts = (a, b) => a === b || (!!a && !!b
  && a.in_progress === b.in_progress && a.needs_review === b.needs_review);

/** Main-owned, read-only summaries for ALL open tabs (dev spec §5.3.2–5.3.4).
 * No project stores, legacy selection, registry writes or topology revisions.
 * Construction is inert; start is idempotent and stop is terminal. refresh only
 * reconciles topology and promptly polls NEW bindings, so it is safe inside the
 * IPC beforePublish seam, including publications caused by onChange itself.
 */
function createTaskCounts({ fetch: request = globalThis.fetch, getTabs, onChange = () => {}, token,
  timers = { setTimeout, clearTimeout }, intervalMs = 3000, timeoutMs = 10000 } = {}) {
  if (typeof request !== 'function' || typeof getTabs !== 'function' || typeof onChange !== 'function'
    || typeof timers.setTimeout !== 'function' || typeof timers.clearTimeout !== 'function'
    || !Number.isSafeInteger(intervalMs) || intervalMs <= 0
    || !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) throw new TypeError('Invalid task counts options');
  const entries = new Map();
  let started = false, stopped = false, timer = null;

  function cancel(entry) {
    entry.controller?.abort();
    if (entry.timeout !== null) timers.clearTimeout(entry.timeout);
    entry.timeout = null;
  }
  function current(entry) {
    return !stopped && entries.get(entry.tabId) === entry && getTabs().some(tab =>
      tab.tabId === entry.tabId && sameBinding(tab.project, entry.binding));
  }
  async function poll(entry) {
    if (entry.pending || !current(entry)) return;
    entry.pending = true;
    const controller = new AbortController();
    entry.controller = controller;
    entry.timeout = timers.setTimeout(() => controller.abort(), timeoutMs);
    entry.timeout?.unref?.();
    let counts = null;
    try {
      const secret = typeof token === 'function' ? token() : token;
      const response = await request(entry.url, { method: 'GET', redirect: 'error', cache: 'no-store',
        headers: secret ? { Authorization: `Bearer ${secret}` } : {}, signal: controller.signal });
      if (response.ok) counts = copyCounts(await response.json());
      if (controller.signal.aborted) counts = null;
    } catch { /* Unavailable/malformed is unknown, never a synthetic zero. */ }
    finally {
      if (entry.timeout !== null) timers.clearTimeout(entry.timeout);
      entry.timeout = null; entry.controller = null; entry.pending = false;
    }
    // Even a fetch implementation that ignores abort cannot resurrect a closed
    // tab, a replacement binding, an expired epoch, or a stopped service.
    if (!current(entry)) return;
    if (!sameCounts(entry.counts, counts)) {
      entry.counts = counts;
      try { onChange(); } catch { /* Observers cannot stop polling/cleanup. */ }
    }
  }
  function refresh() {
    if (!started || stopped) return;
    const tabs = getTabs();
    for (const [id, entry] of entries) {
      if (!tabs.some(tab => tab.tabId === id && sameBinding(tab.project, entry.binding))) {
        entries.delete(id); cancel(entry);
      }
    }
    for (const tab of tabs) {
      const project = tab.project;
      if (entries.has(tab.tabId) || typeof tab.tabId !== 'string'
        || !project || typeof project.root !== 'string' || !project.root
        || typeof project.project_id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(project.project_id)
        || typeof project.epoch !== 'string' || !project.epoch) continue;
      const binding = Object.freeze({ root: project.root, project_id: project.project_id, epoch: project.epoch });
      const url = `${ORIGIN}/api/projects/${binding.project_id}/project/task-counts?epoch=${encodeURIComponent(binding.epoch)}`;
      const entry = { tabId: tab.tabId, binding, url, counts: null, pending: false, controller: null, timeout: null };
      entries.set(tab.tabId, entry);
      void poll(entry);
    }
  }
  function schedule() {
    timer = timers.setTimeout(() => {
      timer = null;
      if (stopped) return;
      refresh();
      for (const entry of entries.values()) void poll(entry);
      schedule();
    }, intervalMs);
    timer?.unref?.();
  }
  return Object.freeze({
    start() { if (started || stopped) return; started = true; refresh(); schedule(); },
    refresh,
    getCounts(tab) {
      const entry = entries.get(tab?.tabId);
      return entry && sameBinding(tab.project, entry.binding) && current(entry) ? entry.counts : null;
    },
    stop() {
      if (stopped) return;
      stopped = true;
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
      for (const entry of entries.values()) cancel(entry);
      entries.clear();
    },
  });
}

module.exports = { createTaskCounts };
