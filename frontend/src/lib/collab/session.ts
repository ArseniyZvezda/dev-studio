/**
 * One editor's membership of a live room (dev spec §16.4).
 *
 * The browser never talks to the remote. It opens a socket to its own backend
 * (`/ws/collab`), which relays into the room for the document — so what is here is the
 * conversation with that relay: join, be handed the room's Yjs document or be asked to seed
 * it, and from then on exchange updates and cursors.
 *
 * A session is only ever handed out *ready*: `openSession` resolves once `doc` holds the
 * room's document, so an editor bound to it never shows — and can never save — a document
 * that is still on its way. It resolves with `null` when there is no room to be had (sync is
 * off, the server is unreachable, the file is not in step yet), and the editor then carries
 * on alone, exactly as it did before any of this existed.
 */

import { Awareness, applyAwarenessUpdate, encodeAwarenessUpdate, removeAwarenessStates } from 'y-protocols/awareness'
import * as Y from 'yjs'

import { withTokenParam } from '@/api/auth'

export interface CollabUser {
  id: string
  name: string
  color: string
}

/**
 * Why a room ended without this editor leaving it.
 *
 * - `reset` — the file was replaced or deleted from outside the room (an agent, a checkout,
 *   a delete). The room's document no longer describes the file; reopen it from disk.
 * - `offline` — the connection to the server, or to the backend, went away. What is on
 *   screen is kept and edited alone until a room can be joined again.
 */
export type EndReason = 'reset' | 'offline'

/** The slice of `WebSocket` a session uses — so a test can stand in for the backend. */
export interface SocketLike {
  send(data: string): void
  close(): void
  onopen: (() => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code: number }) => void) | null
  /** `WebSocket.readyState`, where the socket has one. */
  readonly readyState?: number
}

/** `WebSocket.OPEN`, without needing a `WebSocket` to read it from. */
const OPEN = 1

export interface OpenOptions {
  path: string
  /** Builds the update an empty room is seeded with, from the file's Markdown (`seed.ts`). */
  seed: (markdown: string) => Uint8Array
  socket?: () => SocketLike
  timeoutMs?: number
}

/** The backend's close code for "the room ended, reopen the document" (`sync/protocol.py`). */
const CLOSE_RESET = 4001
const JOIN_TIMEOUT_MS = 8000

/** The origin of every change that arrived from the room, so it is not sent back to it. */
const ROOM = Symbol('room')

interface Frame {
  t: string
  [key: string]: unknown
}

export function toBase64(bytes: Uint8Array): string {
  let binary = ''
  // In chunks: spreading a large update into one call overflows the argument limit.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  }
  return btoa(binary)
}

