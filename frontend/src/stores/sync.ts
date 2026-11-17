/** Online sync observation and captured work fences (dev spec §16;
 * remote-identity/studio-design §5.2–5.3). No credential, socket or persistence here.
 */
import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref, shallowRef } from 'vue'

import { api, createApiClient } from '@/api/client'
import type { ProjectRemoteStatus, SyncPeer, SyncStatus, SyncUpdate } from '@/api/types'
import { ProjectSession, rendererSession, type ProjectScope } from '@/session/project-session'
import { onEvent } from '@/ws/connection'
import type { RemoteBindingChange } from '@/ws/events'

export class StaleRemoteBindingError extends Error {
  constructor() { super('Remote binding is no longer current'); this.name = 'StaleRemoteBindingError' }
}

/** Capture BEFORE the first await/debounce. Retain this exact ticket in the callback.
 * These are backend fences, not the renderer's ProjectSession revision or persistent ID.
 */
export interface RemoteBindingTicket {
  readonly runtime_scope: Readonly<ProjectScope>
  readonly backend_epoch: string
  readonly expected_binding_generation: number
  readonly assertCurrent: () => void
}

type SyncClient = Pick<ReturnType<typeof createApiClient>, 'sync' | 'updateSync' | 'projectRemote'>

export function createSyncStore(
  session: ProjectSession = rendererSession,
  client: SyncClient = session === rendererSession ? api : createApiClient(session),
  listen: typeof onEvent = onEvent,
) {
  return defineStore('sync', () => {
    const status = ref<SyncStatus | null>(null)
    const remote = shallowRef<Readonly<ProjectRemoteStatus> | null>(null)
    const bindingChange = shallowRef<Readonly<RemoteBindingChange> | null>(null)
    const saving = ref(false)
    const remotePending = ref(false)
    let revision = 0
    let bindingRevision = 0
    let remoteRequest = 0
    let compatibilityRequest = 0
    let saveRequest = 0
    let announcedGeneration: number | null = null
    let phaseSeq = -1
    let subscribed = false
    const restartRequired = ref(false)
    const tickets = new WeakSet<RemoteBindingTicket>()
    const unsubscribers: (() => void)[] = []

    const reloadRequired = computed(() => restartRequired.value)
    const live = computed(() => !remotePending.value
      // A same-G metadata read may still say connected while the barrier is
      // draining. That is not admission for NEW rooms (studio-design §5.3).
      && !(bindingChange.value?.binding_generation === remote.value?.binding_generation
        && ['draining', 'ready', 'blocked', 'paused'].includes(bindingChange.value?.state ?? ''))
      && (remote.value?.state ?? status.value?.state) === 'connected')
    const others = computed<SyncPeer[]>(() =>
      (status.value?.peers ?? []).filter(peer => !peer.self),
    )

    function observation() {
      const current = session.capture()
      const capturedRevision = revision
      return () => {
        current()
        if (capturedRevision !== revision) throw new StaleRemoteBindingError()
      }
    }

    /** Unavailable metadata is not permission to guess G, including G=0. */
    function capture(): RemoteBindingTicket {
      const currentSession = session.capture()
      const capturedRevision = bindingRevision
      const current = () => {
        currentSession()
        if (capturedRevision !== bindingRevision) throw new StaleRemoteBindingError()
      }
      const snapshot = remote.value
      const scope = session.scope
      if (!snapshot || !scope || remotePending.value || restartRequired.value) throw new StaleRemoteBindingError()
      const ticket: RemoteBindingTicket = Object.freeze({
        runtime_scope: Object.freeze({ ...scope }),
        backend_epoch: snapshot.backend_epoch,
        expected_binding_generation: snapshot.binding_generation,
        assertCurrent: current,
      })
      tickets.add(ticket)
      return ticket
    }

    /** Future I8/I13 work receives only its captured values, never latest G after await.
     * A stale/foreign ticket refuses BEFORE invoking the operation; no automatic retry.
     */
    async function run<T>(ticket: RemoteBindingTicket, operation: (captured: RemoteBindingTicket) => Promise<T>): Promise<T> {
      if (!tickets.has(ticket)) throw new StaleRemoteBindingError()
      ticket.assertCurrent()
      try {
        return await operation(ticket)
      } finally {
        // An old-G refusal is just as stale as an old-G success. Do not expose
        // its details as an error belonging to the current binding or retry it.
        ticket.assertCurrent()
      }
    }

    /** Explicit project metadata observation. No prepare/enable/editor registration on load.
     * Same-Ctx phase events invalidate the request BEFORE starting their replacement read.
     */
    async function loadRemote(): Promise<boolean> {
      const current = observation()
      if (!session.scope || restartRequired.value) return false // /remote is project-only; never fall back to global.
      const request = ++remoteRequest
      try {
        const result = await client.projectRemote()
        current()
        if (request !== remoteRequest || !session.matches(result)) return false
        // A different backend lifetime cannot be repaired by a metadata refresh.
        // Retain the old observation for display only, revoke every old ticket,
        // and never revive it from a late old-epoch reply (studio-design §5.3).
        if (remote.value && typeof result.backend_epoch === 'string' && result.backend_epoch && result.backend_epoch !== remote.value.backend_epoch) {
          restartRequired.value = true
          remotePending.value = true
          revision += 1
          bindingRevision += 1
          remoteRequest += 1
          compatibilityRequest += 1
          status.value = null
          return false
        }
        if (typeof result.backend_epoch !== 'string' || !result.backend_epoch || !Number.isSafeInteger(result.binding_generation)
          || result.binding_generation < 0
          || (remote.value && (result.backend_epoch !== remote.value.backend_epoch
            || result.binding_generation < remote.value.binding_generation))
          || (announcedGeneration !== null && result.binding_generation < announcedGeneration)) return false
        if (!remote.value || remote.value.binding_generation !== result.binding_generation) {
          revision += 1
          bindingRevision += 1
          compatibilityRequest += 1
          status.value = null
          if (bindingChange.value?.binding_generation !== result.binding_generation) bindingChange.value = null
        }
        remote.value = Object.freeze({ ...result })
        announcedGeneration = null
        remotePending.value = false
        return true
      } catch {
        // Keep the last observation. A failed phase refresh remains fenced, not connected.
        return false
      }
    }

    /** /sync is a legacy display projection, never the source of remote G. */
    async function load(): Promise<void> {
      const current = observation()
      const request = ++compatibilityRequest
      try {
        const result = await client.sync()
        current()
        if (request === compatibilityRequest) status.value = result
      } catch { /* An unreadable compatibility status is not permission to sync. */ }
    }

    function subscribe(): void {
      if (subscribed) return
      subscribed = true
      const currentSession = session.capture()
      unsubscribers.push(listen('sync.status', (payload, envelope) => {
        try { currentSession() } catch { return }
        if (!session.matches(envelope)) return
        // The old payload has no G/backend epoch: use it ONLY as a scoped hint.
        // Close new room admission immediately, including when the refresh fails.
        if (remote.value || remotePending.value) {
          revision += 1
          remotePending.value = true
          void loadRemote()
          return
        }
        compatibilityRequest += 1
        status.value = payload
      }))
      unsubscribers.push(listen('remote.binding_change', (payload, envelope) => {
        try { currentSession() } catch { return }
        if (!session.scope || !session.matches(envelope)
          || !Number.isSafeInteger(envelope.seq) || envelope.seq <= phaseSeq
          || !Number.isSafeInteger(payload.binding_generation) || payload.binding_generation < 0
          || payload.binding_generation < (announcedGeneration ?? remote.value?.binding_generation ?? 0)) return
        phaseSeq = envelope.seq
        // A same-G draining phase still permits the old editor's final save/ACK.
        // Invalidate observation reads, but leave actual admission to the backend.
        if (payload.binding_generation !== (announcedGeneration ?? remote.value?.binding_generation)) bindingRevision += 1
        announcedGeneration = payload.binding_generation
        revision += 1
        bindingChange.value = Object.freeze({ ...payload })
        remotePending.value = true
        status.value = null
        // Same Ctx, no session invalidation, rebind, socket replacement or renderer reload.
        void loadRemote()
      }))
    }

    /** Pre-activation legacy settings only. Backend enforces use_remote_settings after
     * activation; keep old callers compatible, but never retry via a new endpoint.
     */
    async function save(patch: SyncUpdate): Promise<SyncStatus> {
      const current = observation()
      const request = ++saveRequest
      saving.value = true
      try {
        const result = await client.updateSync(patch)
        current()
        if (request !== saveRequest) throw new StaleRemoteBindingError()
        compatibilityRequest += 1
        status.value = result
        return result
      } finally {
        if (request === saveRequest) saving.value = false
      }
    }

    onScopeDispose(() => {
      revision += 1
      bindingRevision += 1
      for (const unsubscribe of unsubscribers) {
        if (typeof unsubscribe === 'function') unsubscribe()
      }
    })
    return { status, remote, bindingChange, saving, remotePending, reloadRequired, live, others,
      load, loadRemote, subscribe, save, capture, run }
  })
}

export const useSyncStore = createSyncStore()
