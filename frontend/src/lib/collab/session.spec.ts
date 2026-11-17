/**
 * A session against a stand-in for the backend and the room behind it (dev spec §16.4).
 *
 * The stand-in is the remote's room logic reduced to what an editor can observe: the first
 * seed wins, updates and cursors reach everyone else, the scribe's job is inherited. The
 * real one is tested where it lives (`remote/tests`, `backend/tests/test_remote_sync.py`);
 * what is checked here is that a session does the right thing with each answer.
 */

import { describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import { ProjectSession } from '@/session/project-session'
import type { DocumentFence } from '@/api/types'
import type { RemoteBindingTicket } from '@/stores/sync'

import {
  CollabSession,
  fromBase64,
  openSession,
  toBase64,
  type SocketLike,
} from './session'

interface Member {
  socket: FakeSocket
  clientId: number
  synced: boolean
}

class FakeRoom {
  readonly doc = new Y.Doc()
  seeded = false
  members: Member[] = []
  scribe: Member | null = null
  private names = 0

  connect(name?: string): () => FakeSocket {
    return () => new FakeSocket(this, name ?? `user${(this.names += 1)}`)
  }

  receive(from: FakeSocket, frame: Record<string, unknown>): void {
    const member = this.members.find((candidate) => candidate.socket === from)
    if (frame.t === 'join') {
      const joiner: Member = { socket: from, clientId: Number(frame.client_id), synced: this.seeded }
      this.members.push(joiner)
      this.scribe ??= joiner
      from.deliver({
        t: 'joined',
        sid: `sid-${from.name}`,
        seeded: this.seeded,
        state: this.seeded ? toBase64(Y.encodeStateAsUpdate(this.doc)) : null,
        scribe: this.scribe === joiner,
        content: this.seeded ? null : '# Seed\n',
        user: { id: from.name, name: from.name, color: '#123456' },
      })
      if (this.seeded) this.others(joiner).forEach((other) => other.socket.deliver({ t: 'peer_joined' }))
    } else if (!member) {
      return
    } else if (frame.t === 'seed') {
      if (this.seeded) {
        member.synced = true
        from.deliver({ t: 'seeded', ok: false, state: toBase64(Y.encodeStateAsUpdate(this.doc)) })
        this.others(member).forEach((other) => other.socket.deliver({ t: 'peer_joined' }))
      } else {
        Y.applyUpdate(this.doc, fromBase64(String(frame.update)))
        this.seeded = true
        member.synced = true
        from.deliver({ t: 'seeded', ok: true, state: null })
      }
    } else if (frame.t === 'update') {
      Y.applyUpdate(this.doc, fromBase64(String(frame.update)))
      this.others(member).forEach((other) => other.socket.deliver({ t: 'update', update: frame.update }))
    } else if (frame.t === 'awareness') {
      this.others(member).forEach((other) => other.socket.deliver({ t: 'awareness', update: frame.update }))
    }
  }

  leave(socket: FakeSocket): void {
    const member = this.members.find((candidate) => candidate.socket === socket)
    if (!member) return
    this.members = this.members.filter((candidate) => candidate !== member)
    this.members.forEach((other) => other.socket.deliver({ t: 'peer_left', client_id: member.clientId }))
    if (this.scribe === member) {
      this.scribe = this.members[0] ?? null
      this.scribe?.socket.deliver({ t: 'scribe' })
    }
  }

  private others(member: Member): Member[] {
    return this.members.filter((candidate) => candidate !== member && candidate.synced)
  }
}

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false

  constructor(
    private readonly room: FakeRoom,
    readonly name: string,
  ) {
    queueMicrotask(() => this.onopen?.())
  }

  send(data: string): void {
    if (this.closed) throw new Error('closed')
    this.room.receive(this, JSON.parse(data) as Record<string, unknown>)
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.room.leave(this)
  }

  deliver(frame: Record<string, unknown>): void {
    if (!this.closed) this.onmessage?.({ data: JSON.stringify(frame) })
  }

  /** The backend hangs up on us. */
  drop(code: number): void {
    this.closed = true
    this.room.leave(this)
    this.onclose?.({ code })
  }
}

/** A seed with one recognisable line, and the number of times it was asked for. */
function seeder(): { seed: (markdown: string) => Uint8Array; asked: string[] } {
  const asked: string[] = []
  return {
    asked,
    seed: (markdown) => {
      asked.push(markdown)
      const doc = new Y.Doc()
      doc.getText('body').insert(0, markdown)
      return Y.encodeStateAsUpdate(doc)
    },
  }
}

