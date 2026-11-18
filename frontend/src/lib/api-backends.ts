import { ref } from 'vue'
import { api } from '@/api/client'
import type { ApiBackendConnection } from '@/api/types'

/** Global metadata only; API keys never enter this shared catalogue (§10.6). */
export const apiConnections = ref<ApiBackendConnection[]>([])
let pending: Promise<void> | null = null
let loadedAt = 0
export function loadApiConnections(force = false): Promise<void> {
  if (pending) return pending
  if (Date.now() - loadedAt < 15_000 && !force) return Promise.resolve()
  pending = api.apiBackends().then(items => {
    apiConnections.value = items
    loadedAt = Date.now()
  }).catch(() => { /* A later mount retries after backend recovery. */ }).finally(() => { pending = null })
  return pending
}
