/**
 * Project memory (§10.12): `.voxarium_studio/memory/` and the index agents are sent.
 *
 * Kept live off two events: `team.memory.changed`, which every write through the Studio sends —
 * an agent's `remember` as much as the page's own save — and `explorer.file_changed` for the
 * edits nobody told the Studio about (a text editor, a checkout). A burst of the latter is one
 * change, so that re-read waits for it to stop.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '@/api/client'
import type { MemoryOverview } from '@/api/types'
import { inMemoryFolder } from '@/lib/memory'
import { onEvent } from '@/ws/connection'

export const MEMORY_REFRESH_DEBOUNCE_MS = 300

export const useMemoryStore = defineStore('memory', () => {
  const overview = ref<MemoryOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** Bumped by every answer adopted, so a read that started before a write cannot land after it. */
  let generation = 0
  let pending: { promise: Promise<void>; started: number } | null = null

  function load(): Promise<void> {
    if (pending && pending.started === generation) return pending.promise
    const request = { started: generation, promise: Promise.resolve() }
    pending = request
    loading.value = true
    request.promise = (async () => {
      try {
        const result = await api.memory()
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

  function adopt(result: MemoryOverview): void {
    generation += 1
    overview.value = result
    error.value = null
  }

  /** Read again after a write that answered with one entry rather than the page. */
  function refresh(): Promise<void> {
    generation += 1
    return load()
  }

  let subscribed = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function scheduleLoad(): void {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      void refresh()
    }, MEMORY_REFRESH_DEBOUNCE_MS)
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('team.memory.changed', () => {
      if (overview.value) scheduleLoad()
    })
    onEvent('explorer.file_changed', (payload) => {
      const folder = overview.value?.path
      if (!folder) return
      const touched = payload.changes.some((change) => inMemoryFolder(change.path, folder))
      if (touched || payload.truncated) scheduleLoad()
    })
  }

  return { overview, loading, error, load, adopt, refresh, subscribe }
})