const text = (session: CollabSession): string => session.doc.getText('body').toString()

async function join(room: FakeRoom, name?: string, seed = seeder()): Promise<CollabSession> {
  const session = await openSession({ path: 'doc.md', seed: seed.seed, socket: room.connect(name) })
  if (!session) throw new Error('no session')
  return session
}

describe('base64 framing', () => {
  it('round-trips updates larger than one argument list', () => {
    const bytes = new Uint8Array(100_000).map((_, index) => index % 251)
    expect(fromBase64(toBase64(bytes))).toEqual(bytes)
  })
})

describe('joining a room', () => {
  it('seeds an empty room from the Markdown it is handed, and only then is ready', async () => {
    const room = new FakeRoom()
    const first = seeder()
    const alice = await join(room, 'alice', first)
    expect(first.asked).toEqual(['# Seed\n'])
    expect(text(alice)).toBe('# Seed\n')
    expect(alice.scribe).toBe(true)
    expect(alice.user.name).toBe('alice')
    expect(alice.changed).toBe(false)
  })

  it('takes the room as it is when somebody was first, without seeding', async () => {
    const room = new FakeRoom()
    const alice = await join(room, 'alice')
    alice.doc.getText('body').insert(alice.doc.getText('body').length, 'more')

    const second = seeder()
    const bob = await join(room, 'bob', second)
    expect(second.asked).toEqual([])
    expect(text(bob)).toBe('# Seed\nmore')
    expect(bob.scribe).toBe(false)
  })

  it('drops its own offer when it loses the race to seed', async () => {
    const room = new FakeRoom()
    // Both join before either seeds: hold the first one's seed back by joining by hand.
    const aliceSeed = seeder()
    const bobSeed = seeder()
    const [alice, bob] = await Promise.all([
      openSession({ path: 'doc.md', seed: aliceSeed.seed, socket: room.connect('alice') }),
      (async () => {
        // Arrive while the room exists but before it is seeded.
        const original = room.receive.bind(room)
        let released = false
        const held: Array<() => void> = []
        room.receive = (from, frame) => {
          if (!released && frame.t === 'seed' && from.name === 'alice') {
            held.push(() => original(from, frame))
            return
          }
          original(from, frame)
        }
        const pending = openSession({ path: 'doc.md', seed: bobSeed.seed, socket: room.connect('bob') })
        await Promise.resolve()
        await Promise.resolve()
        released = true
        // Bob's seed has landed first; now Alice's arrives and is turned down.
        held.forEach((run) => run())
        room.receive = original
        return pending
      })(),
    ])
    expect(alice && bob).toBeTruthy()
    expect(aliceSeed.asked).toHaveLength(1)
    expect(bobSeed.asked).toHaveLength(1)
    // One copy of the text in the room and in both editors — not two.
    expect(room.doc.getText('body').toString()).toBe('# Seed\n')
    expect(text(alice!)).toBe('# Seed\n')
    expect(text(bob!)).toBe('# Seed\n')
  })

  it('gives up with nothing when there is no room', async () => {
    const refusing = (): SocketLike => {
      const socket: SocketLike = {
        onopen: null, onmessage: null, onclose: null,
        send: () => {}, close: () => {},
      }
      queueMicrotask(() => socket.onclose?.({ code: 4000 }))
      return socket
    }
    expect(await openSession({ path: 'doc.md', seed: seeder().seed, socket: refusing })).toBeNull()

    const silent = (): SocketLike => ({
      onopen: null, onmessage: null, onclose: null, send: () => {}, close: () => {},
    })
    expect(
      await openSession({ path: 'doc.md', seed: seeder().seed, socket: silent, timeoutMs: 10 }),
    ).toBeNull()
  })
})

