/** Project-renderer side of the close transaction (dev spec §5.3.5).
 * No native disposal, navigation, room leave, or state serialisation lives here.
 */
import { onScopeDispose } from 'vue'
import type { DesktopTabsState } from '@/desktop'

export type CloseGuard = () => boolean | Promise<boolean>
export type CloseValidation = () => boolean

export function createCloseBarrier() {
  const guards = new Set<{ prepare: CloseGuard; validate: CloseValidation }>()
  let generation = 0
  let approvedGeneration: number | null = null
  let pending: Promise<boolean> | null = null
  return {
    register(prepare: CloseGuard, validate: CloseValidation = () => true): () => void {
      const guard = { prepare, validate }
      guards.add(guard)
      generation += 1
      return () => { if (guards.delete(guard)) generation += 1 }
    },
    prepare(): Promise<boolean> {
      if (pending) return pending
      const captured = generation
      approvedGeneration = null
      // Defer invocation until pending is installed, including synchronous guards.
      pending = Promise.resolve().then(async () => {
        try {
          for (const guard of [...guards]) {
            if (generation !== captured || await guard.prepare() !== true) return false
          }
          const prepared = generation === captured && [...guards].every(guard => guard.validate() === true)
          if (prepared) approvedGeneration = captured
          return prepared
        } catch { return false }
      }).finally(() => { pending = null })
      return pending
    },
    /** Recheck synchronously after transport awaits: a new edit/save is not covered by an old approval. */
    validate(): boolean {
      try { return approvedGeneration === generation && [...guards].every(guard => guard.validate() === true) } catch { return false }
    },
  }
}

/** One module instance per live project renderer, not per window or active tab. */
export const rendererCloseBarrier = createCloseBarrier()

/** Register only while the owning editor/form is mounted. */
export function useCloseGuard(guard: CloseGuard, validate?: CloseValidation): void {
  onScopeDispose(rendererCloseBarrier.register(guard, validate))
}

export interface RendererCloseRequest {
  readonly requestId: string
  readonly tabId: string
  /** Main's captured tab revision/generation; never refreshed into a new permission. */
  readonly tabRevision: number
  readonly project_id: string
  readonly epoch: string
  readonly phase: 'prepare' | 'validate' | 'release'
}
export interface RendererCloseAck extends RendererCloseRequest { readonly prepared: boolean }

/** Transport seam for the separately implemented main/preload close controller.
 * Main must match the whole tuple plus sender contents/frame before consuming an ACK.
 * The existing shell prepareClose invoke is NOT a renderer ACK or permission to destroy.
 */
export interface RendererCloseBridge {
  getState(): Promise<DesktopTabsState>
  onPrepareClose(callback: (request: RendererCloseRequest) => void): () => void
  acknowledgeClose(ack: RendererCloseAck): Promise<void>
}

function validRequest(value: RendererCloseRequest): boolean {
  return !!value && typeof value.requestId === 'string' && value.requestId.length > 0
    && value.requestId.length <= 128 && typeof value.tabId === 'string' && !!value.tabId
    && typeof value.project_id === 'string' && !!value.project_id
    && typeof value.epoch === 'string' && !!value.epoch
    && Number.isSafeInteger(value.tabRevision) && value.tabRevision >= 0
    && ['prepare', 'validate', 'release'].includes(value.phase)
}

/** A short-lived user-input fence, NOT a pause of project jobs or remote data.
 * Install before final validation; keep it until main releases or destroys us.
 * Inert covers native editing (including iframe focus), capture also fences
 * app-wide shortcuts, composition, paste/drop and pointer-driven commands.
 */
function fenceInput(): () => void {
  const root = document.documentElement
  const inert = root.inert
  const events = ['keydown', 'keyup', 'beforeinput', 'input', 'change',
    'compositionstart', 'compositionupdate', 'compositionend', 'paste', 'cut',
    'drop', 'dragstart', 'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu']
  const block = (event: Event) => { event.preventDefault(); event.stopImmediatePropagation() }
  for (const name of events) window.addEventListener(name, block, { capture: true, passive: false })
  root.inert = true
  let released = false
  return () => {
    if (released) return
    released = true
    root.inert = inert
    for (const name of events) window.removeEventListener(name, block, true)
  }
}

