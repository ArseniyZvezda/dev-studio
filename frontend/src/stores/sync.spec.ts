import { createPinia, disposePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ProjectRemoteStatus, SyncStatus } from '@/api/types'
import { ProjectSession } from '@/session/project-session'
import type { onEvent } from '@/ws/connection'
import type { EventEnvelope, EventPayloads, EventType, RemoteBindingChange } from '@/ws/events'
import { createSyncStore, StaleRemoteBindingError } from './sync'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
const piniae: ReturnType<typeof createPinia>[] = []
afterEach(() => { piniae.splice(0).forEach(disposePinia); vi.restoreAllMocks() })
function metadata(overrides: Partial<ProjectRemoteStatus> = {}): ProjectRemoteStatus {
  return { project_id: 'A', epoch: 'runtime-A', backend_epoch: 'gate-A',
    persistent_id: 'business-id', identity_revision: 1, config_fingerprint: 'bytes',
    url: 'https://remote.example', enabled: false, has_token: true,
    binding_generation: 0, state: 'disabled', active_rooms: 0, pending_writes: 0, ...overrides }
}
function legacy(overrides: Partial<SyncStatus> = {}): SyncStatus {
  return { enabled: true, url: 'https://remote.example', space: 'legacy',
    has_token: true, insecure: false, state: 'connected', message: null, user: null, peers: [], ...overrides }
}
function harness(isLegacy = false) {
  const session = new ProjectSession(isLegacy ? null : 'A')
  if (!isLegacy) session.bind({ project_id: 'A', epoch: 'runtime-A' })
  const client = { sync: vi.fn<() => Promise<SyncStatus>>(),
    updateSync: vi.fn<(patch: object) => Promise<SyncStatus>>(),
    projectRemote: vi.fn<() => Promise<ProjectRemoteStatus>>() }
  const handlers = new Map<string, (data: never, envelope: never) => void>()
  const unsub = vi.fn()
  const listen = vi.fn((type: string, handler: (data: never, envelope: never) => void) => {
    handlers.set(type, handler); return unsub
  }) as unknown as typeof onEvent
  const pinia = createPinia()
  piniae.push(pinia)
  const store = createSyncStore(session, client, listen)(pinia)
  function emit<T extends EventType>(type: T, data: EventPayloads[T], envelope: Partial<EventEnvelope<T>> = {}) {
    handlers.get(type)?.(data as never, { type, data, seq: 1, ts: '',
      ...(isLegacy ? {} : { project_id: 'A', epoch: 'runtime-A' }), ...envelope } as never)
  }
  return { session, client, store, emit, listen, pinia, unsub }
}
async function seed(h: ReturnType<typeof harness>, row = metadata()) {
  h.client.projectRemote.mockResolvedValueOnce(row)
  expect(await h.store.loadRemote()).toBe(true)
}
function phase(generation: number, state: RemoteBindingChange['state'] = 'draining'): RemoteBindingChange {
  return { binding_generation: generation, state, change_id: 'change', expires_at: 123,
    expected_generation: 0, fenced: generation > 0, required_editor_acks: [], missing_acks: [], reason: null }
}
async function settle() { await Promise.resolve(); await Promise.resolve() }

