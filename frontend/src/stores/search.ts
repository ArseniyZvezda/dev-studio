/**
 * The search field in the rail (spec §6.1), and who answers it.
 *
 * The Studio indexes nothing itself: a *provider* is something a plugin registered (§9.7),
 * and the field exists only while at least one is loaded — `available` is what the rail and
 * the `Ctrl+K` shortcut read. The list follows `plugin.changed`, so enabling a plugin puts
 * the field there without a reload.
 *
 * Every keystroke is a request, and requests answer out of order on a slow provider, so
 * each carries a ticket and only the newest is allowed to land. `deep` is Enter: the
 * provider may spend seconds on it and answer in prose under its hits.
 */

import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'

import { api } from '@/api/client'
import type { SearchProviderInfo, SearchResponse } from '@/api/types'
import { onEvent } from '@/ws/connection'

export const useSearchStore = defineStore('search', () => {
  const providers = ref<SearchProviderInfo[]>([])
  const available = computed(() => providers.value.length > 0)
  const open = ref(false)
  const query = ref('')
  const response = shallowRef<SearchResponse | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  let listening = false
  let latest = 0

  async function load(): Promise<void> {
    try {
      providers.value = await api.searchProviders()
    } catch {
      // No providers is a valid answer, and the field simply stays away.
      providers.value = []
    }
    if (!listening) {
      listening = true
      onEvent('plugin.changed', () => void load())
    }
  }

  function show(): void {
    open.value = true
  }

  function hide(): void {
    open.value = false
  }

  async function run(text: string, deep = false): Promise<void> {
    query.value = text
    const trimmed = text.trim()
    const ticket = ++latest
    if (!trimmed) {
      response.value = null
      loading.value = false
      error.value = null
      return
    }
    loading.value = true
    try {
      const result = await api.search(trimmed, { deep })
      if (ticket !== latest) return
      response.value = result
      error.value = null
    } catch (cause) {
      if (ticket !== latest) return
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      if (ticket === latest) loading.value = false
    }
  }

  function clear(): void {
    latest++
    query.value = ''
    response.value = null
    loading.value = false
    error.value = null
  }

  return { providers, available, open, query, response, loading, error, load, show, hide, run, clear }
})