/** A live transfer/focus/reorder does not call prepare or flush anything.
 * A late result is only an ACK for its ORIGINAL request, never a fresh tab revision.
 */
export function connectRendererCloseBarrier(
  bridge: Pick<RendererCloseBridge, 'getState'> & Partial<RendererCloseBridge> | undefined,
  barrier = rendererCloseBarrier,
) {
  let disposed = false
  let unsubscribe: (() => void) | undefined
  type CloseEntry = {
    prepared: boolean; released: boolean; phases: Set<string>; release?: () => void
  }
  const requests = new Map<string, CloseEntry>()
  const release = (entry: CloseEntry) => {
    entry.released = true; entry.release?.(); entry.release = undefined
  }
  const ready = (async () => {
    if (!bridge?.onPrepareClose || !bridge.acknowledgeClose) return
    const initial = await bridge.getState()
    if (disposed || initial.role !== 'project') return
    const identity = { tabId: initial.tab.tabId, project_id: initial.binding.project_id, epoch: initial.binding.epoch }
    const matches = (state: DesktopTabsState, request: RendererCloseRequest) =>
      !disposed && state.role === 'project' && state.tab.state !== 'closing'
      && state.tab.tabId === identity.tabId && request.tabId === identity.tabId
      && state.binding.project_id === identity.project_id && request.project_id === identity.project_id
      && state.binding.epoch === identity.epoch && request.epoch === identity.epoch
      && state.tab.revision === request.tabRevision

    unsubscribe = bridge.onPrepareClose(raw => {
      if (disposed || !validRequest(raw)) return
      // Copy only primitive protocol fields, never native/editor/File objects.
      const identity = { requestId: raw.requestId, tabId: raw.tabId,
        tabRevision: raw.tabRevision, project_id: raw.project_id, epoch: raw.epoch }
      const request: RendererCloseRequest = { ...identity, phase: raw.phase }
      const key = JSON.stringify(identity)
      let entry = requests.get(key)
      if (request.phase === 'release') {
        if (entry) release(entry)
        return
      }
      if (!entry) {
        if (request.phase !== 'prepare') return
        entry = { prepared: false, released: false, phases: new Set() }
        requests.set(key, entry)
      }
      if (entry.released || entry.phases.has(request.phase)) return
      entry.phases.add(request.phase) // one guard run / ACK per identity AND phase
      const current = entry
      const work = (async (): Promise<RendererCloseAck> => {
        let prepared = false
        try {
          const state = await bridge.getState()
          if (!current.released && matches(state, request)) {
            if (request.phase === 'prepare') {
              prepared = await barrier.prepare()
              prepared = matches(await bridge.getState(), request) && prepared && barrier.validate()
              current.prepared = prepared
            } else if (current.prepared && !current.released) {
              // No await between acquiring the fence, validation and sending
              // the final ACK. A dirty early approver vetoes the whole close.
              current.release = fenceInput()
              prepared = barrier.validate()
            }
          }
        } catch { /* Failure/no current renderer is never permission. */ }
        if (current.released) prepared = false
        if (!prepared) { current.release?.(); current.release = undefined }
        return { ...request, prepared }
      })()
      void work.then(async ack => {
        if (!disposed && !current.released) await bridge.acknowledgeClose!(ack)
      }).catch(() => {
        // A lost invoke response does NOT prove main missed the final ACK.
        // Only main's release (including timeout/error) or disposal may unlock.
      }).finally(() => {
        // Bound duplicate history; main owns request lifetime and consumption.
        if (requests.size > 64) for (const [oldKey, old] of requests) {
          if (old.released) { requests.delete(oldKey); break }
        }
      })
    })
  })().catch(() => { /* No handshake means no ACK, not a legacy fallback. */ })
  return { ready, dispose() {
    disposed = true; unsubscribe?.(); unsubscribe = undefined
    for (const entry of requests.values()) release(entry)
    requests.clear()
  } }
}
