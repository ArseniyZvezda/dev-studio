/**
 * A session against a stand-in for the backend and the room behind it (dev spec §16.4).
 *
 * The stand-in is the remote's room logic reduced to what an editor can observe: the first
 * seed wins, updates and cursors reach everyone else, the scribe's job is inherited. The
 * real one is tested where it lives (`remote/tests`, `backend/tests/test_remote_sync.py`);
 * what is checked here is that a session does the right thing with each answer.
 */

import { describe, expect, it } from 'vitest'
import * as Y from 'yjs'

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
