/**
 * The app's single WebSocket connection (spec §2.4: one multiplexed channel).
 *
 * The backend broadcasts every event to every client — `channel` on the envelope is a
 * tag, not a subscription — so demultiplexing is our job. This module owns the socket
 * and hands each frame to whichever store registered interest in that event type,
 * which keeps the fan-out in one place instead of scattering `if (msg.type === …)`
 * chains through the stores.
 *
 * Reconnection replays: the bus keeps a ring buffer keyed by `seq`, so reconnecting
 * with `?since_seq=` recovers the events that happened while we were away rather than
 * silently losing a node status change or an agent message.
 */

import { ref, shallowRef } from 'vue'

import { withTokenParam } from '@/api/auth'
import { t } from '@/i18n'
import type { EventEnvelope, EventPayloads, EventType } from './events'

type Handler<T extends EventType> = (data: EventPayloads[T], envelope: EventEnvelope<T>) => void

export type ConnectionState = 'connecting' | 'open' | 'closed'

const RECONNECT_MIN_MS = 500
const RECONNECT_MAX_MS = 10_000
const PING_INTERVAL_MS = 25_000

class WsConnection {
  readonly state = ref<ConnectionState>('closed')
  readonly lastError = shallowRef<string | null>(null)
  /** Highest `seq` we have processed; the resume point after a drop. */
  readonly lastSeq = ref<number>(0)
  /** How many times the backend reported a gap in our stream. Surfaced for diagnostics. */
  readonly gaps = ref<number>(0)

  private socket: WebSocket | null = null
  private handlers = new Map<string, Set<Handler<EventType>>>()
  private reconnectDelay = RECONNECT_MIN_MS
  private reconnectTimer: number | null = null
  private pingTimer: number | null = null
  private stopped = false

  connect(): void {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return
    this.stopped = false
    this.state.value = 'connecting'

    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const base = `${scheme}//${window.location.host}/ws`
    // Resume from where we left off; on a first connect seq 0 means "everything the
    // buffer still holds", which is exactly what a freshly loaded UI wants.
    const url = withTokenParam(this.lastSeq.value > 0 ? `${base}?since_seq=${this.lastSeq.value}` : base)

    const socket = new WebSocket(url)
    this.socket = socket

    socket.onopen = () => {
      this.state.value = 'open'
      this.lastError.value = null
      this.reconnectDelay = RECONNECT_MIN_MS
      this.startPing()
    }

    socket.onmessage = (event) => this.dispatch(event.data)

    socket.onerror = () => {
      this.lastError.value = t('api.ws.error')
    }

    socket.onclose = (event) => {
      this.stopPing()
      this.socket = null
      this.state.value = 'closed'
      // 1008 is the backend's "bad or missing token" — retrying cannot fix that, so
      // surface it instead of hammering the endpoint.
      if (event.code === 1008) {
        this.lastError.value = t('api.ws.unauthorised')
        return
      }
      if (!this.stopped) this.scheduleReconnect()
    }
  }

  disconnect(): void {
    this.stopped = true
    this.stopPing()
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.close()
    this.socket = null
    this.state.value = 'closed'
  }

  /** Subscribe to one event type. Returns the unsubscribe function. */
  on<T extends EventType>(type: T, handler: Handler<T>): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    set.add(handler as Handler<EventType>)
    return () => {
      set.delete(handler as Handler<EventType>)
    }
  }

  private dispatch(raw: string): void {
    let envelope: EventEnvelope
    try {
      envelope = JSON.parse(raw) as EventEnvelope
    } catch {
      return
    }
    if (typeof envelope.seq === 'number') {
      // A sequence only grows for as long as its bus lives, and `system.hello` carries the
      // bus's current one. A hello *behind* us is therefore a backend that restarted and is
      // counting from zero again: keeping the old, higher number would make every later
      // resume and gap replay ask for events this bus will not reach for a long time.
      const restarted = envelope.type === 'system.hello' && envelope.seq < this.lastSeq.value
      if (restarted || envelope.seq > this.lastSeq.value) this.lastSeq.value = envelope.seq
    }
    // The backend evicts the oldest events for a subscriber that falls behind rather
    // than stalling a running pipeline, and then says so. Those events are still in the
    // bus's ring buffer, so asking for a replay from our last seq closes the gap without
    // dropping the socket. Done before dispatch so handlers see the recovered frames.
    if (envelope.type === 'system.dropped') {
      this.gaps.value += 1
      this.requestReplay()
    }
    const set = this.handlers.get(envelope.type)
    if (!set) return
    for (const handler of set) {
      try {
        handler(envelope.data, envelope)
      } catch (error) {
        // One misbehaving store must not stop the others from seeing the event.
        console.error(`Handler for ${envelope.type} threw`, error)
      }
    }
  }

  private requestReplay(): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.socket.send(JSON.stringify({ action: 'replay', since_seq: this.lastSeq.value }))
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return
    const delay = this.reconnectDelay
    this.reconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private startPing(): void {
    this.stopPing()
    this.pingTimer = window.setInterval(() => {
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ action: 'ping' }))
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

/** Sugar for stores: `onEvent('node.log', …)` reads better than `ws.on(…)` at call sites. */
export function onEvent<T extends EventType>(type: T, handler: Handler<T>): () => void {
  return ws.on(type, handler)
}
