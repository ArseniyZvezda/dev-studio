/**
 * One multiplexed event channel per renderer (dev spec §2.4, §5.3.2).
 * Project sockets never follow the legacy browser selection. Their replay position
 * belongs to (project_id, epoch), not to an unqualified sequence number.
 */
import { ref, shallowRef } from 'vue'

import { withTokenParam } from '@/api/auth'
import { scopedWsUrl } from '@/api/project-scope'
import { t } from '@/i18n'
import { ProjectSession, rendererSession, type ProjectScope } from '@/session/project-session'
import type { EventEnvelope, EventPayloads, EventType } from './events'

type Handler<T extends EventType> = (data: EventPayloads[T], envelope: EventEnvelope<T>) => void
export type ConnectionState = 'connecting' | 'open' | 'closed'

const RECONNECT_MIN_MS = 500
const RECONNECT_MAX_MS = 10_000
const PING_INTERVAL_MS = 25_000

export class WsConnection {
  readonly state = ref<ConnectionState>('closed')
  readonly lastError = shallowRef<string | null>(null)
  /** Highest delivered sequence in cursorScope, never a different project's cursor. */
  readonly lastSeq = ref<number>(0)
  readonly gaps = ref<number>(0)

  private socket: WebSocket | null = null
  private handlers = new Map<string, Set<Handler<EventType>>>()
  private reconnectDelay = RECONNECT_MIN_MS
  private reconnectTimer: number | null = null
  private pingTimer: number | null = null
  private stopped = false
  private cursorScope: ProjectScope | null = null
  private cursorKnown = false
  private cursorGeneration = -1
  private socketGeneration = -1
  private lifetime = 0

  constructor(readonly session: ProjectSession = rendererSession) {}

  connect(): void {
    // Validate before creating a socket, including a scoped session not bootstrapped yet.
    const assertCurrent = this.session.capture()
    const scope = this.session.scope
    const scopeChanged = this.cursorGeneration !== this.session.generation
      || scope?.project_id !== this.cursorScope?.project_id || scope?.epoch !== this.cursorScope?.epoch
    if (scopeChanged) {
      this.lastSeq.value = 0
      this.cursorKnown = false
      this.cursorScope = scope
      this.cursorGeneration = this.session.generation
    }
    const resume = this.lastSeq.value > 0 || (!this.session.isLegacy && this.cursorKnown)
    const path = scopedWsUrl(this.session, false, resume ? this.lastSeq.value : undefined)
    if (this.socket && this.socketGeneration === this.session.generation && this.socket.readyState <= WebSocket.OPEN) return
    this.disconnect()
    this.stopped = false
    this.state.value = 'connecting'
    const lifetime = ++this.lifetime
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(withTokenParam(`${scheme}//${window.location.host}${path}`))
    this.socket = socket
    this.socketGeneration = this.session.generation
    const current = (): boolean => {
      if (this.stopped || this.socket !== socket || lifetime !== this.lifetime) return false
      try { assertCurrent(); return true } catch { return false }
    }

    socket.onopen = () => {
      if (!current()) return
      this.state.value = 'open'
      this.lastError.value = null
      this.reconnectDelay = RECONNECT_MIN_MS
      this.startPing(socket, current)
    }
    socket.onmessage = (event) => { if (current()) this.dispatch(event.data) }
    socket.onerror = () => { if (current()) this.lastError.value = t('api.ws.error') }
    socket.onclose = (event) => {
      // A delayed close from an abandoned socket cannot close its replacement.
      if (this.socket !== socket || lifetime !== this.lifetime) return
      const reconnect = current()
      this.stopPing()
      this.socket = null
      this.state.value = 'closed'
      if (!reconnect) return
      // Policy violations include bad tokens and expired project handles. No fallback.
      if (event.code === 1008) {
        this.lastError.value = event.reason || t('api.ws.unauthorised')
        return
      }
      this.scheduleReconnect(assertCurrent)
    }
  }

  disconnect(): void {
    this.stopped = true
    this.lifetime += 1
    this.stopPing()
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.state.value = 'closed'
  }

  /** Subscribe to this connection only. Returns the unsubscribe function. */
  on<T extends EventType>(type: T, handler: Handler<T>): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    set.add(handler as Handler<EventType>)
    return () => { set.delete(handler as Handler<EventType>) }
  }

  private dispatch(raw: unknown): void {
    if (typeof raw !== 'string') return
    let envelope: EventEnvelope
    try { envelope = JSON.parse(raw) as EventEnvelope } catch { return }
    if (!envelope || typeof envelope.type !== 'string' || !envelope.data
      || typeof envelope.data !== 'object' || !this.session.matches(envelope)) return

    const hello = envelope.type === 'system.hello'
    if (typeof envelope.seq === 'number' && Number.isSafeInteger(envelope.seq) && envelope.seq >= 0) {
      this.cursorKnown = true
      if (hello) {
        const restarted = envelope.seq < this.lastSeq.value
        // On resume hello is a watermark BEFORE replay, not an acknowledgement that
        // those queued events were delivered. Otherwise a second drop loses history.
        const resumed = (envelope.data as EventPayloads['system.hello']).resumed_from
        if (this.session.isLegacy || resumed == null || restarted) this.lastSeq.value = envelope.seq
        if (restarted && !this.session.isLegacy) {
          this.emit({ type: 'system.dropped', data: { count: 0, resync: true, reason: 'restart' },
            project_id: envelope.project_id, epoch: envelope.epoch } as EventEnvelope<'system.dropped'>)
          this.gaps.value += 1
        }
      } else {
        if (!this.session.isLegacy && envelope.seq <= this.lastSeq.value) return
        if (envelope.seq > this.lastSeq.value) this.lastSeq.value = envelope.seq
      }
    }
    if (envelope.type === 'system.dropped') {
      this.gaps.value += 1
      // Scoped resync means history is incomplete: notify only this renderer's
      // subscribers to refetch REST. Replaying an expired cursor in response to the
      // resulting dropped/resync would cause an endless request/drop loop (§5.3.2).
      if (this.session.isLegacy || !(envelope.data as EventPayloads['system.dropped']).resync) this.requestReplay()
    }
    this.emit(envelope)
  }

  private emit(envelope: EventEnvelope): void {
    const set = this.handlers.get(envelope.type)
    if (!set) return
    for (const handler of set) {
      try { handler(envelope.data, envelope) }
      catch (error) { console.error(`Handler for ${envelope.type} threw`, error) }
    }
  }

  private requestReplay(): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.socket.send(JSON.stringify({ action: 'replay', since_seq: this.lastSeq.value, ...this.cursorScope }))
  }

  private scheduleReconnect(assertCurrent: () => void): void {
    if (this.reconnectTimer !== null) return
    const delay = this.reconnectDelay
    this.reconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      try { assertCurrent() } catch { return }
      if (!this.stopped) this.connect()
    }, delay)
  }

  private startPing(socket: WebSocket, current: () => boolean): void {
    this.stopPing()
    this.pingTimer = window.setInterval(() => {
      if (current() && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: 'ping', ...this.cursorScope }))
      }
    }, PING_INTERVAL_MS)
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }
}

export const ws = new WsConnection()

/** Sugar for the singleton renderer's stores. */
export function onEvent<T extends EventType>(type: T, handler: Handler<T>): () => void {
  return ws.on(type, handler)
}