describe('captured remote work', () => {
  it('starts idle; /sync and an unbound legacy renderer cannot supply G', async () => {
    const h = harness()
    expect(h.store.remote).toBeNull()
    expect(h.client.projectRemote).not.toHaveBeenCalled()
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
    h.client.sync.mockResolvedValueOnce(legacy())
    await h.store.load()
    expect(h.store.live).toBe(true)
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
    const old = harness(true)
    expect(await old.store.loadRemote()).toBe(false)
    expect(old.client.projectRemote).not.toHaveBeenCalled()
  })

  it('captures immutable runtime scope and distinct backend epoch/G BEFORE async', async () => {
    const h = harness()
    await seed(h)
    const ticket = h.store.capture()
    expect(Object.isFrozen(ticket)).toBe(true)
    expect(Object.isFrozen(ticket.runtime_scope)).toBe(true)
    expect(ticket.runtime_scope).toEqual({ project_id: 'A', epoch: 'runtime-A' })
    expect(ticket.backend_epoch).toBe('gate-A')
    const pending = deferred<string>()
    const result = h.store.run(ticket, captured => { expect(captured).toBe(ticket); return pending.promise })
    const rejected = expect(result).rejects.toThrow(StaleRemoteBindingError)
    await seed(h, metadata({ binding_generation: 1 }))
    pending.resolve('stale result')
    await rejected
    expect(ticket.expected_binding_generation).toBe(0)
    expect(() => ticket.assertCurrent()).toThrow(StaleRemoteBindingError)
    const operation = vi.fn(async () => 'must not send')
    await expect(h.store.run(ticket, operation)).rejects.toThrow(StaleRemoteBindingError)
    await expect(h.store.run({ ...h.store.capture() }, operation)).rejects.toThrow(StaleRemoteBindingError)
    const other = harness()
    await seed(other)
    await expect(other.store.run(h.store.capture(), operation)).rejects.toThrow(StaleRemoteBindingError)
    expect(operation).not.toHaveBeenCalled()
  })

  it('keeps same-G tickets across metadata refresh; propagates refusal without retry', async () => {
    const h = harness()
    await seed(h)
    const ticket = h.store.capture()
    await seed(h, metadata({ identity_revision: 2 }))
    expect(await h.store.run(ticket, async t => t.backend_epoch)).toBe('gate-A')
    const refusal = new Error('fence refusal')
    const operation = vi.fn(async () => { throw refusal })
    await expect(h.store.run(ticket, operation)).rejects.toBe(refusal)
    expect(operation).toHaveBeenCalledTimes(1)
  })

  it.each([
    { project_id: 'B' }, { epoch: 'old-runtime' },
    { binding_generation: -1 }, { binding_generation: 0.5 }, { binding_generation: 0 },
  ])('discards mismatched/old metadata: %j', async patch => {
    const h = harness()
    await seed(h, metadata({ binding_generation: 1 }))
    h.client.projectRemote.mockResolvedValueOnce(metadata(patch))
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.capture().expected_binding_generation).toBe(1)
    expect(h.store.remote?.backend_epoch).toBe('gate-A')
  })

  it('discards out-of-order initial reads and invalidated-runtime responses', async () => {
    const h = harness()
    const old = deferred<ProjectRemoteStatus>()
    h.client.projectRemote.mockReturnValueOnce(old.promise).mockResolvedValueOnce(metadata({ binding_generation: 2 }))
    const older = h.store.loadRemote()
    expect(await h.store.loadRemote()).toBe(true)
    old.resolve(metadata())
    expect(await older).toBe(false)
    const pending = deferred<ProjectRemoteStatus>()
    h.client.projectRemote.mockReturnValueOnce(pending.promise)
    const result = h.store.loadRemote()
    h.session.invalidate()
    pending.resolve(metadata({ binding_generation: 3 }))
    expect(await result).toBe(false)
    expect(h.store.remote?.binding_generation).toBe(2)
  })
})