describe('in a room', () => {
  it('sends its own edits and applies everyone else’s, without echoing them back', async () => {
    const room = new FakeRoom()
    const alice = await join(room, 'alice')
    const bob = await join(room, 'bob')
    let aliceChanges = 0
    let bobChanges = 0
    alice.onChange(() => (aliceChanges += 1))
    bob.onChange(() => (bobChanges += 1))

    bob.doc.getText('body').insert(0, 'B:')
    expect(text(alice)).toBe('B:# Seed\n')
    alice.doc.getText('body').insert(0, 'A:')
    expect(text(bob)).toBe(text(alice))
    // Each heard of both edits exactly once: an update applied from the room is not sent on.
    expect([aliceChanges, bobChanges]).toEqual([2, 2])
    expect(alice.changed && bob.changed).toBe(true)
  })

  it('shows who else is here, and forgets them when they leave', async () => {
    const room = new FakeRoom()
    const alice = await join(room, 'alice')
    let notified = 0
    alice.onPeers(() => (notified += 1))
    expect(alice.peers()).toEqual([])

    const bob = await join(room, 'bob')
    // Bob's cursor reached Alice; Alice re-announced hers for the newcomer.
    expect(alice.peers().map((peer) => peer.name)).toEqual(['bob'])
    expect(bob.peers().map((peer) => peer.name)).toEqual(['alice'])
    expect(notified).toBeGreaterThan(0)

    bob.close()
    expect(alice.peers()).toEqual([])
  })

  it('inherits the scribe’s job when its holder leaves', async () => {
    const room = new FakeRoom()
    const alice = await join(room, 'alice')
    const bob = await join(room, 'bob')
    let appointed = 0
    bob.onScribe(() => (appointed += 1))
    expect(bob.scribe).toBe(false)
    alice.close()
    expect(bob.scribe).toBe(true)
    expect(appointed).toBe(1)
  })

  it('reports why a room ended, once, and not for a leave of its own', async () => {
    const room = new FakeRoom()
    const alice = await join(room, 'alice')
    const bob = await join(room, 'bob')
    const carol = await join(room, 'carol')
    const ends: string[] = []
    alice.onEnd((reason) => ends.push(`alice:${reason}`))
    bob.onEnd((reason) => ends.push(`bob:${reason}`))
    carol.onEnd((reason) => ends.push(`carol:${reason}`))

    room.members.find((member) => member.socket.name === 'alice')!.socket.drop(4001)
    room.members.find((member) => member.socket.name === 'bob')!.socket.drop(1006)
    carol.close()
    expect(ends).toEqual(['alice:reset', 'bob:offline'])
  })
})

/** The local scoped relay envelope, independent of remote room incarnation epochs. */
class ScopedSocket implements SocketLike {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  readyState = 1
  closed = false
  sent: Array<Record<string, unknown>> = []
  send(data: string): void { this.sent.push(JSON.parse(data)) }
  close(): void { this.closed = true; this.readyState = 3 }
  deliver(frame: Record<string, unknown>): void { this.onmessage?.({ data: JSON.stringify(frame) }) }
}

function projectSession(id: string): ProjectSession {
  const binding = new ProjectSession(id)
  binding.bind({ project_id: id, epoch: 'backend-epoch' })
  return binding
}
function roomDocument(body: string): Y.Doc {
  const doc = new Y.Doc()
  doc.getText('body').insert(0, body)
  return doc
}
function joinedFrame(binding: ProjectSession, doc: Y.Doc): Record<string, unknown> {
  return {
    ...binding.scope, t: 'joined', sid: `sid-${binding.projectId}`, room_epoch: 'room-incarnation',
    scribe: true, seeded: true, dirty: false, state: toBase64(Y.encodeStateAsUpdate(doc)),
    user: { id: 'alice', name: 'Alice', color: '#123456' },
  }
}

