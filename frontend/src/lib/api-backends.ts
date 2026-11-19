import { ref } from 'vue'
import { api } from '@/api/client'
import type { ApiBackendConnection } from '@/api/types'

/** Global metadata only; API keys never enter this shared catalogue (§10.6). */
export const apiConnections = ref<ApiBackendConnection[]>([])
let pending: Promise<void> | null = null
let loadedAt = 0
export function loadApiConnections(force = false): Promise<void> {
  if (!force && pending) return pending
  if (!force && Date.now() - loadedAt < 15_000) return Promise.resolve()
  const request = api.apiBackends().then(items => {
    // A forced read after a mutation supersedes any snapshot requested before it.
    if (pending === request) {
      apiConnections.value = items
      loadedAt = Date.now()
    }
  }).catch(() => { /* A later mount retries after backend recovery. */ }).finally(() => {
    if (pending === request) pending = null
  })
  pending = request
  return request
}