describe('Ctx-only phase observations', () => {
  it('preserves same-G final-save/ACK tickets and can observe a later missed generation', async () => {
    const h = harness()
    await seed(h)
    const ticket = h.store.capture()
    h.store.subscribe()
    h.client.projectRemote.mockResolvedValueOnce(metadata({ state: 'quiescing' }))
    h.emit('remote.binding_change', phase(0))
    await settle()
    expect(await h.store.run(ticket, async captured => captured.expected_binding_generation)).toBe(0)
    expect(h.store.live).toBe(false)
    await seed(h, metadata({ binding_generation: 2 }))
    expect(() => ticket.assertCurrent()).toThrow(StaleRemoteBindingError)
  })

  it('fences before refetch; discards old-G replies/events without renderer rebind', async () => {
    const h = harness()
    await seed(h, metadata({ state: 'connected' }))
    h.store.subscribe(); h.store.subscribe()
    expect(h.listen).toHaveBeenCalledTimes(2)
    const ticket = h.store.capture()
    const scope = h.session.scope
    const invalidate = vi.spyOn(h.session, 'invalidate')
    const bind = vi.spyOn(h.session, 'bind')
    const old = deferred<ProjectRemoteStatus>()
    const fresh = deferred<ProjectRemoteStatus>()
    h.client.projectRemote.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const older = h.store.loadRemote()
    h.emit('remote.binding_change', phase(1), { seq: 10 })
    expect(h.store.live).toBe(false)
    expect(() => ticket.assertCurrent()).toThrow(StaleRemoteBindingError)
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
    old.resolve(metadata({ state: 'connected' }))
    expect(await older).toBe(false)
    fresh.resolve(metadata({ binding_generation: 1 }))
    await settle()
    expect(h.store.capture().expected_binding_generation).toBe(1)
    h.emit('remote.binding_change', phase(0), { seq: 11 })
    h.emit('remote.binding_change', phase(1, 'prepared'), { seq: 9 })
    h.emit('remote.binding_change', phase(2), { seq: 12, epoch: 'other-runtime' })
    h.emit('remote.binding_change', phase(2), { seq: 12, project_id: 'B' })
    h.emit('sync.status', legacy())
    expect(h.store.bindingChange?.state).toBe('draining')
    expect(h.store.status).toBeNull()
    expect(h.store.live).toBe(false)
    expect(h.client.projectRemote).toHaveBeenCalledTimes(4)
    expect(h.session.scope).toBe(scope)
    expect(h.session.generation).toBe(0)
    expect(invalidate).not.toHaveBeenCalled()
    expect(bind).not.toHaveBeenCalled()
  })

  it('keeps failed/wrong-G phase refresh fenced', async () => {
    const h = harness()
    await seed(h, metadata({ state: 'connected' }))
    h.store.subscribe()
    h.client.projectRemote.mockResolvedValueOnce(metadata())
    h.emit('remote.binding_change', phase(1))
    await settle()
    expect(h.store.remotePending).toBe(true)
    expect(h.store.live).toBe(false)
    h.client.projectRemote.mockRejectedValueOnce(new Error('unavailable'))
    expect(await h.store.loadRemote()).toBe(false)
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
  })

  it('cleans up subscriptions/tickets and rejects invalid-session events', async () => {
    const h = harness()
    await seed(h)
    h.store.subscribe()
    const ticket = h.store.capture()
    disposePinia(h.pinia)
    expect(h.unsub).toHaveBeenCalledTimes(2)
    expect(() => ticket.assertCurrent()).toThrow(StaleRemoteBindingError)
    h.session.invalidate()
    h.emit('remote.binding_change', phase(2))
    expect(h.client.projectRemote).toHaveBeenCalledTimes(1)
  })
})

describe('legacy compatibility', () => {
  it('loads and saves legacy state; transient secrets/refusals are not retained/retried', async () => {
    const h = harness(true)
    h.client.sync.mockResolvedValueOnce(legacy())
    await h.store.load()
    h.store.subscribe(); h.store.subscribe()
    const self = { user: { id: 'self', name: 'Self', color: '#fff' }, self: true, connections: 1, docs: [] }
    const other = { user: { id: 'other', name: 'Other', color: '#fff' }, self: false, connections: 1, docs: [] }
    h.emit('sync.status', legacy({ peers: [self, other] }))
    expect(h.store.others).toEqual([other])
    const pending = deferred<SyncStatus>()
    h.client.updateSync.mockReturnValueOnce(pending.promise)
    const saving = h.store.save({ enabled: false })
    expect(h.store.saving).toBe(true)
    pending.resolve(legacy({ enabled: false, state: 'disabled' }))
    expect((await saving).enabled).toBe(false)
    expect(h.store.live).toBe(false)
    expect(h.store.saving).toBe(false)
    const refusal = new Error('use_remote_settings')
    h.client.updateSync.mockRejectedValueOnce(refusal)
    await expect(h.store.save({ token: 'transient-only' })).rejects.toBe(refusal)
    expect(h.store.saving).toBe(false)
    expect(JSON.stringify(h.store.$state)).not.toContain('transient-only')
    expect(h.client.projectRemote).not.toHaveBeenCalled()
  })

  it('discards stale-G /sync GET/save responses with no upgrade/retry', async () => {
    const h = harness()
    await seed(h)
    const load = deferred<SyncStatus>(), save = deferred<SyncStatus>()
    h.client.sync.mockReturnValueOnce(load.promise)
    h.client.updateSync.mockReturnValueOnce(save.promise)
    const loading = h.store.load(), saving = h.store.save({ enabled: true })
    const rejected = expect(saving).rejects.toThrow(StaleRemoteBindingError)
    await seed(h, metadata({ binding_generation: 1 }))
    load.resolve(legacy()); save.resolve(legacy())
    await loading; await rejected
    expect(h.store.status).toBeNull()
    expect(h.store.live).toBe(false)
    expect(h.client.updateSync).toHaveBeenCalledTimes(1)
  })

  it('does not let an older save clear the newest saving indicator', async () => {
    const h = harness(true)
    const old = deferred<SyncStatus>(), fresh = deferred<SyncStatus>()
    h.client.updateSync.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const first = h.store.save({ enabled: true })
    const rejected = expect(first).rejects.toThrow(StaleRemoteBindingError)
    const second = h.store.save({ enabled: false })
    old.resolve(legacy()); await rejected
    expect(h.store.saving).toBe(true)
    fresh.resolve(legacy({ state: 'disabled' })); await second
    expect(h.store.saving).toBe(false)
  })
})

