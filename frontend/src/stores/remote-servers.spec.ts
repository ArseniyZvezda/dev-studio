/** Renderer-local, secret-free metadata contract (remote-identity/studio-design §5.1/§10.1).
 * Mock the local API boundary, not Remote transport. Deferred replies deliberately arrive
 * out of order: a stale response must fail, not silently become a successful empty list.
 */
import { createPinia, disposePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { createRemoteApiClient, type RemoteServerProfile, type RemoteServers,
  type VerificationDTO } from '@/api/remote'
import { ProjectSession } from '@/session/project-session'
import { createRemoteServersStore, type RemoteServerMutationTicket } from './remote-servers'

vi.mock('@/api/remote', () => ({ createRemoteApiClient: vi.fn() }))

type RemoteClient = ReturnType<typeof createRemoteApiClient>
const id = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const newId = '33333333-3333-4333-8333-333333333333'
const url = 'https://remote.example'
const secret = 'REMOTE-CREDENTIAL-CANARY-NEVER-IN-PINIA'
const piniae: ReturnType<typeof createPinia>[] = []

function checked(overrides: Partial<VerificationDTO> = {}): VerificationDTO {
  return {
    state: 'valid', checked_at: '2026-11-16T12:00:00Z',
    user: { id: 'alice', display_name: 'Alice', color: '#123456' },
    wire_protocol: 2, catalog_version: 2, error_code: null, ...overrides,
  }
}
function profile(overrides: Partial<RemoteServerProfile> = {}): RemoteServerProfile {
  return { profile_id: id, revision: 7, name: '  Historical label  ', url,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' }, ...overrides }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function makeClient() {
  return {
    listServers: vi.fn<RemoteClient['listServers']>(),
    verify: vi.fn<RemoteClient['verify']>(),
    createProfile: vi.fn<RemoteClient['createProfile']>(),
    renameProfile: vi.fn<RemoteClient['renameProfile']>(),
    changeProfileUrl: vi.fn<RemoteClient['changeProfileUrl']>(),
    setCredential: vi.fn<RemoteClient['setCredential']>(),
    clearCredential: vi.fn<RemoteClient['clearCredential']>(),
    deleteProfile: vi.fn<RemoteClient['deleteProfile']>(),
    recoverProfile: vi.fn<RemoteClient['recoverProfile']>(),
    listProjects: vi.fn<RemoteClient['listProjects']>(),
    createProject: vi.fn<RemoteClient['createProject']>(),
    observe: vi.fn<RemoteClient['observe']>(),
  } satisfies RemoteClient
}
function harness(projectId = 'A') {
  const session = new ProjectSession(projectId)
  session.bind({ project_id: projectId, epoch: `epoch-${projectId}` })
  const api = makeClient()
  vi.mocked(createRemoteApiClient).mockReturnValueOnce(api)
  const useStore = createRemoteServersStore(session)
  const pinia = createPinia()
  piniae.push(pinia)
  return { session, api, pinia, useStore, store: useStore(pinia) }
}
type Harness = ReturnType<typeof harness>
async function seed(h: Harness, items = [profile()]) {
  h.api.listServers.mockResolvedValueOnce({ items })
  expect(await h.store.load()).toBe(true)
}
function ticket(h: Harness, profileId = id): RemoteServerMutationTicket {
  const result = h.store.beginMutation(profileId)
  expect(result).not.toBeNull()
  return result!
}
function expectChecked(h: Harness, p: RemoteServerProfile, result: VerificationDTO) {
  expect(h.store.lastChecked[p.profile_id]).toEqual({
    profile_id: p.profile_id, revision: p.revision, url: p.url, result,
  })
}
function snapshot(h: Harness) { return JSON.parse(JSON.stringify(h.store.$state)) }

beforeEach(() => {
  vi.mocked(createRemoteApiClient).mockReset()
  // Any direct network, persistence or shell side effect is a contract violation.
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Use the local API seam')))
  vi.stubGlobal('WebSocket', vi.fn())
  const storage = () => ({ getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn(), clear: vi.fn() })
  vi.stubGlobal('localStorage', storage())
  vi.stubGlobal('sessionStorage', storage())
  vi.stubGlobal('indexedDB', { open: vi.fn(), deleteDatabase: vi.fn() })
})
afterEach(() => {
  for (const pinia of piniae.splice(0)) disposePinia(pinia)
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('explicit metadata operations', () => {
  it('returns a Pinia definition, binds only the injected renderer and starts idle', async () => {
    vi.useFakeTimers()
    const h = harness()
    expect(h.useStore(h.pinia)).toBe(h.store)
    expect(createRemoteApiClient).toHaveBeenCalledExactlyOnceWith(h.session)
    expect(h.store.$state).toEqual({ items: [], loaded: false, loading: false, error: null,
      lastChecked: {}, busy: {}, errors: {} })
    await vi.advanceTimersByTimeAsync(60_000)
    for (const method of Object.values(h.api)) expect(method).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it('loads metadata explicitly without checking, changing credentials or selecting a project', async () => {
    const h = harness()
    const pending = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(pending.promise)
    const result = h.store.load()
    expect(h.store.loading).toBe(true)
    expect(h.store.loaded).toBe(false)
    expect(h.store.items).toEqual([])
    pending.resolve({ items: [profile()] })
    expect(await result).toBe(true)
    expect(h.store.items).toEqual([profile()])
    expect(h.store.loaded).toBe(true)
    expect(h.store.loading).toBe(false)
    expect(h.store.error).toBeNull()
    expect(h.api.listServers).toHaveBeenCalledExactlyOnceWith()
    for (const [name, method] of Object.entries(h.api)) {
      if (name !== 'listServers') expect(method).not.toHaveBeenCalled()
    }
    expect(h.session.scope).toEqual({ project_id: 'A', epoch: 'epoch-A' })
  })

  it.each(['unavailable', 'unchecked', 'checking'] as const)(
    'keeps %s distinct and does not invent a timestamp, user or last checked result', async state => {
      const h = harness()
      await seed(h, [profile({ verification: { state } })])
      expect(h.store.items[0]!.verification).toEqual({ state })
      expect(h.store.lastChecked).toEqual({})
      expect(h.api.verify).not.toHaveBeenCalled()
    })

  it.each(['valid', 'unauthorised', 'offline', 'incompatible', 'insecure_blocked', 'error'] as const)(
    'retains a checked %s DTO, its exact binding and its nullable fields', async state => {
      const h = harness()
      const result = checked({ state, user: null, wire_protocol: null, catalog_version: null,
        error_code: state === 'valid' ? null : 'remote_error' })
      const p = profile({ verification: result })
      await seed(h, [p])
      expect(h.store.items).toEqual([p])
      expectChecked(h, p, result)
    })

  it('treats an empty success as loaded, not as a failure', async () => {
    const h = harness()
    await seed(h, [])
    expect(h.store.$state).toEqual({ items: [], loaded: true, loading: false, error: null,
      lastChecked: {}, busy: {}, errors: {} })
  })

  it.each([
    { cause: new ApiError(503, secret), status: 503 },
    { cause: new Error(secret), status: null },
    { cause: { message: secret, status: 418, request: { token: secret } }, status: null },
  ])('reports a first-load failure safely without claiming successful empty data ($status)', async ({ cause, status }) => {
    const h = harness()
    h.api.listServers.mockRejectedValueOnce(cause)
    expect(await h.store.load()).toBe(false)
    expect(h.store.items).toEqual([])
    expect(h.store.loaded).toBe(false)
    expect(h.store.loading).toBe(false)
    expect(h.store.error).toEqual({ kind: 'request_failed', status })
    expect(JSON.stringify(h.store.$state)).not.toContain(secret)
  })

  it('keeps prior data and checked history on load failure, then clears the error on explicit retry', async () => {
    const h = harness()
    const p = profile({ verification: checked() })
    await seed(h, [p])
    h.api.listServers.mockRejectedValueOnce(new ApiError(401, secret))
    expect(await h.store.load()).toBe(false)
    expect(h.store.loaded).toBe(true)
    expect(h.store.items).toEqual([p])
    expectChecked(h, p, checked())
    expect(h.store.error).toEqual({ kind: 'request_failed', status: 401 })
    const pending = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(pending.promise)
    const retry = h.store.load()
    expect(h.store.error).toBeNull()
    expect(h.store.items).toEqual([p])
    pending.resolve({ items: [] })
    expect(await retry).toBe(true)
    expect(h.store.items).toEqual([])
    expect(h.store.lastChecked).toEqual({})
    expect(h.api.listServers).toHaveBeenCalledTimes(3)
  })

  it.each([false, true])('rejects duplicate profile IDs as a whole-list failure (previously loaded=%s)', async previouslyLoaded => {
    const h = harness()
    const original = [profile({ verification: checked() }), profile({ profile_id: otherId })]
    if (previouslyLoaded) await seed(h, original)
    const before = snapshot(h)
    h.api.listServers.mockResolvedValueOnce({ items: [
      profile({ profile_id: newId, revision: 1, name: 'Do not partially add' }),
      profile({ revision: 8, name: 'Do not partially update' }),
      profile({ revision: 9, name: 'Duplicate ID with another revision' }),
    ] })
    expect(await h.store.load()).toBe(false)
    expect(h.store.items).toEqual(before.items)
    expect(h.store.loaded).toBe(previouslyLoaded)
    expect(h.store.lastChecked).toEqual(before.lastChecked)
    expect(h.store.loading).toBe(false)
    expect(h.store.error).toEqual({ kind: 'request_failed', status: null })
    expect(h.api.verify).not.toHaveBeenCalled()
    // Failure must not tombstone previously present IDs or keep partial incoming rows.
    await seed(h, original)
    expect(h.store.items).toEqual(original)
  })
})

describe('checked history and metadata binding', () => {
  it('retains the last checked DTO through unknown metadata only for the exact same binding', async () => {
    const h = harness()
    const p = profile({ verification: checked() })
    await seed(h, [p])
    for (const state of ['unavailable', 'unchecked', 'checking'] as const) {
      await seed(h, [profile({ name: 'New label', verification: { state } })])
      expect(h.store.items[0]!.verification).toEqual({ state })
      expectChecked(h, p, checked())
    }
    await seed(h, [profile({ revision: 8 })])
    expect(h.store.lastChecked).toEqual({})
  })

  it('does not share a checked result across different profile IDs even at the same revision and URL', async () => {
    const h = harness()
    await seed(h, [profile({ verification: checked() }), profile({ profile_id: otherId })])
    expect(Object.keys(h.store.lastChecked)).toEqual([id])
    await seed(h, [profile({ profile_id: otherId })])
    expect(h.store.lastChecked).toEqual({})
  })

  it.each([
    { revision: 6, url },
    { revision: 7, url: 'https://wrong-binding.example' },
  ])('never downgrades known metadata to revision $revision / $url', async incoming => {
    const h = harness()
    const p = profile({ verification: checked() })
    await seed(h, [p])
    await seed(h, [profile({ ...incoming, name: 'Stale', verification: checked({ state: 'offline' }) })])
    expect(h.store.items).toEqual([p])
    expectChecked(h, p, checked())
  })

  it('does not let a stale checked GET replace a newer failed last check with green status', async () => {
    const h = harness()
    await seed(h)
    const latest = checked({ state: 'offline', checked_at: '2026-11-16T12:10:00Z',
      user: null, error_code: 'transport_unavailable' })
    h.api.verify.mockResolvedValueOnce(latest)
    expect(await h.store.verify(id)).toBe(true)
    await seed(h, [profile({ name: 'Refreshed metadata', verification: checked() })])
    expect(h.store.items).toEqual([profile({ name: 'Refreshed metadata', verification: latest })])
    expectChecked(h, profile(), latest)
    expect(h.api.verify).toHaveBeenCalledTimes(1)
  })

  it('copies list DTOs and nested checked users rather than retaining caller-owned references or extras', async () => {
    const h = harness()
    const result = checked()
    const rawResult = { ...result, token: secret, user: { ...result.user!, authorization: secret } }
    const untrusted = { ...profile({ verification: rawResult }), token: secret, request: { token: secret } }
    await seed(h, [untrusted])
    expect(h.store.items).toEqual([profile({ verification: result })])
    expectChecked(h, profile(), result)
    untrusted.name = secret
    untrusted.verification = { state: 'unavailable' }
    rawResult.user.display_name = secret
    expect(h.store.items[0]!.name).toBe('  Historical label  ')
    expect(JSON.stringify(h.store.$state)).not.toContain(secret)
  })
})

describe('overlapping metadata loads', () => {
  it.each(['resolve', 'reject'] as const)('ignores an older load that will %s after the latest success', async outcome => {
    const h = harness()
    const old = deferred<RemoteServers>()
    const latest = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = h.store.load()
    const b = h.store.load()
    latest.resolve({ items: [profile({ revision: 8 })] })
    expect(await b).toBe(true)
    const before = snapshot(h)
    if (outcome === 'resolve') old.resolve({ items: [] })
    else old.reject(new Error(secret))
    expect(await a).toBe(false)
    expect(snapshot(h)).toEqual(before)
  })

  it('does not let an older completion clear loading while the newest request is pending', async () => {
    const h = harness()
    const old = deferred<RemoteServers>()
    const latest = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = h.store.load()
    const b = h.store.load()
    old.resolve({ items: [profile()] })
    expect(await a).toBe(false)
    expect(h.store.loading).toBe(true)
    expect(h.store.loaded).toBe(false)
    latest.resolve({ items: [] })
    expect(await b).toBe(true)
    expect(h.store.loading).toBe(false)
  })

  it('does not fall back to an older successful load when the latest load fails', async () => {
    const h = harness()
    await seed(h)
    const old = deferred<RemoteServers>()
    const latest = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = h.store.load()
    const b = h.store.load()
    latest.reject(new ApiError(503, secret))
    expect(await b).toBe(false)
    const before = snapshot(h)
    old.resolve({ items: [] })
    expect(await a).toBe(false)
    expect(snapshot(h)).toEqual(before)
    expect(h.store.items).toEqual([profile()])
    expect(h.store.error).toEqual({ kind: 'request_failed', status: 503 })
  })

  it.each(['success', 'failure'] as const)('fences a load started before Verify, including Verify %s', async outcome => {
    const h = harness()
    await seed(h)
    const list = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    const loading = h.store.load()
    if (outcome === 'success') h.api.verify.mockResolvedValueOnce(checked())
    else h.api.verify.mockRejectedValueOnce(new ApiError(502, secret))
    expect(await h.store.verify(id)).toBe(outcome === 'success')
    const before = snapshot(h)
    list.resolve({ items: [] })
    expect(await loading).toBe(false)
    expect(h.store.items).toEqual(before.items)
    expect(h.store.lastChecked).toEqual(before.lastChecked)
    expect(h.store.errors).toEqual(before.errors)
    expect(h.store.loading).toBe(false)
  })

  it('fences a load started during Verify if Verify completes before the load', async () => {
    const h = harness()
    await seed(h)
    const check = deferred<VerificationDTO>()
    const list = deferred<RemoteServers>()
    h.api.verify.mockReturnValueOnce(check.promise)
    h.api.listServers.mockReturnValueOnce(list.promise)
    const checking = h.store.verify(id)
    const loading = h.store.load()
    check.resolve(checked())
    expect(await checking).toBe(true)
    list.resolve({ items: [profile()] })
    expect(await loading).toBe(false)
    expectChecked(h, profile(), checked())
  })

  it.each(['success', 'failure'] as const)('fences a load when a mutation begins after it (%s)', async outcome => {
    const h = harness()
    await seed(h)
    const list = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    const loading = h.store.load()
    const t = ticket(h)
    if (outcome === 'success') expect(h.store.applyMutation(t, profile({ revision: 8 }))).toBe(true)
    else expect(h.store.endMutation(t)).toBe(true)
    const before = snapshot(h)
    list.resolve({ items: [] })
    expect(await loading).toBe(false)
    expect(h.store.items).toEqual(before.items)
    expect(h.store.errors).toEqual(before.errors)
  })

  it.each(['existing', 'create'] as const)('fences a load begun while a %s mutation is pending even if it finishes first', async kind => {
    const h = harness()
    await seed(h)
    const t = kind === 'existing' ? ticket(h) : h.store.beginMutation()!
    const list = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    const loading = h.store.load()
    expect(h.store.endMutation(t, false)).toBe(true)
    list.resolve({ items: [] })
    expect(await loading).toBe(false)
    expect(h.store.items).toEqual([profile()])
    // A subsequent, explicit load is allowed; there is no hidden refresh/retry.
    await seed(h)
    expect(h.api.listServers).toHaveBeenCalledTimes(3)
  })
})

describe('explicit Verify and per-profile races', () => {
  it('sends only saved ID/revision, never optimistic checked success, and stores the exact checked result', async () => {
    const h = harness()
    await seed(h)
    const pending = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(pending.promise)
    const result = h.store.verify(id)
    expect(h.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
    expect(h.store.busy).toEqual({ [id]: 'verify' })
    expect(h.store.items[0]!.verification).toEqual({ state: 'unavailable' })
    expect(h.store.lastChecked).toEqual({})
    pending.resolve(checked({ state: 'offline', user: null, error_code: 'transport_unavailable' }))
    expect(await result).toBe(true)
    expect(h.store.items[0]!.verification.state).toBe('offline')
    expectChecked(h, profile(), checked({ state: 'offline', user: null, error_code: 'transport_unavailable' }))
    expect(h.store.busy).toEqual({})
    expect(h.store.errors).toEqual({})
  })

  it('refuses unknown profiles and Verify while the saved profile is mutating', async () => {
    const h = harness()
    expect(await h.store.verify(id)).toBe(false)
    await seed(h)
    const t = ticket(h)
    expect(await h.store.verify(id)).toBe(false)
    expect(await h.store.verify(otherId)).toBe(false)
    expect(h.api.verify).not.toHaveBeenCalled()
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    expect(h.store.endMutation(t, false)).toBe(true)
  })

  it('preserves checked data on request failure, exposes only sanitized per-ID errors, and retries only explicitly', async () => {
    const h = harness()
    const p = profile({ verification: checked() })
    await seed(h, [p])
    h.api.verify.mockRejectedValueOnce(new ApiError(502, secret))
    expect(await h.store.verify(id)).toBe(false)
    expect(h.store.items).toEqual([p])
    expectChecked(h, p, checked())
    expect(h.store.error).toBeNull()
    expect(h.store.errors).toEqual({ [id]: { kind: 'request_failed', status: 502 } })
    expect(h.store.busy).toEqual({})
    expect(h.api.verify).toHaveBeenCalledTimes(1)
    const next = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(next.promise)
    const retry = h.store.verify(id)
    expect(h.store.errors).toEqual({})
    next.resolve(checked({ checked_at: '2026-11-16T12:01:00Z' }))
    expect(await retry).toBe(true)
  })

  it('lets explicit offline Verify replace prior valid status even when the backend clock moves backwards', async () => {
    const h = harness()
    const prior = checked({ checked_at: '2026-11-16T12:10:00Z' })
    await seed(h, [profile({ verification: prior })])
    const latest = checked({ state: 'offline', checked_at: '2026-11-16T12:00:00Z',
      user: null, error_code: 'transport_unavailable' })
    h.api.verify.mockResolvedValueOnce(latest)
    expect(await h.store.verify(id)).toBe(true)
    expect(h.store.items[0]!.verification).toEqual(latest)
    expectChecked(h, profile(), latest)
    expect(h.store.busy).toEqual({})
    expect(h.store.errors).toEqual({})
  })

  it.each(['resolve', 'reject'] as const)('ignores an older same-binding Verify that will %s after the newer result', async outcome => {
    const h = harness()
    await seed(h)
    const old = deferred<VerificationDTO>()
    const latest = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = h.store.verify(id)
    const b = h.store.verify(id)
    latest.resolve(checked())
    expect(await b).toBe(true)
    const before = snapshot(h)
    if (outcome === 'resolve') old.resolve(checked({ state: 'offline' }))
    else old.reject(new Error(secret))
    expect(await a).toBe(false)
    expect(snapshot(h)).toEqual(before)
  })

  it('keeps the newest Verify busy and its failure authoritative over an older successful Verify', async () => {
    const h = harness()
    await seed(h)
    const old = deferred<VerificationDTO>()
    const latest = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const a = h.store.verify(id)
    const b = h.store.verify(id)
    old.resolve(checked())
    expect(await a).toBe(false)
    expect(h.store.busy).toEqual({ [id]: 'verify' })
    expect(h.store.lastChecked).toEqual({})
    latest.reject(new Error(secret))
    expect(await b).toBe(false)
    expect(h.store.busy).toEqual({})
    expect(h.store.errors).toEqual({ [id]: { kind: 'request_failed', status: null } })
  })

  it.each([
    { label: 'same URL but new revision', next: profile({ revision: 8 }) },
    { label: 'new URL and new revision', next: profile({ revision: 8, url: 'https://new.example' }) },
  ])('rejects old Verify after loading $label', async ({ next }) => {
    const h = harness()
    await seed(h)
    const check = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(check.promise)
    const pending = h.store.verify(id)
    await seed(h, [next])
    check.resolve(checked())
    expect(await pending).toBe(false)
    expect(h.store.items).toEqual([next])
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.busy).toEqual({})
    h.api.verify.mockResolvedValueOnce(checked())
    expect(await h.store.verify(id)).toBe(true)
    expect(h.api.verify).toHaveBeenLastCalledWith(id, 8)
    expectChecked(h, next, checked())
  })

  it('accepts an in-flight Verify after a metadata refresh of its exact same binding', async () => {
    const h = harness()
    await seed(h)
    const check = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(check.promise)
    const pending = h.store.verify(id)
    await seed(h, [profile({ name: 'Refreshed label' })])
    expect(h.store.busy).toEqual({ [id]: 'verify' })
    check.resolve(checked())
    expect(await pending).toBe(true)
    expect(h.store.items[0]!.name).toBe('Refreshed label')
    expectChecked(h, profile(), checked())
  })

  it('keeps busy/errors independent for different IDs, including simultaneous mutation and Verify', async () => {
    const h = harness()
    await seed(h, [profile(), profile({ profile_id: otherId, url: 'https://other.example' })])
    const a = deferred<VerificationDTO>()
    const b = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    const checkingA = h.store.verify(id)
    const checkingB = h.store.verify(otherId)
    a.reject(new ApiError(401, secret))
    expect(await checkingA).toBe(false)
    expect(h.store.busy).toEqual({ [otherId]: 'verify' })
    const t = ticket(h)
    expect(h.store.errors).toEqual({})
    expect(h.store.busy).toEqual({ [id]: 'mutation', [otherId]: 'verify' })
    b.resolve(checked())
    expect(await checkingB).toBe(true)
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    expect(h.store.endMutation(t)).toBe(true)
    expect(h.store.errors).toEqual({ [id]: { kind: 'request_failed', status: null } })
    expect(Object.keys(h.store.lastChecked)).toEqual([otherId])
  })
})

describe('confirmed, secret-free form mutation tickets', () => {
  it('captures intent before the direct form call, leaves old metadata intact, and accepts only the confirmed DTO', async () => {
    const h = harness()
    await seed(h)
    const direct = deferred<RemoteServerProfile>()
    h.api.renameProfile.mockReturnValueOnce(direct.promise)
    const t = ticket(h)
    expect(h.api.renameProfile).not.toHaveBeenCalled()
    expect(h.store.items).toEqual([profile()])
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    const request = h.api.renameProfile(id, { expected_revision: 7, name: 'Renamed' })
    direct.resolve(profile({ revision: 8, name: 'Renamed' }))
    expect(h.store.applyMutation(t, await request)).toBe(true)
    expect(h.store.items).toEqual([profile({ revision: 8, name: 'Renamed' })])
    expect(h.store.busy).toEqual({})
    expect(h.store.applyMutation(t, profile({ revision: 9 }))).toBe(false)
    expect(h.store.endMutation(t)).toBe(false)
    expect(h.api.listServers).toHaveBeenCalledTimes(1)
    expect(h.api.verify).not.toHaveBeenCalled()
  })

  it.each([
    { label: 'equal revision', next: profile() },
    { label: 'older revision', next: profile({ revision: 6 }) },
    { label: 'same revision with changed URL', next: profile({ url: 'https://new.example' }) },
    { label: 'different profile ID', next: profile({ profile_id: otherId, revision: 8 }) },
  ])('refuses a confirmed DTO with $label and consumes the ticket without optimistic changes', async ({ next }) => {
    const h = harness()
    await seed(h)
    const t = ticket(h)
    expect(h.store.applyMutation(t, next)).toBe(false)
    expect(h.store.items).toEqual([profile()])
    expect(h.store.busy).toEqual({})
    expect(h.store.applyMutation(t, profile({ revision: 9 }))).toBe(false)
  })

  it('invalidates old checked history on a confirmed new binding and records only its own checked DTO', async () => {
    const h = harness()
    await seed(h, [profile({ verification: checked() })])
    const renamed = profile({ revision: 8 })
    expect(h.store.applyMutation(ticket(h), renamed)).toBe(true)
    expect(h.store.lastChecked).toEqual({})
    const moved = profile({ revision: 9, url: 'https://new.example', verification: checked({ user: null }) })
    expect(h.store.applyMutation(ticket(h), moved)).toBe(true)
    expectChecked(h, moved, checked({ user: null }))
  })

  it('copies a confirmed DTO including nested user data instead of retaining the form result object', async () => {
    const h = harness()
    await seed(h)
    const next = profile({ revision: 8, verification: checked() })
    expect(h.store.applyMutation(ticket(h), next)).toBe(true)
    next.name = 'Caller edited after completion'
    next.url = 'https://caller.example'
    if ('user' in next.verification) next.verification.user!.display_name = 'Caller changed user'
    expect(h.store.items).toEqual([profile({ revision: 8, verification: checked() })])
    expectChecked(h, profile({ revision: 8 }), checked())
  })

  it('adds a keyless Create only after confirmation and cannot overwrite an existing profile', async () => {
    const h = harness()
    await seed(h)
    const t = h.store.beginMutation()!
    expect(h.store.items).toEqual([profile()])
    expect(h.store.busy).toEqual({})
    expect(h.store.applyMutation(t, profile({ profile_id: newId, has_token: false,
      credential_source: 'none', revision: 1 }))).toBe(true)
    expect(h.store.items).toHaveLength(2)
    expect(h.store.applyMutation(h.store.beginMutation()!, profile({ revision: 99 }))).toBe(false)
    expect(h.store.items.find(p => p.profile_id === id)).toEqual(profile())
    expect(h.api.createProfile).not.toHaveBeenCalled()
  })

  it('refuses unknown IDs and forged tickets without effects', async () => {
    const h = harness()
    await seed(h)
    const before = snapshot(h)
    expect(h.store.beginMutation(otherId)).toBeNull()
    const forged = Object.freeze({}) as RemoteServerMutationTicket
    expect(h.store.applyMutation(forged, profile({ revision: 8 }))).toBe(false)
    expect(h.store.applyRemoval(forged)).toBe(false)
    expect(h.store.endMutation(forged)).toBe(false)
    expect(snapshot(h)).toEqual(before)
  })

  it.each([true, false])('ends a failed/cancelled mutation with failed=%s using no raw error argument or recovery', async failed => {
    const h = harness()
    const p = profile({ verification: checked() })
    await seed(h, [p])
    const t = ticket(h)
    expect(h.store.endMutation(t, failed)).toBe(true)
    expect(h.store.items).toEqual([p])
    expectChecked(h, p, checked())
    expect(h.store.errors).toEqual(failed ? { [id]: { kind: 'request_failed', status: null } } : {})
    expect(h.store.busy).toEqual({})
    expect(h.api.recoverProfile).not.toHaveBeenCalled()
    expect(h.api.verify).not.toHaveBeenCalled()
    expect(h.api.listServers).toHaveBeenCalledTimes(1)
  })

  it.each(['success', 'failure'] as const)('serializes same-ID mutations without dispatching a second form call while the first ends in %s', async outcome => {
    const h = harness()
    await seed(h)
    const first = ticket(h)
    const direct = deferred<RemoteServerProfile>()
    h.api.renameProfile.mockReturnValueOnce(direct.promise)
    const saving = h.api.renameProfile(id, { expected_revision: 7, name: 'First rename' })
    const before = snapshot(h)
    const blocked = h.store.beginMutation(id)
    expect(blocked).toBeNull()
    // The future form must check the ticket before API dispatch, including a failed CAS.
    if (blocked) await h.api.setCredential(id, { expected_revision: 7, token: secret })
    expect(h.api.setCredential).not.toHaveBeenCalled()
    expect(snapshot(h)).toEqual(before)
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    if (outcome === 'success') {
      direct.resolve(profile({ revision: 8, name: 'First rename' }))
      expect(h.store.applyMutation(first, await saving)).toBe(true)
    } else {
      direct.reject(new ApiError(409, secret))
      await expect(saving).rejects.toBeInstanceOf(ApiError)
      expect(h.store.endMutation(first)).toBe(true)
    }
    expect(h.store.items[0]!.revision).toBe(outcome === 'success' ? 8 : 7)
    expect(h.store.busy).toEqual({})
    // Releasing the lock allows a new explicit intent, not an automatic retry.
    expect(h.store.endMutation(ticket(h), false)).toBe(true)
    expect(h.api.renameProfile).toHaveBeenCalledTimes(1)
    expect(h.api.setCredential).not.toHaveBeenCalled()
  })

  it('preserves confirmed first deletion when a later same-ID form intent cannot dispatch', async () => {
    const h = harness()
    await seed(h, [profile({ verification: checked() })])
    const first = ticket(h)
    const direct = deferred<void>()
    h.api.deleteProfile.mockReturnValueOnce(direct.promise)
    h.api.renameProfile.mockRejectedValue(new ApiError(409, secret))
    const deleting = h.api.deleteProfile(id, { expected_revision: 7 })
    const blocked = h.store.beginMutation(id)
    expect(blocked).toBeNull()
    if (blocked) await h.api.renameProfile(id, { expected_revision: 7, name: 'Later rename' })
    expect(h.api.renameProfile).not.toHaveBeenCalled()
    expect(h.store.items).toHaveLength(1)
    direct.resolve(undefined)
    await deleting
    expect(h.store.applyRemoval(first)).toBe(true)
    expect(h.store.items).toEqual([])
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.busy).toEqual({})
    expect(h.store.errors).toEqual({})
    expect(h.store.beginMutation(id)).toBeNull()
    expect(h.store.applyMutation(first, profile({ revision: 8 }))).toBe(false)
    await seed(h, [profile({ revision: 8, verification: checked() })])
    expect(h.store.items).toEqual([])
    expect(h.store.lastChecked).toEqual({})
    expect(h.api.renameProfile).not.toHaveBeenCalled()
    expect(h.api.deleteProfile).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7 })
  })

  it('allows different profile IDs and multiple anonymous Creates to mutate concurrently', async () => {
    const h = harness()
    await seed(h, [profile(), profile({ profile_id: otherId, url: 'https://other.example' })])
    const first = ticket(h)
    const second = ticket(h, otherId)
    const createA = h.store.beginMutation()
    const createB = h.store.beginMutation()
    expect(createA).not.toBeNull()
    expect(createB).not.toBeNull()
    expect(createA).not.toBe(createB)
    expect(h.store.busy).toEqual({ [id]: 'mutation', [otherId]: 'mutation' })
    const createdA = profile({ profile_id: newId, revision: 1, url: 'https://new-a.example',
      has_token: false, credential_source: 'none' })
    const createdB = profile({ profile_id: '44444444-4444-4444-8444-444444444444', revision: 1,
      url: 'https://new-b.example', has_token: false, credential_source: 'none' })
    expect(h.store.applyMutation(createB!, createdB)).toBe(true)
    expect(h.store.endMutation(second)).toBe(true)
    expect(h.store.applyMutation(createA!, createdA)).toBe(true)
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    expect(h.store.applyMutation(first, profile({ revision: 8 }))).toBe(true)
    expect(h.store.busy).toEqual({})
    expect(h.store.items).toEqual(expect.arrayContaining([createdA, createdB, profile({ revision: 8 })]))
    expect(h.store.items).toHaveLength(4)
    expect(h.store.errors).toEqual({ [otherId]: { kind: 'request_failed', status: null } })
    expect(h.api.createProfile).not.toHaveBeenCalled()
  })

  it.each(['resolve', 'reject'] as const)('fences Verify when a form mutation begins, including late Verify %s', async outcome => {
    const h = harness()
    await seed(h)
    const check = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(check.promise)
    const checking = h.store.verify(id)
    const t = ticket(h)
    if (outcome === 'resolve') check.resolve(checked())
    else check.reject(new ApiError(503, secret))
    expect(await checking).toBe(false)
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.busy).toEqual({ [id]: 'mutation' })
    expect(h.store.errors).toEqual({})
    expect(h.store.endMutation(t, false)).toBe(true)
  })
})

describe('confirmed deletion and tombstones', () => {
  it.each(['delete', 'null recovery'] as const)('removes metadata, checked history, busy and errors only after confirmed %s', async kind => {
    const h = harness()
    await seed(h, [profile({ verification: checked() }), profile({ profile_id: otherId })])
    const t = ticket(h)
    expect(h.store.items).toHaveLength(2)
    expect(h.store.lastChecked[id]).toBeDefined()
    if (kind === 'delete') {
      h.api.deleteProfile.mockResolvedValueOnce(undefined)
      await h.api.deleteProfile(id, { expected_revision: 7 })
    } else {
      h.api.recoverProfile.mockResolvedValueOnce({ profile: null })
      const result = await h.api.recoverProfile(id, { expected_revision: 7,
        confirmation: { profile_id: id, action: 'discard_uncertain_credential' } })
      expect(result.profile).toBeNull()
    }
    expect(h.store.applyRemoval(t)).toBe(true)
    expect(h.store.items).toEqual([profile({ profile_id: otherId })])
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.busy).toEqual({})
    expect(h.store.errors).toEqual({})
    expect(h.store.applyRemoval(t)).toBe(false)
    expect(await h.store.verify(id)).toBe(false)
    expect(h.store.beginMutation(id)).toBeNull()
  })

  it('does not remove a profile after failed DELETE or an unconfirmed keyless removal', async () => {
    const h = harness()
    await seed(h)
    expect(h.store.endMutation(ticket(h))).toBe(true)
    expect(h.store.items).toEqual([profile()])
    const create = h.store.beginMutation()!
    expect(h.store.applyRemoval(create)).toBe(false)
    expect(h.store.items).toEqual([profile()])
    expect(h.store.endMutation(create, false)).toBe(true)
  })

  it('cannot resurrect a deleted ID through an older load, a later stale snapshot, or a keyless Create callback', async () => {
    const h = harness()
    await seed(h)
    const list = deferred<RemoteServers>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    const pending = h.store.load()
    const staleCreate = h.store.beginMutation()!
    expect(h.store.applyRemoval(ticket(h))).toBe(true)
    list.resolve({ items: [profile({ revision: 99, verification: checked() })] })
    expect(await pending).toBe(false)
    expect(h.store.applyMutation(staleCreate, profile({ revision: 100 }))).toBe(false)
    await seed(h, [profile({ revision: 101, verification: checked() })])
    expect(h.store.items).toEqual([])
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.applyMutation(h.store.beginMutation()!, profile({ revision: 102 }))).toBe(false)
  })

  it.each(['resolve', 'reject'] as const)('cannot resurrect or report per-ID errors from a deleted profile\'s late Verify %s', async outcome => {
    const h = harness()
    await seed(h)
    const check = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(check.promise)
    const pending = h.store.verify(id)
    expect(h.store.applyRemoval(ticket(h))).toBe(true)
    const before = snapshot(h)
    if (outcome === 'resolve') check.resolve(checked())
    else check.reject(new ApiError(503, secret))
    expect(await pending).toBe(false)
    expect(snapshot(h)).toEqual(before)
  })

  it('invalidates Verify and clears lastChecked when an authoritative list no longer contains the profile', async () => {
    const h = harness()
    await seed(h, [profile({ verification: checked() })])
    const check = deferred<VerificationDTO>()
    h.api.verify.mockReturnValueOnce(check.promise)
    const pending = h.store.verify(id)
    await seed(h, [])
    check.resolve(checked())
    expect(await pending).toBe(false)
    expect(h.store.items).toEqual([])
    expect(h.store.lastChecked).toEqual({})
    expect(h.store.busy).toEqual({})
  })
})