describe('project-scoped collab binding (§5.3.2)', () => {
  it('isolates identical document paths, handshakes, updates and awareness across A/B', async () => {
    const a = projectSession('A'); const b = projectSession('B')
    const sa = new ScopedSocket(); const sb = new ScopedSocket()
    const roomA = roomDocument('A'); const roomB = roomDocument('B')
    const seed = vi.fn(seeder().seed)
    const openingA = openSession({ path: 'same.md', seed, session: a, socket: () => sa })
    const openingB = openSession({ path: 'same.md', seed, session: b, socket: () => sb })
    sa.onopen!(); sb.onopen!()
    expect(sa.sent[0]).toMatchObject({ t: 'join', path: 'same.md', ...a.scope })
    expect(sb.sent[0]).toMatchObject({ t: 'join', path: 'same.md', ...b.scope })
    // A foreign join cannot seed A or hand it B's document, even at the same path.
    sa.deliver({ ...joinedFrame(b, roomB), seeded: false, content: 'foreign' })
    sa.deliver({ ...joinedFrame(a, roomA), epoch: 'expired' })
    sa.deliver({ t: 'joined', seeded: false, content: 'unscoped' })
    expect(seed).not.toHaveBeenCalled(); expect(sa.sent).toHaveLength(1)
    sa.deliver(joinedFrame(a, roomA)); sb.deliver(joinedFrame(b, roomB))
    const [alice, bob] = await Promise.all([openingA, openingB])
    expect(text(alice!)).toBe('A'); expect(text(bob!)).toBe('B')
    expect(alice!.scope).toEqual(a.scope); expect(bob!.scope).toEqual(b.scope)
    expect(alice!.changed).toBe(false)
    roomB.getText('body').insert(1, '-foreign')
    sa.deliver({ t: 'update', update: toBase64(Y.encodeStateAsUpdate(roomB)), ...b.scope })
    roomA.getText('body').insert(1, '-local')
    const update = toBase64(Y.encodeStateAsUpdate(roomA))
    sa.deliver({ t: 'update', update, ...a.scope, epoch: 'expired' })
    sa.deliver({ t: 'update', update })
    expect(text(alice!)).toBe('A')
    sa.deliver({ t: 'update', update, ...a.scope })
    expect(text(alice!)).toBe('A-local'); expect(text(bob!)).toBe('B')
    alice!.doc.getText('body').insert(0, '!')
    expect(sa.sent.some(frame => frame.t === 'update')).toBe(true)
    expect(sa.sent.some(frame => frame.t === 'awareness')).toBe(true)
    expect(sa.sent.every(frame => frame.project_id === 'A' && frame.epoch === 'backend-epoch')).toBe(true)
    expect(sb.sent.every(frame => frame.project_id === 'B' && frame.epoch === 'backend-epoch')).toBe(true)
    alice!.close(); bob!.close(); roomA.destroy(); roomB.destroy()
  })

  it('envelopes seeds and preserves the originally captured document path', async () => {
    const a = projectSession('A'); const socket = new ScopedSocket()
    const options = { path: 'original.md', seed: seeder().seed, session: a, socket: () => socket }
    const pending = openSession(options)
    options.path = 'different.md'
    socket.onopen!()
    expect(socket.sent[0]).toMatchObject({ path: 'original.md', ...a.scope })
    socket.deliver({ t: 'joined', seeded: false, sid: 'sid', scribe: true, content: '# own', ...a.scope })
    expect(socket.sent[1]).toMatchObject({ t: 'seed', ...a.scope })
    socket.deliver({ t: 'seeded', ok: true, ...a.scope })
    const session = await pending
    expect(session!.path).toBe('original.md'); expect(text(session!)).toBe('# own')
    expect(session!.changed).toBe(false)
    session!.close()
  })

  it('drops stale pending handshakes and does not send edits after invalidation', async () => {
    const a = projectSession('A'); const socket = new ScopedSocket(); const doc = roomDocument('A')
    const pending = openSession({ path: 'same.md', seed: seeder().seed, session: a, socket: () => socket })
    socket.onopen!(); a.invalidate(); socket.deliver(joinedFrame(a, doc))
    expect(await pending).toBeNull(); expect(socket.closed).toBe(true)

    const b = projectSession('B'); const sb = new ScopedSocket()
    const opening = openSession({ path: 'same.md', seed: seeder().seed, session: b, socket: () => sb })
    sb.onopen!(); sb.deliver(joinedFrame(b, doc))
    const session = (await opening)!
    const ended = vi.fn(); session.onEnd(ended)
    const sent = sb.sent.length
    b.invalidate(); session.doc.getText('body').insert(0, 'stale')
    sb.deliver({ t: 'scribe', ...b.scope })
    expect(sb.sent).toHaveLength(sent); expect(sb.closed).toBe(true)
    expect(ended).toHaveBeenCalledExactlyOnceWith('offline')
    session.close(); doc.destroy()
  })

  it('requires bootstrap even with a supplied transport and fails closed on malformed state', async () => {
    const socket = new ScopedSocket()
    expect(() => openSession({ path: 'same.md', seed: seeder().seed, session: new ProjectSession('A'), socket: () => socket })).toThrow()
    const a = projectSession('A')
    const pending = openSession({ path: 'same.md', seed: seeder().seed, session: a, socket: () => socket })
    socket.onopen!(); socket.deliver({ t: 'joined', seeded: true, state: 'not base64!', ...a.scope })
    expect(await pending).toBeNull(); expect(socket.closed).toBe(true)
  })

  it('uses the scoped WS handshake URL instead of the legacy collab relay', async () => {
    const a = projectSession('A'); let url = ''; let socket: ScopedSocket | undefined
    vi.stubGlobal('window', {
      location: { protocol: 'https:', host: 'localhost:8765' },
      localStorage: { getItem: () => null },
    })
    vi.stubGlobal('WebSocket', class extends ScopedSocket {
      constructor(value: string) { super(); url = value; socket = this }
    })
    try {
      const pending = openSession({ path: 'same.md', seed: seeder().seed, session: a })
      expect(new URL(url).protocol).toBe('wss:')
      expect(new URL(url).pathname).toBe('/ws/projects/A/collab')
      expect(new URL(url).searchParams.get('project_id')).toBe('A')
      expect(new URL(url).searchParams.get('epoch')).toBe('backend-epoch')
      socket!.onclose!({ code: 1008 })
      expect(await pending).toBeNull()
    } finally { vi.unstubAllGlobals() }
  })
})