export function fromBase64(text: string): Uint8Array {
  const binary = atob(text)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

function defaultSocket(): SocketLike {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(withTokenParam(`${scheme}//${window.location.host}/ws/collab`)) as unknown as SocketLike
}

function parse(data: unknown): Frame | null {
  if (typeof data !== 'string') return null
  try {
    const frame = JSON.parse(data) as Frame
    return frame && typeof frame.t === 'string' ? frame : null
  } catch {
    return null
  }
}

export class CollabSession {
  readonly awareness: Awareness
  /** True once anything in the room changed after this editor joined it. */
  changed = false

  private isScribe: boolean
  private ended = false
  private readonly changeHandlers = new Set<() => void>()
  private readonly scribeHandlers = new Set<() => void>()
  private readonly peerHandlers = new Set<() => void>()
  private readonly endHandlers = new Set<(reason: EndReason) => void>()

  constructor(
    readonly path: string,
    readonly sid: string,
    readonly user: CollabUser,
    readonly doc: Y.Doc,
    private readonly socket: SocketLike,
    scribe: boolean,
    /**
     * The room had edits no scribe had written to the file yet when this editor joined —
     * so what it was handed is ahead of every disk, and if the scribe's job comes to it
     * the file is owed a save even though nothing changed since.
     */
    readonly aheadAtJoin: boolean,
  ) {
    this.isScribe = scribe
    this.awareness = new Awareness(doc)
    doc.on('update', this.onDocUpdate)
    this.awareness.on('update', this.onAwarenessUpdate)
    this.awareness.on('change', this.onAwarenessChange)
    this.awareness.setLocalStateField('user', user)
  }

  /**
   * Whether this editor is the one that writes the room's Markdown to disk.
   *
   * Exactly one member is, so the file follows the room's document without two editors
   * saving over each other. The job is inherited when its holder leaves (`onScribe`).
   */
  get scribe(): boolean {
    return this.isScribe
  }

  /** Everyone else in the room, one entry per person however many windows they have open. */
  peers(): CollabUser[] {
    const people = new Map<string, CollabUser>()
    for (const [clientId, state] of this.awareness.getStates()) {
      if (clientId === this.doc.clientID) continue
      const user = (state as { user?: Partial<CollabUser> }).user
      if (!user?.name) continue
      const id = String(user.id ?? user.name)
      if (id === this.user.id) continue
      people.set(id, { id, name: String(user.name), color: String(user.color ?? '#888888') })
    }
    return [...people.values()].sort((a, b) => a.name.localeCompare(b.name))
  }

  /** The room's document changed — here or anywhere else. */
  onChange(handler: () => void): () => void {
    this.changeHandlers.add(handler)
    return () => this.changeHandlers.delete(handler)
  }

  /** This editor has just become the scribe. */
  onScribe(handler: () => void): () => void {
    this.scribeHandlers.add(handler)
    return () => this.scribeHandlers.delete(handler)
  }

  onPeers(handler: () => void): () => void {
    this.peerHandlers.add(handler)
    return () => this.peerHandlers.delete(handler)
  }

  onEnd(handler: (reason: EndReason) => void): () => void {
    this.endHandlers.add(handler)
    return () => this.endHandlers.delete(handler)
  }

  /**
   * Leave the room. Call it once the editor bound to `doc` is gone — the binding observes
   * the document, and destroying it under a live editor is not a thing either survives.
   */
  close(): void {
    if (!this.ended) {
      // Said while the socket is still open, so the others drop this cursor at once
      // instead of when the server notices the socket has gone. Before `ended` is set:
      // `send` drops everything after it, and this removal is the one frame that must go.
      removeAwarenessStates(this.awareness, [this.doc.clientID], 'local')
      this.ended = true
      this.socket.onclose = null
      this.socket.close()
    }
    this.doc.off('update', this.onDocUpdate)
    this.awareness.destroy()
    this.doc.destroy()
    this.changeHandlers.clear()
    this.scribeHandlers.clear()
    this.peerHandlers.clear()
    this.endHandlers.clear()
  }

  /** A frame from the room. */
  receive(frame: Frame): void {
    if (this.ended) return
    try {
      switch (frame.t) {
        case 'update':
          Y.applyUpdate(this.doc, fromBase64(String(frame.update)), ROOM)
          break
        case 'awareness':
          applyAwarenessUpdate(this.awareness, fromBase64(String(frame.update)), ROOM)
          break
        case 'peer_joined':
          // The server keeps nobody's cursor; whoever has one sends it to the newcomer.
          this.announce()
          break
        case 'peer_left':
          removeAwarenessStates(this.awareness, [Number(frame.client_id)], ROOM)
          break
        case 'scribe':
          this.isScribe = true
          for (const handler of this.scribeHandlers) handler()
          break
      }
    } catch (error) {
      // One frame that will not apply must not take the room away from the editor.
      console.error(`Collaboration frame ${frame.t} could not be applied`, error)
    }
  }

  /** The socket closed without `close()` having been called. */
  end(reason: EndReason): void {
    if (this.ended) return
    this.ended = true
    for (const handler of [...this.endHandlers]) handler(reason)
  }

  private send(frame: Frame): void {
    if (this.ended) return
    // A socket the other end has started closing still takes a `send` — and a browser
    // logs each one as an error. The close itself arrives through `onclose` a moment later.
    if (this.socket.readyState !== undefined && this.socket.readyState !== OPEN) return
    try {
      this.socket.send(JSON.stringify(frame))
    } catch {
      // A socket that is closing reports it through `onclose`; nothing to add here.
    }
  }

  private announce(): void {
    if (this.awareness.getLocalState() === null) return
    this.send({
      t: 'awareness',
      update: toBase64(encodeAwarenessUpdate(this.awareness, [this.doc.clientID])),
    })
  }

  private readonly onDocUpdate = (update: Uint8Array, origin: unknown): void => {
    if (origin !== ROOM) this.send({ t: 'update', update: toBase64(update) })
    this.changed = true
    for (const handler of this.changeHandlers) handler()
  }

  private readonly onAwarenessUpdate = (
    changes: { added: number[]; updated: number[]; removed: number[] },
    origin: unknown,
  ): void => {
    if (origin === ROOM) return
    const mine = [...changes.added, ...changes.updated, ...changes.removed].filter(
      (clientId) => clientId === this.doc.clientID,
    )
    if (!mine.length) return
    this.send({ t: 'awareness', update: toBase64(encodeAwarenessUpdate(this.awareness, mine)) })
  }

  private readonly onAwarenessChange = (): void => {
    for (const handler of this.peerHandlers) handler()
  }
}

/**
 * Join the room for *path* and resolve once its document is in hand — or with `null` when
 * there is no room to join.
 */
export function openSession(options: OpenOptions): Promise<CollabSession | null> {
  return new Promise((resolve) => {
    const doc = new Y.Doc()
    let socket: SocketLike
    try {
      socket = (options.socket ?? defaultSocket)()
    } catch {
      doc.destroy()
      resolve(null)
      return
    }

    let session: CollabSession | null = null
    let settled = false
    let joined: Frame | null = null
    let offered: Uint8Array | null = null
    let scribe = false

    const fail = (): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      socket.onclose = null
      socket.onmessage = null
      try {
        socket.close()
      } catch {
        // Already closed, which is what was wanted.
      }
      doc.destroy()
      resolve(null)
    }
    const timer = setTimeout(fail, options.timeoutMs ?? JOIN_TIMEOUT_MS)

    const ready = (state: Uint8Array): void => {
      if (settled || !joined) return
      try {
        Y.applyUpdate(doc, state, ROOM)
      } catch {
        fail()
        return
      }
      settled = true
      clearTimeout(timer)
      const user = (joined.user ?? {}) as Partial<CollabUser>
      session = new CollabSession(
        options.path,
        String(joined.sid),
        { id: String(user.id ?? ''), name: String(user.name ?? ''), color: String(user.color ?? '#888888') },
        doc,
        socket,
        scribe,
        joined.dirty === true,
      )
      resolve(session)
    }

    socket.onopen = () => {
      socket.send(JSON.stringify({ t: 'join', path: options.path, client_id: doc.clientID }))
    }
    socket.onmessage = (event) => {
      const frame = parse(event.data)
      if (!frame) return
      if (session) {
        session.receive(frame)
        return
      }
      if (frame.t === 'joined') {
        joined = frame
        scribe = frame.scribe === true
        if (frame.seeded) {
          ready(fromBase64(String(frame.state)))
          return
        }
        // An empty room. Offer the document, built from the Markdown the backend read at
        // the revision the room was opened at — the same text every member would build
        // from. The server takes the first offer and answers the rest with the winner's.
        try {
          offered = options.seed(typeof frame.content === 'string' ? frame.content : '')
        } catch {
          fail()
          return
        }
        socket.send(JSON.stringify({ t: 'seed', update: toBase64(offered) }))
      } else if (frame.t === 'seeded' && joined) {
        if (frame.ok === true && offered) ready(offered)
        else if (typeof frame.state === 'string') ready(fromBase64(frame.state))
        else fail()
      } else if (frame.t === 'scribe') {
        // The member before us left while we were still arriving.
        scribe = true
      }
    }
    socket.onclose = (event) => {
      if (session) session.end(event.code === CLOSE_RESET ? 'reset' : 'offline')
      else fail()
    }
  })
}