describe('renderer lifetime and isolation', () => {
  it.each(['resolve', 'reject'] as const)('$dispose clears metadata and fences pending load/Verify/mutation %s', async outcome => {
    const h = harness()
    await seed(h, [profile({ verification: checked() }), profile({ profile_id: otherId })])
    const list = deferred<RemoteServers>()
    const check = deferred<VerificationDTO>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    h.api.verify.mockReturnValueOnce(check.promise)
    const loading = h.store.load()
    const checking = h.store.verify(otherId)
    const t = ticket(h)
    const create = h.store.beginMutation()!
    h.store.$dispose()
    expect(h.store.$state).toEqual({ items: [], loaded: false, loading: false, error: null,
      lastChecked: {}, busy: {}, errors: {} })
    const before = snapshot(h)
    if (outcome === 'resolve') { list.resolve({ items: [profile()] }); check.resolve(checked()) }
    else { list.reject(new ApiError(503, secret)); check.reject(new Error(secret)) }
    expect(await loading).toBe(false)
    expect(await checking).toBe(false)
    expect(h.store.applyMutation(t, profile({ revision: 8 }))).toBe(false)
    expect(h.store.applyMutation(create, profile({ profile_id: newId }))).toBe(false)
    expect(h.store.applyRemoval(t)).toBe(false)
    expect(h.store.endMutation(t)).toBe(false)
    expect(await h.store.load()).toBe(false)
    expect(await h.store.verify(id)).toBe(false)
    expect(h.store.beginMutation()).toBeNull()
    expect(snapshot(h)).toEqual(before)
    expect(h.api.listServers).toHaveBeenCalledTimes(2)
    expect(h.api.verify).toHaveBeenCalledTimes(1)
  })

  it.each(['resolve', 'reject'] as const)('session invalidation fences pending/future operations without rebinding or writes (%s)', async outcome => {
    const h = harness()
    await seed(h, [profile(), profile({ profile_id: otherId })])
    const bind = vi.spyOn(h.session, 'bind')
    const invalidate = vi.spyOn(h.session, 'invalidate')
    const list = deferred<RemoteServers>()
    const check = deferred<VerificationDTO>()
    h.api.listServers.mockReturnValueOnce(list.promise)
    h.api.verify.mockReturnValueOnce(check.promise)
    const loading = h.store.load()
    const checking = h.store.verify(otherId)
    const t = ticket(h)
    const create = h.store.beginMutation()!
    h.session.invalidate()
    const before = snapshot(h)
    if (outcome === 'resolve') { list.resolve({ items: [] }); check.resolve(checked()) }
    else { list.reject(new Error(secret)); check.reject(new ApiError(503, secret)) }
    expect(await loading).toBe(false)
    expect(await checking).toBe(false)
    expect(h.store.applyMutation(t, profile({ revision: 8 }))).toBe(false)
    expect(h.store.applyRemoval(t)).toBe(false)
    expect(h.store.endMutation(t)).toBe(false)
    expect(h.store.applyMutation(create, profile({ profile_id: newId }))).toBe(false)
    expect(await h.store.load()).toBe(false)
    expect(await h.store.verify(id)).toBe(false)
    expect(h.store.beginMutation()).toBeNull()
    expect(snapshot(h)).toEqual(before)
    expect(bind).not.toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalledTimes(1)
    expect(h.api.listServers).toHaveBeenCalledTimes(2)
    expect(h.api.verify).toHaveBeenCalledTimes(1)
  })

  it('isolates A/B Pinia roots, identical profile IDs, deferred replies, tickets, errors and disposal', async () => {
    const a = harness('A')
    const b = harness('B')
    expect(a.store).not.toBe(b.store)
    expect(createRemoteApiClient).toHaveBeenNthCalledWith(1, a.session)
    expect(createRemoteApiClient).toHaveBeenNthCalledWith(2, b.session)
    await seed(a, [profile({ name: 'A' })])
    await seed(b, [profile({ name: 'B', revision: 20, url: 'https://b.example' })])
    const check = deferred<VerificationDTO>()
    a.api.verify.mockReturnValueOnce(check.promise)
    const checkingA = a.store.verify(id)
    b.api.verify.mockRejectedValueOnce(new ApiError(401, secret))
    expect(await b.store.verify(id)).toBe(false)
    const bBefore = snapshot(b)
    check.resolve(checked())
    expect(await checkingA).toBe(true)
    expect(snapshot(b)).toEqual(bBefore)
    const t = ticket(a)
    expect(b.store.applyMutation(t, profile({ revision: 21 }))).toBe(false)
    expect(b.store.applyRemoval(t)).toBe(false)
    expect(a.store.applyRemoval(t)).toBe(true)
    expect(b.store.items[0]!.name).toBe('B')
    a.session.invalidate()
    a.store.$dispose()
    expect(snapshot(b)).toEqual(bBefore)
    b.api.verify.mockResolvedValueOnce(checked({ user: null }))
    expect(await b.store.verify(id)).toBe(true)
    expect(b.api.verify).toHaveBeenLastCalledWith(id, 20)
    expectChecked(b, profile({ revision: 20, url: 'https://b.example' }), checked({ user: null }))
  })

  it('has no module-level metadata or tombstone cache in a fresh renderer', async () => {
    const a = harness('A')
    await seed(a)
    expect(a.store.applyRemoval(ticket(a))).toBe(true)
    a.store.$dispose()
    const fresh = harness('A')
    expect(fresh.store.items).toEqual([])
    expect(fresh.store.loaded).toBe(false)
    await seed(fresh)
    expect(fresh.store.items).toEqual([profile()])
  })

  it('keeps A/B definitions unique even if accidentally instantiated in the same Pinia root', async () => {
    const pinia = createPinia()
    piniae.push(pinia)
    const sessionA = new ProjectSession('A')
    const sessionB = new ProjectSession('B')
    sessionA.bind({ project_id: 'A', epoch: 'epoch-A' })
    sessionB.bind({ project_id: 'B', epoch: 'epoch-B' })
    const apiA = makeClient()
    const apiB = makeClient()
    vi.mocked(createRemoteApiClient).mockImplementation(session => {
      if (session === sessionA) return apiA
      if (session === sessionB) return apiB
      throw new Error('Unexpected renderer session')
    })
    const useA = createRemoteServersStore(sessionA)
    const useB = createRemoteServersStore(sessionB)
    const a = { session: sessionA, api: apiA, pinia, useStore: useA, store: useA(pinia) }
    const b = { session: sessionB, api: apiB, pinia, useStore: useB, store: useB(pinia) }
    expect(a.store.$id).not.toBe(b.store.$id)
    expect(a.store).not.toBe(b.store)
    expect(createRemoteApiClient).toHaveBeenCalledTimes(2)
    await seed(a, [profile({ name: 'A' })])
    expect(b.store.items).toEqual([])
    expect(b.store.loaded).toBe(false)
    await seed(b, [profile({ name: 'B', revision: 20 })])
    const before = snapshot(b)
    expect(a.store.applyRemoval(ticket(a))).toBe(true)
    a.session.invalidate()
    a.store.$dispose()
    expect(snapshot(b)).toEqual(before)
    b.api.verify.mockResolvedValueOnce(checked())
    expect(await b.store.verify(id)).toBe(true)
    expect(b.api.verify).toHaveBeenCalledExactlyOnceWith(id, 20)
    expectChecked(b, profile({ revision: 20 }), checked())
  })
})

