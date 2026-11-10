/**
 * Online sync with a Voxarium Studio Remote (dev spec §16).
 *
 * What this store knows is what the backend tells it: whether this Studio is connected to
 * the project's server, as whom, and who else is there. The browser never talks to that
 * server itself — the backend does, with the user's key — so there is no socket and no
 * credential here, only a status that arrives whole (`sync.status`) whenever it changes.
 *
 * `live` is the one fact the rest of the app acts on: the documents are in step with the
 * server, so a document being opened can be opened in its room.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { SyncPeer, SyncStatus, SyncUpdate } from '@/api/types'
import { onEvent } from '@/ws/connection'

export const useSyncStore = defineStore('sync', () => {
  const status = ref<SyncStatus | null>(null)
  const saving = ref(false)
  let subscribed = false

  /** Connected and caught up — the only state live editing is offered in. */
  const live = computed(() => status.value?.state === 'connected')
  /** Everyone else connected to the space. */
  const others = computed<SyncPeer[]>(() =>
    (status.value?.peers ?? []).filter((peer) => !peer.self),
  )

  async function load(): Promise<void> {
    try {
      status.value = await api.sync()
    } catch {
      // A status that cannot be read is "not syncing", which is what `null` already says.
    }
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('sync.status', (payload) => {
      status.value = payload
    })
  }

  /** Save settings and reconnect with them. Throws what the backend refused. */
  async function save(patch: SyncUpdate): Promise<SyncStatus> {
    saving.value = true
    try {
      status.value = await api.updateSync(patch)
      return status.value
    } finally {
      saving.value = false
    }
  }

  return { status, saving, live, others, load, subscribe, save }
})
