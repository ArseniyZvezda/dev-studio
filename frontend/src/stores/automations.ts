/**
 * The project's automations (§6.5): `.voxarium_studio/automations.yaml` and what it has done.
 *
 * The list is never patched client-side. Every write answers with the whole page, a write from
 * another window arrives as `automation.changed`, and a hand edit of the file arrives the way
 * every other file change does — `explorer.file_changed` — so all three end in the same
 * re-read, and the generation counter below keeps an answer that a write overtook from
 * landing over it (the rule `stores/skills.ts` established).
 *
 * The log is the one thing that *is* patched: `automation.fired` carries a new row and
 * `automation.run_updated` the same row later — its work reporting, or ending. Snapshots keep
 * the events received while they were in flight, without retaining stale pre-gap rows.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { AutomationInput, AutomationRun, AutomationsOverview } from '@/api/types'
import { mergeRun } from '@/lib/automations'
import { rendererSession, StaleProjectResponseError } from '@/session/project-session'
import { onEvent } from '@/ws/connection'

/** Trailing, like the Skills folder's: an editor saving a file is several watcher events. */
export const AUTOMATIONS_REFRESH_DEBOUNCE_MS = 300

/** As many rows as `GET /api/automations` answers with, so a live page and a reloaded one agree. */
export const MAX_RUNS = 50

/** Queued progress can lag REST, but a run id never goes from terminal to running. */
function keepTerminal(stored: AutomationRun | undefined, incoming: AutomationRun): AutomationRun {
  return stored && stored.status !== 'running' && incoming.status === 'running' ? stored : incoming
}