describe('credential and persistence boundary', () => {
  it('keeps credentials in the direct form/API call, never in Pinia state, action arguments, persistence or logs', async () => {
    const h = harness()
    const actions: { name: string; args: unknown[] }[] = []
    h.store.$onAction(({ name, args }) => { actions.push({ name, args }) }, true)
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    await seed(h)
    const direct = deferred<RemoteServerProfile>()
    h.api.setCredential.mockReturnValueOnce(direct.promise)
    const t = ticket(h) // Captured BEFORE form activity; no token ever enters an action.
    const saving = h.api.setCredential(id, { expected_revision: 7, token: secret })
    expect(h.store.items).toEqual([profile()])
    const dto = profile({ revision: 8, credential_source: 'environment' })
    direct.resolve(dto)
    expect(h.store.applyMutation(t, await saving)).toBe(true)
    h.api.verify.mockRejectedValueOnce(new ApiError(502, secret))
    expect(await h.store.verify(id)).toBe(false)
    h.api.listServers.mockRejectedValueOnce(new Error(secret))
    expect(await h.store.load()).toBe(false)
    expect(h.store.endMutation(ticket(h))).toBe(true) // Boolean/default failure, not the raw form exception.
    expect(actions.map(action => action.name)).toEqual([
      'load', 'beginMutation', 'applyMutation', 'verify', 'load', 'beginMutation', 'endMutation',
    ])
    expect(actions[1]!.args).toEqual([id])
    expect(actions[2]!.args).toEqual([t, dto])
    expect(actions[6]!.args).toHaveLength(1)
    expect(h.api.setCredential).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7, token: secret })
    expect(JSON.stringify(actions)).not.toContain(secret)
    expect(JSON.stringify(h.pinia.state.value)).not.toContain(secret)
    expect(Object.keys(h.store.$state).sort()).toEqual([
      'busy', 'error', 'errors', 'items', 'lastChecked', 'loaded', 'loading',
    ])
    for (const storage of [localStorage, sessionStorage]) {
      expect(storage.getItem).not.toHaveBeenCalled()
      expect(storage.setItem).not.toHaveBeenCalled()
      expect(storage.removeItem).not.toHaveBeenCalled()
    }
    expect(indexedDB.open).not.toHaveBeenCalled()
    expect(indexedDB.deleteDatabase).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(WebSocket).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })
})