/** Mirrors an I7 ticket: a barrier phase at the SAME G keeps it valid for final
 * saves; fence/commit invalidates it. No new metadata is read by the session. */
function capturedRemote(binding: ProjectSession, generation = 7) {
  let current = true
  const ticket: RemoteBindingTicket = Object.freeze({
    runtime_scope: binding.scope!, backend_epoch: 'mutation-epoch',
    expected_binding_generation: generation,
    assertCurrent: () => { if (!current) throw new Error('stale remote binding') },
  })
  const fence: DocumentFence = {
    backend_epoch: ticket.backend_epoch, expected_binding_generation: generation,
    editor_id: 'editor-A', client_instance_id: 'browser-A',
  }
  return { ticket, fence, invalidate: () => { current = false } }
}

describe('captured remote collab fence (I8, studio-design §5.3)', () => {
  it('captures the join before transport/debounce, then keeps the lease and assigned sid for updates', async () => {
    const binding = projectSession('A'); const socket = new ScopedSocket()
    const captured = capturedRemote(binding); const original = { ...captured.fence }
    const options = { path: 'same.md', session: binding, socket: () => socket,
      seed: seeder().seed, fence: captured.fence, ticket: captured.ticket }
    const opening = openSession(options)
    // A caller mutating/replacing its options cannot relabel an outstanding join.
    Object.assign(captured.fence, { expected_binding_generation: 8, editor_id: 'editor-new' })
    options.fence = { ...original, expected_binding_generation: 9 }
    socket.onopen!()
    expect(socket.sent[0]).toMatchObject({ t: 'join', ...original })
    socket.deliver({ t: 'joined', seeded: false, sid: 'old-sid', content: '# old', ...binding.scope })
    expect(socket.sent[1]).toMatchObject({ t: 'seed', ...original })
    socket.deliver({ t: 'seeded', ok: true, ...binding.scope })
    const session = (await opening)!
    expect(session.fence).toEqual({ ...original, sid: 'old-sid' })
    expect(Object.isFrozen(session.fence)).toBe(true)
    session.doc.getText('body').insert(0, 'final ')
    expect(socket.sent.filter(frame => frame.t === 'update')).toHaveLength(1)
    expect(socket.sent.at(-1)).toMatchObject({ t: 'update', ...original, sid: 'old-sid' })
    session.close()
  })

  it('refuses a delayed old-G join without creating a replacement session', async () => {
    const binding = projectSession('A'); const socket = new ScopedSocket()
    const captured = capturedRemote(binding); const factory = vi.fn(() => socket)
    const opening = openSession({ path: 'same.md', session: binding, socket: factory,
      seed: seeder().seed, ...captured })
    captured.invalidate()
    socket.onopen!()
    expect(await opening).toBeNull()
    expect(socket.sent).toEqual([]); expect(socket.closed).toBe(true)
    expect(factory).toHaveBeenCalledTimes(1)
    expect(() => openSession({ path: 'same.md', session: binding, socket: factory,
      seed: seeder().seed, ...captured })).toThrow('stale remote binding')
    expect(factory).toHaveBeenCalledTimes(1)
  })

  it('fences late updates/scribe callbacks but retains the Y.Doc until explicit final-save cleanup', async () => {
    const binding = projectSession('A'); const socket = new ScopedSocket(); const roomDoc = roomDocument('screen')
    const captured = capturedRemote(binding)
    const opening = openSession({ path: 'same.md', session: binding, socket: () => socket,
      seed: seeder().seed, ...captured })
    socket.onopen!(); socket.deliver({ ...joinedFrame(binding, roomDoc), scribe: false })
    const session = (await opening)!; const destroy = vi.spyOn(session.doc, 'destroy')
    const ended = vi.fn(); const appointed = vi.fn()
    session.onEnd(ended); session.onScribe(appointed)
    const delayedReceive = socket.onmessage!; const count = socket.sent.length
    captured.invalidate()
    roomDoc.getText('body').insert(0, 'foreign ')
    delayedReceive({ data: JSON.stringify({ t: 'update', update: toBase64(Y.encodeStateAsUpdate(roomDoc)), ...binding.scope }) })
    delayedReceive({ data: JSON.stringify({ t: 'scribe', ...binding.scope }) })
    session.doc.getText('body').insert(0, 'unsaved ')
    expect(text(session)).toBe('unsaved screen')
    expect(destroy).not.toHaveBeenCalled(); expect(socket.sent).toHaveLength(count)
    expect(appointed).not.toHaveBeenCalled(); expect(session.scribe).toBe(false)
    expect(ended).toHaveBeenCalledExactlyOnceWith('offline'); expect(socket.closed).toBe(true)
    session.close(); expect(destroy).toHaveBeenCalledOnce(); roomDoc.destroy()
  })

  it('keeps offline screen edits without reconnecting or claiming that closing the socket saved them', async () => {
    const binding = projectSession('A'); const socket = new ScopedSocket(); const roomDoc = roomDocument('screen')
    const captured = capturedRemote(binding); const factory = vi.fn(() => socket)
    const opening = openSession({ path: 'same.md', session: binding, socket: factory, seed: seeder().seed, ...captured })
    socket.onopen!(); socket.deliver(joinedFrame(binding, roomDoc))
    const session = (await opening)!; const destroy = vi.spyOn(session.doc, 'destroy')
    const count = socket.sent.length
    socket.onclose!({ code: 1006 })
    session.doc.getText('body').insert(0, 'offline ')
    expect(text(session)).toBe('offline screen'); expect(destroy).not.toHaveBeenCalled()
    expect(socket.sent).toHaveLength(count); expect(factory).toHaveBeenCalledOnce()
    expect(session.fence).toMatchObject({ expected_binding_generation: 7 })
    session.close(); roomDoc.destroy()
  })

  it('rejects a foreign ticket or a lease relabelled with newer G before creating a socket', () => {
    const binding = projectSession('A'); const other = projectSession('B')
    const captured = capturedRemote(other); const factory = vi.fn(() => new ScopedSocket())
    const options = { path: 'same.md', session: binding, socket: factory, seed: seeder().seed, ...captured }
    expect(() => openSession(options)).toThrow('does not match')
    const own = capturedRemote(binding)
    expect(() => openSession({ ...options, ...own, fence: { ...own.fence, expected_binding_generation: 8 } })).toThrow('does not match')
    expect(factory).not.toHaveBeenCalled()
  })
})


describe('materialization room frame revision (I8)', () => {
  it('invalidates even a duplicate update without writing or destroying the room', async () => {
    const binding = projectSession('A'); const socket = new ScopedSocket(); const roomDoc = roomDocument('screen')
    const captured = capturedRemote(binding)
    const opening = openSession({ path: 'same.md', session: binding, socket: () => socket,
      seed: seeder().seed, ...captured })
    socket.onopen!(); socket.deliver(joinedFrame(binding, roomDoc))
    const session = (await opening)!
    const before = session.materializationRevision
    const changed = vi.fn(); session.onChange(changed)
    session.receive({ t: 'update', update: toBase64(Y.encodeStateAsUpdate(roomDoc)), ...binding.scope })
    expect(session.materializationRevision).toBeGreaterThan(before)
    expect(changed).not.toHaveBeenCalled()
    expect(text(session)).toBe('screen')
    expect(session.doc.isDestroyed).toBe(false)
    const observed = session.materializationRevision
    session.end('offline')
    expect(session.materializationRevision).toBeGreaterThan(observed)
    session.close(); roomDoc.destroy()
  })
})