export const useAutomationsStore = defineStore('automations', () => {
  const overview = ref<AutomationsOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** The automation a request is in flight for, or `''` for one that has no id yet. */
  const busy = ref<string | null>(null)

  const automations = computed(() => overview.value?.automations ?? [])
  const runs = computed(() => overview.value?.runs ?? [])

  /** The newest firing of each automation, for the line under its card. */
  const lastRun = computed(() => {
    const newest = new Map<string, AutomationRun>()
    for (const run of runs.value) if (!newest.has(run.automation_id)) newest.set(run.automation_id, run)
    return newest
  })

  let generation = 0
  let pending: Promise<void> | null = null
  let readingGeneration = 0
  let refreshRequested = false
  let writesRunning = 0
  let writesSettled: Promise<void> | null = null
  let settleWrites: (() => void) | null = null
  // Only events received DURING a snapshot win over it. Keeping the entire old log
  // would preserve the very `running` row whose terminal event the gap lost (§5.3.2).
  const snapshots = new Set<Map<string, AutomationRun>>()

  function load(): Promise<void> {
    if (pending) {
      if (readingGeneration !== generation) refreshRequested = true
      return pending
    }
    readingGeneration = generation
    // Begin journalling at the call, not in the first microtask: live events can
    // arrive immediately after the gap or while we wait for a write to finish.
    let live = new Map<string, AutomationRun>()
    snapshots.add(live)
    loading.value = true
    pending = Promise.resolve().then(async () => {
      try {
        do {
          // A pre-gap write can carry an older log too. Read AFTER it settles so
          // its delayed response cannot undo the recovered terminal state.
          while (writesSettled) await writesSettled
          const required = refreshRequested
          refreshRequested = false
          const started = generation
          readingGeneration = started
          try {
            const current = rendererSession.capture()
            const result = await api.automations()
            current()
            if (started === generation) adoptSnapshot(result, live)
            // A write overtook this read. Start after it rather than letting a gap's
            // recovery silently finish with a discarded, pre-write snapshot.
            else if (required) refreshRequested = true
          } catch (cause) {
            if (cause instanceof StaleProjectResponseError) return
            if (started === generation) error.value = cause instanceof Error ? cause.message : String(cause)
            else if (required) refreshRequested = true
          } finally {
            snapshots.delete(live)
          }
          if (refreshRequested) {
            live = new Map<string, AutomationRun>()
            snapshots.add(live)
          }
        } while (refreshRequested)
      } finally {
        // Clear synchronously with the last dirty check: a gap in a later microtask
        // must start a new request, not join an already-finished drain.
        pending = null
        loading.value = false
      }
    })
    return pending
  }

  /** Invalidate even an in-flight read; repeated gaps coalesce into one trailing read. */
  function refresh(): Promise<void> {
    refreshRequested = true
    return load()
  }

  function adoptSnapshot(result: AutomationsOverview, live: Map<string, AutomationRun>): void {
    let runs = result.runs.slice(0, MAX_RUNS).map((run) =>
      keepTerminal(overview.value?.runs.find((entry) => entry.id === run.id), run))
    for (const run of live.values()) {
      const stored = runs.find((entry) => entry.id === run.id)
      runs = mergeRun(runs, keepTerminal(stored, run), MAX_RUNS)
    }
    adopt({ ...result, runs })
  }

  function adopt(result: AutomationsOverview): void {
    generation += 1
    overview.value = result
    error.value = null
  }

  /** Run a write and take its answer as the page. `false` means refused; `error` says why. */
  let latestWrite = 0
  async function write(id: string, call: () => Promise<AutomationsOverview>): Promise<boolean> {
    const request = ++latestWrite
    generation += 1
    if (writesRunning++ === 0) {
      writesSettled = new Promise((resolve) => { settleWrites = resolve })
    }
    const live = new Map<string, AutomationRun>()
    snapshots.add(live)
    busy.value = id
    try {
      const current = rendererSession.capture()
      const result = await call()
      current()
      if (request === latestWrite) adoptSnapshot(result, live)
      return true
    } catch (cause) {
      if (!(cause instanceof StaleProjectResponseError) && request === latestWrite) {
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
      return false
    } finally {
      snapshots.delete(live)
      if (request === latestWrite) busy.value = null
      if (--writesRunning === 0) {
        writesSettled = null
        settleWrites?.()
        settleWrites = null
      }
    }
  }

  const create = (input: AutomationInput) => write('', () => api.createAutomation(input))
  const update = (id: string, input: AutomationInput) => write(id, () => api.updateAutomation(id, input))
  const setEnabled = (id: string, enabled: boolean) =>
    write(id, () => api.setAutomationEnabled(id, enabled))
  const remove = (id: string) => write(id, () => api.deleteAutomation(id))
  const clearRuns = () => write('', () => api.clearAutomationRuns())

  /**
   * The action, now. Answered once the firing has a row — in progress for long work — and the
   * row arrives as `automation.fired` like any other firing's.
   */
  async function runNow(id: string): Promise<boolean> {
    busy.value = id
    try {
      await api.runAutomation(id)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = null
    }
  }

  function putRun(run: AutomationRun): void {
    const current = overview.value
    const stored = current?.runs.find((entry) => entry.id === run.id)
    run = keepTerminal(stored, run)
    for (const live of snapshots) live.set(run.id, keepTerminal(live.get(run.id), run))
    if (current) current.runs = mergeRun(current.runs, run, MAX_RUNS)
  }

  let subscribed = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function reloadSoon(): void {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      void refresh()
    }, AUTOMATIONS_REFRESH_DEBOUNCE_MS)
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('system.dropped', (payload, envelope) => {
      // Legacy replay remains the WS adapter's job; never recover another scope.
      // Updating server data only leaves the renderer, open dialog and draft alone.
      if (rendererSession.isLegacy || !payload.resync || !rendererSession.matches(envelope)) return
      if (timer) clearTimeout(timer)
      timer = null
      return refresh()
    })
    onEvent('automation.fired', putRun)
    onEvent('automation.run_updated', putRun)
    onEvent('automation.changed', () => {
      // A remote write may follow this window's own mutation. The trailing read
      // waits for writes, rather than dropping an invalidation while busy.
      if (overview.value) reloadSoon()
    })
    onEvent('explorer.file_changed', (payload) => {
      const file = overview.value?.path
      if (!file) return
      if (payload.truncated || payload.changes.some((change) => change.path === file)) reloadSoon()
    })
    // What an action may name comes and goes with plugins: the picker's list, and the
    // problem line under a card whose plugin was just disabled.
    onEvent('plugin.changed', () => {
      if (overview.value) reloadSoon()
    })
  }

  return {
    overview,
    automations,
    runs,
    lastRun,
    loading,
    error,
    busy,
    load,
    adopt,
    create,
    update,
    setEnabled,
    remove,
    runNow,
    clearRuns,
    putRun,
    subscribe,
  }
})