describe('independent review regressions', () => {
  it.each(['connected', 'offline'] as const)('uses an unversioned %s event only as a pessimistic refresh hint', async state => {
    const h = harness()
    await seed(h, metadata({ state: 'connected' }))
    const ticket = h.store.capture()
    h.store.subscribe()
    const pending = deferred<ProjectRemoteStatus>()
    h.client.projectRemote.mockReturnValueOnce(pending.promise)
    h.emit('sync.status', legacy({ state }))
    expect(h.store.live).toBe(false)
    expect(h.store.remotePending).toBe(true)
    expect(h.store.status).toBeNull()
    expect(h.client.projectRemote).toHaveBeenCalledTimes(2)
    pending.resolve(metadata({ state: 'offline' }))
    await settle()
    expect(h.store.remote?.state).toBe('offline')
    expect(h.store.remotePending).toBe(false)
    expect(h.store.live).toBe(false)
    expect(await h.store.run(ticket, async captured => captured.expected_binding_generation)).toBe(0)
  })

  it('ignores foreign hints and cannot remain live when a scoped hint refresh fails', async () => {
    const h = harness()
    await seed(h, metadata({ state: 'connected' }))
    h.store.subscribe()
    h.emit('sync.status', legacy(), { epoch: 'other-epoch' })
    h.emit('sync.status', legacy(), { project_id: 'B' })
    expect(h.client.projectRemote).toHaveBeenCalledTimes(1)
    expect(h.store.live).toBe(true)
    h.client.projectRemote.mockRejectedValueOnce(new Error('metadata unavailable'))
    h.emit('sync.status', legacy())
    await settle()
    expect(h.store.remote?.state).toBe('connected') // retained observation is NOT admission
    expect(h.store.remotePending).toBe(true)
    expect(h.store.live).toBe(false)
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
  })

  it.each(['draining', 'ready', 'blocked', 'paused'] as const)(
    'does not admit new rooms during %s, even after connected same-G GET; final-save/ACK tickets survive', async state => {
      const h = harness()
      await seed(h, metadata({ state: 'connected' }))
      const ticket = h.store.capture()
      h.store.subscribe()
      h.client.projectRemote.mockResolvedValueOnce(metadata({ state: 'connected' }))
      h.emit('remote.binding_change', phase(0, state))
      expect(h.store.live).toBe(false)
      expect(await h.store.run(ticket, async captured => captured.expected_binding_generation)).toBe(0)
      await settle()
      expect(h.store.remotePending).toBe(false)
      expect(h.store.remote?.state).toBe('connected')
      expect(h.store.live).toBe(false)
      expect(await h.store.run(ticket, async captured => captured.expected_binding_generation)).toBe(0)
    },
  )

  it('clears old-G status/peers and pending compatibility reads on explicit G advance, not a same-G refresh', async () => {
    const h = harness()
    await seed(h)
    const peer = { user: { id: 'old', name: 'Old', color: '#fff' }, self: false, connections: 1, docs: [] }
    h.client.sync.mockResolvedValueOnce(legacy({ peers: [peer] }))
    await h.store.load()
    await seed(h)
    expect(h.store.others).toEqual([peer])
    const pending = deferred<SyncStatus>()
    h.client.sync.mockReturnValueOnce(pending.promise)
    const loading = h.store.load()
    await seed(h, metadata({ binding_generation: 1 }))
    expect(h.store.status).toBeNull()
    expect(h.store.others).toEqual([])
    pending.resolve(legacy({ peers: [peer] }))
    await loading
    expect(h.store.status).toBeNull()
    expect(h.store.others).toEqual([])
  })

  it('replaces a late old-G refusal with StaleRemoteBindingError without retrying or upgrading', async () => {
    const h = harness()
    await seed(h)
    const ticket = h.store.capture()
    let reject!: (reason: unknown) => void
    const pending = new Promise<string>((_, no) => { reject = no })
    const operation = vi.fn(() => pending)
    const result = h.store.run(ticket, operation)
    const rejected = expect(result).rejects.toThrow(StaleRemoteBindingError)
    await seed(h, metadata({ binding_generation: 1 }))
    reject(new Error('old binding refusal'))
    await rejected
    expect(operation).toHaveBeenCalledExactlyOnceWith(ticket)
    expect(ticket.expected_binding_generation).toBe(0)
    expect(h.store.capture().expected_binding_generation).toBe(1)
  })
})


