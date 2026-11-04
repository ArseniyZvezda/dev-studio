/**
 * The project's automations (§6.5): `voxarium_studio/automations.yaml` and what it has done.
 *
 * The list is never patched client-side. Every write answers with the whole page, a write from
 * another window arrives as `automation.changed`, and a hand edit of the file arrives the way
 * every other file change does — `explorer.file_changed` — so all three end in the same
 * re-read, and the generation counter below keeps an answer that a write overtook from
 * landing over it (the rule `stores/skills.ts` established).
 *
 * The log is the one thing that *is* patched: `automation.fired` carries a new row and
 * `automation.run_updated` the same row later — its work reporting, or ending — and neither is
 * anything another write could contradict (`mergeRun`).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { AutomationInput, AutomationRun, AutomationsOverview } from '@/api/types'
import { mergeRun } from '@/lib/automations'
import { onEvent } from '@/ws/connection'

/** Trailing, like the Skills folder's: an editor saving a file is several watcher events. */
export const AUTOMATIONS_REFRESH_DEBOUNCE_MS = 300

/** As many rows as `GET /api/automations` answers with, so a live page and a reloaded one agree. */
export const MAX_RUNS = 50

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
  let pending: { promise: Promise<void>; started: number } | null = null

  function load(): Promise<void> {
    if (pending && pending.started === generation) return pending.promise
    const request = { started: generation, promise: Promise.resolve() }
    pending = request
    loading.value = true
    request.promise = (async () => {
      try {
        const result = await api.automations()
        if (request.started === generation) adopt(result)
      } catch (cause) {
        error.value = cause instanceof Error ? cause.message : String(cause)
      } finally {
        if (pending === request) {
          pending = null
          loading.value = false
        }
      }
    })()
    return request.promise
  }

  function adopt(result: AutomationsOverview): void {
    generation += 1
    overview.value = result
    error.value = null
  }

  /** Run a write and take its answer as the page. `false` means refused; `error` says why. */
  async function write(id: string, call: () => Promise<AutomationsOverview>): Promise<boolean> {
    busy.value = id
    try {
      adopt(await call())
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = null
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
    if (current) current.runs = mergeRun(current.runs, run, MAX_RUNS)
  }

  let subscribed = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function reloadSoon(): void {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      void load()
    }, AUTOMATIONS_REFRESH_DEBOUNCE_MS)
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('automation.fired', putRun)
    onEvent('automation.run_updated', putRun)
    onEvent('automation.changed', () => {
      // Nobody has opened the page yet, so there is nothing on screen to correct; and while
      // a write of this window's is in flight, its own answer is the newer one.
      if (overview.value && busy.value === null) reloadSoon()
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