describe('safe monotonic G and restart fences', () => {
  it.each([Number.MAX_SAFE_INTEGER + 1, 2 ** 62, NaN, Infinity, '9'])(
    'refuses a non-lossless generation %s, never upgrading a captured callback', async value => {
      const h = harness()
      await seed(h, metadata({ binding_generation: 7 }))
      const ticket = h.store.capture()
      h.client.projectRemote.mockResolvedValueOnce(metadata({ binding_generation: value as number }))
      expect(await h.store.loadRemote()).toBe(false)
      expect(ticket.expected_binding_generation).toBe(7)
      expect(h.store.remote?.binding_generation).toBe(7)
    })

  it('admits successive fence/commit G and loses no precision at the safe boundary', async () => {
    const h = harness()
    await seed(h, metadata({ binding_generation: Number.MAX_SAFE_INTEGER - 2 }))
    const old = h.store.capture()
    await seed(h, metadata({ binding_generation: Number.MAX_SAFE_INTEGER - 1, state: 'quiescing' }))
    expect(() => old.assertCurrent()).toThrow(StaleRemoteBindingError)
    await seed(h, metadata({ binding_generation: Number.MAX_SAFE_INTEGER, persistent_id: 'new-id' }))
    expect(h.store.capture().expected_binding_generation).toBe(Number.MAX_SAFE_INTEGER)
    h.client.projectRemote.mockResolvedValueOnce(metadata({ binding_generation: Number.MAX_SAFE_INTEGER - 1 }))
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.remote?.persistent_id).toBe('new-id')
  })

  it('quarantines a restarted backend and never revives old tickets or late replies', async () => {
    const h = harness()
    await seed(h, metadata({ binding_generation: 9, state: 'connected' }))
    const old = h.store.capture()
    expect(h.store.reloadRequired).toBe(false)
    const late = deferred<ProjectRemoteStatus>()
    h.client.projectRemote.mockReturnValueOnce(late.promise)
    const pending = h.store.loadRemote()
    h.client.projectRemote.mockResolvedValueOnce(metadata({ backend_epoch: 'gate-B', binding_generation: 0 }))
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.live).toBe(false)
    expect(h.store.remotePending).toBe(true)
    expect(h.store.reloadRequired).toBe(true)
    expect(() => old.assertCurrent()).toThrow(StaleRemoteBindingError)
    expect(() => h.store.capture()).toThrow(StaleRemoteBindingError)
    late.resolve(metadata({ binding_generation: 10, state: 'connected' }))
    expect(await pending).toBe(false)
    expect(await h.store.loadRemote()).toBe(false) // no old epoch revival from refresh
    const operation = vi.fn(async () => 'must not run')
    await expect(h.store.run(old, operation)).rejects.toThrow(StaleRemoteBindingError)
    expect(operation).not.toHaveBeenCalled()
    expect(h.store.remote?.backend_epoch).toBe('gate-A') // display only
    expect(h.session.scope).toEqual({ project_id: 'A', epoch: 'runtime-A' })
    const fresh = harness()
    await seed(fresh, metadata({ backend_epoch: 'gate-B', binding_generation: 0 }))
    await expect(fresh.store.run(old, operation)).rejects.toThrow(StaleRemoteBindingError)
    expect(fresh.store.capture().backend_epoch).toBe('gate-B')
  })
})
