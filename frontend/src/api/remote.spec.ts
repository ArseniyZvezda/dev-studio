import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { createRemoteApiClient, RemoteApiError, type VerificationDTO, type RemoteServerProfile,
  type CreateRemoteProfileInput, type RenameRemoteProfileInput, type ChangeRemoteProfileUrlInput,
  type RemoteCredentialInput, type RemoteRevisionInput, type RecoverRemoteProfileInput } from './remote'
import { ProjectSession, StaleProjectResponseError } from '@/session/project-session'

vi.mock('./auth', () => ({
  authHeaders: () => ({ Authorization: 'Bearer studio-test-token' }),
  withTokenParam: (url: string) => url,
}))

const profileId = 'c503da2b-f8fe-4fb0-b945-86bc119495b3'
const cursor = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
const verification: VerificationDTO = {
  state: 'valid', checked_at: '2026-11-16T12:00:00Z',
  user: { id: 'user-1', display_name: 'Alice', color: '#123456' },
  wire_protocol: 1, catalog_version: 2, error_code: null,
}
const server = {
  profile_id: profileId, name: '  Historical name  ', url: 'https://remote.example', revision: 7,
  has_token: true, credential_source: 'environment', verification,
}
const projects = {
  items: [{ id: profileId, display_name: 'Game', storage_epoch: 'epoch-remote',
    metadata_revision: 3, created_at: '2026-11-16T10:00:00Z' }],
  next_cursor: cursor,
}
function session(id = 'A') {
  const scope = new ProjectSession(id)
  scope.bind({ project_id: id, epoch: `epoch-${id}` })
  return scope
}
function reply(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function mockReply(value: unknown, status = 200) {
  const fetcher = vi.fn().mockResolvedValue(reply(value, status))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

type RemoteClient = ReturnType<typeof createRemoteApiClient>
const revisionInput = { expected_revision: 7 }
const credentialCanary = 'remote-credential-canary-DO-NOT-ECHO'
const confirmation = { profile_id: profileId, action: 'discard_uncertain_credential' as const }
const mutations = [
  { name: 'createProfile', method: 'POST', suffix: '', status: 201,
    input: { name: '  New label  ', url: 'remote.example/' },
    call: (c: RemoteClient, input: unknown) => c.createProfile(input as CreateRemoteProfileInput) },
  { name: 'renameProfile', method: 'PATCH', suffix: `/${profileId}`, status: 200,
    input: { ...revisionInput, name: 'New label' },
    call: (c: RemoteClient, input: unknown, id = profileId) => c.renameProfile(id, input as RenameRemoteProfileInput) },
  { name: 'changeProfileUrl', method: 'PATCH', suffix: `/${profileId}`, status: 200,
    input: { ...revisionInput, url: 'https://new.example', clear_credential: true },
    call: (c: RemoteClient, input: unknown, id = profileId) => c.changeProfileUrl(id, input as ChangeRemoteProfileUrlInput) },
  { name: 'setCredential', method: 'PUT', suffix: `/${profileId}/credential`, status: 200,
    input: { ...revisionInput, token: credentialCanary },
    call: (c: RemoteClient, input: unknown, id = profileId) => c.setCredential(id, input as RemoteCredentialInput) },
  { name: 'clearCredential', method: 'DELETE', suffix: `/${profileId}/credential`, status: 200,
    input: revisionInput,
    call: (c: RemoteClient, input: unknown, id = profileId) => c.clearCredential(id, input as RemoteRevisionInput) },
  { name: 'deleteProfile', method: 'DELETE', suffix: `/${profileId}`, status: 204,
    input: revisionInput,
    call: (c: RemoteClient, input: unknown, id = profileId) => c.deleteProfile(id, input as RemoteRevisionInput) },
  { name: 'recoverProfile', method: 'POST', suffix: `/${profileId}/recover`, status: 200,
    input: { ...revisionInput, confirmation },
    call: (c: RemoteClient, input: unknown, id = profileId) => c.recoverProfile(id, input as RecoverRemoteProfileInput) },
] as const
const jsonMutations = mutations.filter(m => m.name !== 'deleteProfile')
const profileMutations = mutations.filter(m => !['deleteProfile', 'recoverProfile'].includes(m.name))
// Full local _ERROR_STATUS vocabulary, including codes allowed inside VerificationDTO.
// This does not expose C8/Create-project/observe methods or their routes.
const backendErrorStatuses: [string, number][] = [
  ...['invalid_request', 'invalid_id', 'invalid_display_name', 'invalid_operation_id', 'invalid_intent']
    .map((code): [string, number] => [code, 422]),
  ...['profile_not_found', 'operation_not_found'].map((code): [string, number] => [code, 404]),
  ...['profile_revision_conflict', 'credential_binding_conflict', 'operation_conflict', 'operation_in_progress',
    'credential_binding_unproven', 'profile_url_collision', 'profile_in_use', 'profile_consumers_not_drained',
    'credential_clear_confirmation_required', 'profile_recovery_confirmation_conflict', 'profile_recovery_required',
    'credential_required', 'invalid_credential'].map((code): [string, number] => [code, 409]),
  ...['profile_store_unavailable', 'application_store_unavailable', 'credential_store_unavailable', 'metadata_unavailable',
    'consumer_lifecycle_unavailable', 'profile_recovery_unavailable', 'profile_committed_metadata_unavailable',
    'intent_store_unavailable', 'intent_store_invalid', 'invalid_intent_path', 'invalid_intent_mode', 'invalid_catalog_readers']
    .map((code): [string, number] => [code, 503]),
  ...['invalid_profile_metadata', 'invalid_response', 'invalid_server_url', 'remote_unauthorised', 'insecure_blocked',
    'transport_unavailable', 'catalog_unavailable', 'version_mismatch', 'capabilities_missing', 'redirect_refused',
    'remote_error', 'temporarily_unavailable', 'verification_failed', 'invalid_clock', 'service_unavailable',
    'identity_mismatch', 'project_not_found', 'metadata_conflict', 'snapshot_changed', 'payload_too_large']
    .map((code): [string, number] => [code, 502]),
]
function mutationPayload(name: string, profile: unknown = server) {
  return name === 'recoverProfile' ? { profile } : profile
}
function mutationResponse(m: typeof mutations[number]) {
  return m.status === 204 ? new Response(null, { status: 204 }) : reply(mutationPayload(m.name), m.status)
}
async function expectInvalidResponse(pending: Promise<unknown>) {
  const error = await pending.catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(RemoteApiError)
  expect(error).toMatchObject({ status: 502, code: 'invalid_response', details: {}, isAuthFailure: false })
}

// Mirror api/remote.py's strict DTOs: required nullable fields are not optional,
// nested users forbid extras, and checked error codes use the local vocabulary.
const invalidVerifications: [string, unknown][] = [
  ['missing verification', undefined], ['null verification', null],
  ['string verification', 'unchecked'], ['array verification', []],
  ['number verification', 1], ['boolean verification', false],
  ['missing state', {}], ['null state', { state: null }],
  ['numeric state', { state: 1 }], ['unknown state', { state: 'future-state' }],
  ...['unchecked', 'checking', 'unavailable'].map((state): [string, unknown] =>
    [`${state} with extra checked_at`, { state, checked_at: verification.checked_at }]),
  ...['unchecked', 'checking', 'unavailable'].flatMap(state =>
    ['checked_at', 'user', 'capabilities'].map((field): [string, unknown] =>
      [`${state} with null extra ${field}`, { state, [field]: null }])),
  ['checked extra field', { ...verification, token: 'forbidden' }],
  ['checked null timestamp', { ...verification, checked_at: null }],
  ['checked numeric timestamp', { ...verification, checked_at: 1 }],
  ['checked string user', { ...verification, user: 'Alice' }],
  ['checked array user', { ...verification, user: [] }],
  ['nested user extra field', { ...verification, user: { ...verification.user, token: 'forbidden' } }],
  ...['id', 'display_name', 'color'].flatMap((field): [string, unknown][] => {
    const user = { ...verification.user } as Record<string, unknown>
    delete user[field]
    return [
      [`missing user ${field}`, { ...verification, user }],
      [`invalid user ${field}`, { ...verification, user: { ...verification.user, [field]: 1 } }],
    ]
  }),
  ...['wire_protocol', 'catalog_version'].flatMap((field): [string, unknown][] =>
    ['1', true, 1.5].map(value => [`invalid ${field} ${JSON.stringify(value)}`, { ...verification, [field]: value }])),
  ['numeric error code', { ...verification, error_code: 1 }],
  ['unknown error code', { ...verification, error_code: 'future-error' }],
  ...Object.keys(verification).map((field): [string, unknown] => {
    const value: Record<string, unknown> = { ...verification }
    delete value[field]
    return [`missing checked ${field}`, value]
  }),
]

// All I/O is opt-in synthetic fetch; even unexpected calls never reach the network.
beforeEach(() => vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected fetch') })))
afterEach(() => vi.unstubAllGlobals())

describe('typed Remote API boundary (studio-design §5.1/§6)', () => {
  it('lists the exact server envelope without Verify, catalog calls or metadata rewriting', async () => {
    const payload = { items: [server] }
    const fetcher = mockReply(payload)
    await expect(createRemoteApiClient().listServers()).resolves.toEqual(payload)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]![0]).toBe('/api/remote/servers')
  })

  it.each(['stored', 'environment', 'none'] as const)('preserves %s credential metadata', async source => {
    const payload = { items: [{ ...server, has_token: source !== 'none', credential_source: source }] }
    mockReply(payload)
    await expect(createRemoteApiClient().listServers()).resolves.toEqual(payload)
  })

  it.each([null, [], {}, { items: null }, { items: {} }, { items: [], extra: null },
    { items: [null] }, { items: ['profile'] }])('rejects malformed list envelope/row %j', async value => {
    mockReply(value)
    await expectInvalidResponse(createRemoteApiClient().listServers())
  })

  it('preserves a successfully read empty server list', async () => {
    mockReply({ items: [] })
    await expect(createRemoteApiClient().listServers()).resolves.toEqual({ items: [] })
  })

  it.each(['unchecked', 'checking', 'unavailable'])('retains explicit %s without inventing check fields', async state => {
    mockReply({ items: [{ ...server, verification: { state } }] })
    const result = await createRemoteApiClient().listServers()
    expect(result.items[0]!.verification).toEqual({ state })
  })

  it.each(invalidVerifications)(
    'rejects list %s without fabricating unavailable', async (_label, status) => {
      const { verification: _ignored, ...metadata } = server
      mockReply({ items: [{ ...metadata, ...(status === undefined ? {} : { verification: status }) }] })
      await expectInvalidResponse(createRemoteApiClient().listServers())
    },
  )

  it.each(['valid', 'unauthorised', 'offline', 'incompatible', 'insecure_blocked', 'error'] as const)(
    'preserves complete %s verification DTOs from both list and explicit Verify', async state => {
      const value = { ...verification, state, error_code: state === 'valid' ? null : 'verification_failed' }
      const fetcher = vi.fn().mockResolvedValueOnce(reply({ items: [{ ...server, verification: value }] }))
        .mockResolvedValueOnce(reply(value))
      vi.stubGlobal('fetch', fetcher)
      const client = createRemoteApiClient()
      expect((await client.listServers()).items[0]!.verification).toEqual(value)
      await expect(client.verify(profileId, 7)).resolves.toEqual(value)
    },
  )

  it('sends only expected_revision in Verify, using Studio auth and JSON headers', async () => {
    const fetcher = mockReply(verification)
    await createRemoteApiClient().verify(profileId, 7)
    const [url, init] = fetcher.mock.calls[0]!
    expect(url).toBe(`/api/remote/servers/${profileId}/verify`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ expected_revision: 7 })
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer studio-test-token')
  })

  it.each([
    ...invalidVerifications,
    ...['unchecked', 'checking', 'unavailable'].map((state): [string, unknown] => [state, { state }]),
  ])(
    'rejects Verify %s rather than inventing a checked result', async (_label, value) => {
      mockReply(value)
      await expectInvalidResponse(createRemoteApiClient().verify(profileId, 1))
    },
  )

  it.each([
    { ...verification, user: null, wire_protocol: null, catalog_version: null, error_code: null },
    // These are plain strings/integers in the DTO, not timestamps, colours or positive versions.
    { ...verification, checked_at: '', user: { id: '', display_name: '', color: '' },
      wire_protocol: -1, catalog_version: 0, error_code: 'invalid_response' },
  ])('preserves backend-permitted checked values %j', async value => {
    const fetcher = vi.fn().mockResolvedValueOnce(reply({ items: [{ ...server, verification: value }] }))
      .mockResolvedValueOnce(reply(value))
    vi.stubGlobal('fetch', fetcher)
    const client = createRemoteApiClient()
    expect((await client.listServers()).items[0]!.verification).toEqual(value)
    await expect(client.verify(profileId, 7)).resolves.toEqual(value)
  })

  it.each([0, 1, 2])('rejects the entire mixed list with malformed verification at index %s', async index => {
    const items: unknown[] = [
      { ...server, verification: { state: 'unavailable' } },
      { ...server, verification: { state: 'checking' } },
      server,
    ]
    items.splice(index, 0, { ...server, verification: { ...verification, user: [] } })
    const fetcher = mockReply({ items })
    await expectInvalidResponse(createRemoteApiClient().listServers())
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it.each([200, 401])('reports malformed HTTP %s bodies as typed errors, retaining auth semantics', async status => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not JSON', { status })))
    await expect(createRemoteApiClient().listServers()).rejects.toMatchObject({
      status: status === 200 ? 502 : status, code: 'invalid_response', details: {}, isAuthFailure: status === 401,
    })
  })

  it('reuses ApiError for current-session network failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('connection failed')))
    const error = await createRemoteApiClient().listServers().catch((cause: unknown) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 0, detail: 'connection failed', isAuthFailure: false })
  })

  it('returns one catalog page unchanged, forwards only cursor and never auto-follows or verifies', async () => {
    const fetcher = mockReply(projects)
    const client = createRemoteApiClient()
    await expect(client.listProjects(profileId, cursor)).resolves.toEqual(projects)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]![0]).toBe(`/api/remote/servers/${profileId}/projects?cursor=${cursor}`)
    const init = fetcher.mock.calls[0]![1]
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer studio-test-token')
    expect(new Headers(init.headers).has('Content-Type')).toBe(false)
  })

  it.each([undefined, null])('omits absent cursor %s and preserves terminal/empty pages', async value => {
    const payload = { items: [], next_cursor: null }
    const fetcher = mockReply(payload)
    await expect(createRemoteApiClient().listProjects(profileId, value)).resolves.toEqual(payload)
    expect(fetcher.mock.calls[0]![0]).toBe(`/api/remote/servers/${profileId}/projects`)
  })

  it('encodes cursor as a single opaque query value rather than injecting query keys', async () => {
    const fetcher = mockReply(projects)
    await createRemoteApiClient().listProjects(profileId, 'a b/+?&epoch=foreign#x')
    const url = new URL(fetcher.mock.calls[0]![0], 'http://studio.test')
    expect([...url.searchParams]).toEqual([['cursor', 'a b/+?&epoch=foreign#x']])
  })

  it('keeps all three methods global for A, B, unbound and legacy sessions', async () => {
    const fetcher = vi.fn().mockImplementation(async (url: string) => reply(
      url.endsWith('/verify') ? verification : url.includes('/projects') ? projects : { items: [server] },
    ))
    vi.stubGlobal('fetch', fetcher)
    for (const scope of [session('A'), session('B'), new ProjectSession('unbound'), new ProjectSession()]) {
      const client = createRemoteApiClient(scope)
      await client.listServers()
      await client.verify(profileId, 1)
      await client.listProjects(profileId)
    }
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(Array.from({ length: 4 }, () => [
      '/api/remote/servers', `/api/remote/servers/${profileId}/verify`, `/api/remote/servers/${profileId}/projects`,
    ]).flat())
  })

  it.each(['', 'not-a-uuid', profileId.toUpperCase(), profileId.replaceAll('-', ''), `{${profileId}}`,
    `urn:uuid:${profileId}`, `${profileId}\n`, `${profileId}\r`, `${profileId}/extra`,
    `%63${profileId.slice(1)}`, '../x', `${profileId}?x=1`, `${profileId}#x`, null, 7])(
    'rejects noncanonical profile ID %j before fetch for both parameterized methods', async id => {
      const client = createRemoteApiClient(session())
      await expect(client.verify(id as string, 1)).rejects.toBeInstanceOf(RemoteApiError)
      await expect(client.listProjects(id as string)).rejects.toBeInstanceOf(RemoteApiError)
      expect(fetch).not.toHaveBeenCalled()
    },
  )

  it.each(['00000000-0000-0000-0000-000000000000', cursor])('accepts canonical UUID %s without version/variant restrictions', async id => {
    const fetcher = mockReply(verification)
    await createRemoteApiClient().verify(id, Number.MAX_SAFE_INTEGER)
    expect(fetcher.mock.calls[0]![0]).toBe(`/api/remote/servers/${id}/verify`)
  })

  it.each([0, -1, 1.5, NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER + 1, '1', null, undefined, true])(
    'rejects invalid expected revision %j before fetch', async revision => {
      await expect(createRemoteApiClient().verify(profileId, revision as number)).rejects.toBeInstanceOf(RemoteApiError)
      expect(fetch).not.toHaveBeenCalled()
    },
  )

  it.each([[401, 'studio_unauthorised'], [403, 'studio_forbidden'], [404, 'profile_not_found'],
    [409, 'profile_revision_conflict'], [422, 'invalid_request'], [502, 'remote_unauthorised'],
    [503, 'metadata_unavailable']] as const)('preserves HTTP %s error code/details and ApiError auth semantics', async (status, code) => {
      const details = { field: 'revision', reason: 'changed' }
      mockReply({ code, details }, status)
      const error = await createRemoteApiClient().listServers().catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(RemoteApiError)
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({ status, code, details, isAuthFailure: status === 401 })
    },
  )
})

describe('explicit Remote profile mutations (C6/C7)', () => {
  it.each(mutations)('$name sends one exact global request with Studio auth, CAS and no-store', async m => {
    const response = mutationResponse(m)
    const json = vi.spyOn(response, 'json')
    const fetcher = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetcher)
    const result = await m.call(createRemoteApiClient(session()), m.input)
    expect(result).toEqual(m.status === 204 ? undefined : mutationPayload(m.name))
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/remote/servers${m.suffix}`, {
      method: m.method, cache: 'no-store',
      headers: { Authorization: 'Bearer studio-test-token', 'Content-Type': 'application/json' },
      body: JSON.stringify(m.input),
    })
    expect(json).toHaveBeenCalledTimes(m.status === 204 ? 0 : 1)
    expect(JSON.stringify(result) ?? '').not.toContain(credentialCanary)
  })

  it.each(mutations)('$name uses identical global URLs for A/B/unbound/legacy without rebinding', async m => {
    const fetcher = vi.fn().mockImplementation(async () => mutationResponse(m))
    vi.stubGlobal('fetch', fetcher)
    for (const scope of [session('A'), session('B'), new ProjectSession('unbound'), new ProjectSession()]) {
      const binding = scope.scope, generation = scope.generation
      await m.call(createRemoteApiClient(scope), m.input)
      expect(scope.scope).toBe(binding)
      expect(scope.generation).toBe(generation)
    }
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(Array(4).fill(`/api/remote/servers${m.suffix}`))
  })

  for (const m of mutations) {
    it.each([[401, 'studio_unauthorised'], [403, 'studio_forbidden'], [404, 'profile_not_found'],
      [409, 'profile_revision_conflict'], [409, 'profile_url_collision'], [409, 'profile_in_use'],
      [409, 'profile_consumers_not_drained'], [409, 'profile_recovery_confirmation_conflict'],
      [405, 'method_not_allowed'], [502, 'invalid_response'],
      [409, 'profile_recovery_required'], [422, 'invalid_request'], [503, 'profile_recovery_unavailable'],
      [503, 'consumer_lifecycle_unavailable'], [503, 'profile_committed_metadata_unavailable'],
      [503, 'credential_store_unavailable']] as const)(`${m.name} preserves %s %s without retry`, async (status, code) => {
      const details = { reason: 'changed' }
      const fetcher = mockReply({ code, details }, status)
      const error = await m.call(createRemoteApiClient(), m.input).catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toBeInstanceOf(RemoteApiError)
      expect(error).toMatchObject({ status, code, details, isAuthFailure: status === 401 })
      expect(fetcher).toHaveBeenCalledTimes(1)
    })

    it.each([200, 201, 202, 204, 205, 206].filter(status => status !== m.status))(
      `${m.name} rejects unexpected success status %s`, async status => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
          [204, 205].includes(status) ? new Response(null, { status }) : reply(mutationPayload(m.name), status),
        ))
        await expectInvalidResponse(m.call(createRemoteApiClient(), m.input))
        expect(fetch).toHaveBeenCalledTimes(1)
      },
    )

    it.each([null, [], {}, { code: 'invalid_request' }, { code: 1, details: {} },
      { code: 'invalid_request', details: [] }, { code: 'invalid_request', details: { field: 1 } },
      { code: 'invalid_request', details: {}, token: credentialCanary },
      { code: credentialCanary, details: {} }])(`${m.name} rejects malformed error %j`, async payload => {
      mockReply(payload, 401)
      const error = await m.call(createRemoteApiClient(), m.input).catch((cause: unknown) => cause)
      expect(error).toMatchObject({ status: 401, code: 'invalid_response', details: {}, isAuthFailure: true })
      expect(JSON.stringify(error)).not.toContain(credentialCanary)
    })

    it(`${m.name} maps non-JSON errors and network errors without echoing input`, async () => {
      const fetcher = vi.fn().mockResolvedValueOnce(new Response(credentialCanary, { status: 503 }))
        .mockRejectedValueOnce(new Error(credentialCanary))
      vi.stubGlobal('fetch', fetcher)
      await expect(m.call(createRemoteApiClient(), m.input)).rejects.toMatchObject({ status: 503, code: 'invalid_response' })
      const error = await m.call(createRemoteApiClient(), m.input).catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({ status: 0, isAuthFailure: false })
      expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
      expect(fetcher).toHaveBeenCalledTimes(2)
    })

    it.each([null, [], {}, { ...m.input, extra: true },
      m.name === 'createProfile' ? { ...m.input, token: credentialCanary } : { ...m.input, name: 'x', url: 'https://x.example' }])(
      `${m.name} rejects invalid input/extra/composite %j before fetch`, async input => {
        await expect(m.call(createRemoteApiClient(), input)).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
        expect(fetch).not.toHaveBeenCalled()
      },
    )

    it(`${m.name} rejects an already disposed session`, async () => {
      const scope = session()
      scope.invalidate()
      await expect(m.call(createRemoteApiClient(scope), m.input)).rejects.toBeInstanceOf(StaleProjectResponseError)
      expect(fetch).not.toHaveBeenCalled()
    })

    it.each(['success', 'http-error', 'network-error'])(`${m.name} discards stale fetch %s including 204`, async outcome => {
      const scope = session(), waiting = deferred<Response>()
      vi.stubGlobal('fetch', vi.fn(() => waiting.promise))
      const pending = m.call(createRemoteApiClient(scope), m.input)
      scope.invalidate()
      if (outcome === 'network-error') waiting.reject(new Error(credentialCanary))
      else waiting.resolve(outcome === 'success' ? mutationResponse(m) : reply({ code: 'studio_unauthorised', details: {} }, 401))
      await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    })

    // Even DELETE's error body must be fenced; its successful 204 never reads a body.
    it.each(['http-error', ...(m.status === 204 ? [] : ['success', 'body-error'])])(
      `${m.name} discards stale body %s`, async outcome => {
        const scope = session(), waiting = deferred<unknown>(), reading = deferred<void>()
        const response = outcome === 'http-error' ? reply({}, 409) : mutationResponse(m)
        response.json = vi.fn(() => { reading.resolve(); return waiting.promise })
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
        const pending = m.call(createRemoteApiClient(scope), m.input)
        await reading.promise
        scope.invalidate()
        if (outcome === 'body-error') waiting.reject(new Error(credentialCanary))
        else waiting.resolve(outcome === 'http-error' ? { code: 'profile_revision_conflict', details: {} } : mutationPayload(m.name))
        await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
      },
    )
  }

  for (const m of mutations.filter(m => m.name !== 'createProfile')) {
    it.each(['', 'invalid', profileId.toUpperCase(), profileId.replaceAll('-', ''), `{${profileId}}`,
      `urn:uuid:${profileId}`, `${profileId}\n`, `${profileId}/extra`, `%63${profileId.slice(1)}`,
      `${profileId}?x=1`, `${profileId}#x`, null, 1])(`${m.name} rejects noncanonical ID %j`, async id => {
      await expect(m.call(createRemoteApiClient(), m.input, id as string)).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
      expect(fetch).not.toHaveBeenCalled()
    })
    it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '1', null, undefined, true])(
      `${m.name} rejects invalid CAS %j`, async expected_revision => {
        await expect(m.call(createRemoteApiClient(), { ...m.input, expected_revision })).rejects.toMatchObject({ status: 422 })
        expect(fetch).not.toHaveBeenCalled()
      },
    )
  }

  it.each(jsonMutations)('$name rejects non-JSON success', async m => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not JSON', { status: m.status })))
    await expectInvalidResponse(m.call(createRemoteApiClient(), m.input))
  })

  const malformedProfiles: [string, unknown][] = [
    ['null', null], ['array', []], ['empty', {}], ['wrapped', { profile: server }],
    ['credential echo', { ...server, token: credentialCanary }],
    ...Object.keys(server).map((field): [string, unknown] => {
      const value: Record<string, unknown> = { ...server }
      delete value[field]
      return [`missing ${field}`, value]
    }),
    ...['profile_id', 'name', 'url'].map((field): [string, unknown] => [`bad ${field}`, { ...server, [field]: 1 }]),
    ['noncanonical id', { ...server, profile_id: profileId.toUpperCase() }],
    ...['', 'remote.example', 'wss://remote.example', 'file:///tmp/key', 'https://',
      'https:///remote.example', 'https:////remote.example',
      `https://user:${credentialCanary}@example.invalid`, `https://${credentialCanary}@example.invalid`,
      `https://example.invalid?token=${credentialCanary}`, `https://example.invalid#${credentialCanary}`,
      'https://bad host.example', 'https://host:invalid', 'https://[invalid]',
    ].map((url): [string, unknown] => ['invalid URL', { ...server, url }]),
    ...[0, -1, 1.5, '7', true, null].map((revision): [string, unknown] => [`revision ${revision}`, { ...server, revision }]),
    ['invalid has_token', { ...server, has_token: 'true' }],
    ['unknown source', { ...server, credential_source: 'future' }],
    ['inconsistent source', { ...server, credential_source: 'none' }],
    ...invalidVerifications.map(([label, verification]): [string, unknown] => [label, { ...server, verification }]),
  ]
  for (const m of jsonMutations) {
    it.each(malformedProfiles.filter(([, profile]) => m.name !== 'recoverProfile' || profile !== null))(
      `${m.name} rejects malformed profile %s without credential echo`, async (_label, profile) => {
      mockReply(mutationPayload(m.name, profile), m.status)
      const error = await m.call(createRemoteApiClient(), m.input).catch((cause: unknown) => cause)
      expect(error).toMatchObject({ status: 502, code: 'invalid_response', details: {} })
      expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
    })
  }

  it.each(['stored', 'environment', 'none'] as const)('preserves exact %s metadata, historical label and unavailable', async source => {
    const profile: RemoteServerProfile = { ...server, credential_source: source, has_token: source !== 'none', verification: { state: 'unavailable' } }
    for (const m of profileMutations) {
      mockReply(profile, m.status)
      await expect(m.call(createRemoteApiClient(), m.input)).resolves.toEqual(profile)
    }
  })

  it.each(backendErrorStatuses)('preserves backend code %s in verification and typed HTTP %s errors', async (error_code, status) => {
    const value = { ...verification, state: 'error' as const, error_code }
    const profile = { ...server, verification: value }
    for (const m of jsonMutations) {
      mockReply(mutationPayload(m.name, profile), m.status)
      await expect(m.call(createRemoteApiClient(), m.input)).resolves.toEqual(mutationPayload(m.name, profile))
    }
    const fetcher = vi.fn().mockResolvedValueOnce(reply({ items: [profile] }))
      .mockResolvedValueOnce(reply(value))
    vi.stubGlobal('fetch', fetcher)
    const client = createRemoteApiClient()
    expect((await client.listServers()).items[0]!.verification).toEqual(value)
    await expect(client.verify(profileId, 7)).resolves.toEqual(value)
    for (const m of mutations) {
      mockReply({ code: error_code, details: {} }, status)
      await expect(m.call(client, m.input)).rejects.toMatchObject({ status, code: error_code })
    }
  })
  it.each(['http://127.0.0.1:8787/prefix', 'https://[::1]:9443/prefix',
    'https://remote.example/a b', 'https://remote.example:443/prefix'])('preserves backend URL without rewriting %s', async url => {
    const profile = { ...server, url }
    mockReply(profile, 201)
    await expect(createRemoteApiClient().createProfile({ name: 'Label', url })).resolves.toEqual(profile)
  })

  it.each([null, server])('recover returns the exact mandatory nullable profile envelope %j', async profile => {
    mockReply({ profile })
    await expect(createRemoteApiClient().recoverProfile(profileId, { ...revisionInput, confirmation })).resolves.toEqual({ profile })
  })
  it.each([null, [], {}, server, { profile: null, extra: true }, { profile: null, token: credentialCanary }])(
    'rejects malformed recovery envelope %j', async payload => {
      mockReply(payload)
      await expectInvalidResponse(createRemoteApiClient().recoverProfile(profileId, { ...revisionInput, confirmation }))
    },
  )
  it.each([null, undefined, {}, { profile_id: profileId }, { ...confirmation, extra: true },
    { ...confirmation, profile_id: profileId.toUpperCase() }, { ...confirmation, action: true }])(
    'requires explicit well-formed recovery confirmation %j before fetch', async value => {
      await expect(createRemoteApiClient().recoverProfile(profileId, {
        ...revisionInput, confirmation: value,
      } as RecoverRemoteProfileInput)).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
      expect(fetch).not.toHaveBeenCalled()
    },
  )
  it('forwards a structurally valid mismatched confirmation to the backend typed 409', async () => {
    const input = { ...revisionInput, confirmation: { ...confirmation, profile_id: cursor } }
    const fetcher = mockReply({ code: 'profile_recovery_confirmation_conflict', details: {} }, 409)
    await expect(createRemoteApiClient().recoverProfile(profileId, input)).rejects.toMatchObject({ status: 409 })
    expect(JSON.parse(fetcher.mock.calls[0]![1].body)).toEqual(input)
  })
  it.each([undefined, false, 1, 'true', null])('URL change requires literal clear_credential:true, not %j', async clear_credential => {
    await expect(createRemoteApiClient().changeProfileUrl(profileId, {
      ...revisionInput, url: 'https://new.example', clear_credential,
    } as ChangeRemoteProfileUrlInput)).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each(['', '  \n ', null, true, 1])('rejects malformed/empty token %j without echoing it', async token => {
    await expect(createRemoteApiClient().setCredential(profileId, { ...revisionInput, token } as RemoteCredentialInput))
      .rejects.toMatchObject({ status: 422, code: 'invalid_request', details: {} })
    expect(fetch).not.toHaveBeenCalled()
  })
  it('does not retain or resend credential on subsequent explicit mutations', async () => {
    const fetcher = vi.fn().mockImplementation(async () => reply(server))
    vi.stubGlobal('fetch', fetcher)
    const client = createRemoteApiClient()
    const input = { ...revisionInput, token: credentialCanary }
    await client.setCredential(profileId, input)
    expect(input).toEqual({ ...revisionInput, token: credentialCanary })
    await client.renameProfile(profileId, { expected_revision: 8, name: 'Next' })
    expect(fetcher.mock.calls[0]![0]).not.toContain(credentialCanary)
    expect(JSON.stringify(fetcher.mock.calls[1])).not.toContain(credentialCanary)
    expect(JSON.stringify(client)).not.toContain(credentialCanary)
  })
})

describe('Remote renderer disposal guards', () => {
  const methods = ['listServers', 'verify', 'listProjects'] as const
  function call(method: typeof methods[number], scope: ProjectSession) {
    const client = createRemoteApiClient(scope)
    return method === 'verify' ? client.verify(profileId, 1)
      : method === 'listProjects' ? client.listProjects(profileId) : client.listServers()
  }

  it.each(methods)('%s rejects an already disposed renderer before fetch', async method => {
    const scope = session()
    scope.invalidate()
    await expect(call(method, scope)).rejects.toBeInstanceOf(StaleProjectResponseError)
    expect(fetch).not.toHaveBeenCalled()
  })

  for (const method of methods) {
    it.each(['success', 'http-error', 'network-error'])(`${method} discards stale fetch %s`, async outcome => {
      const scope = session(), pendingFetch = deferred<Response>()
      vi.stubGlobal('fetch', vi.fn(() => pendingFetch.promise))
      const pending = call(method, scope)
      scope.invalidate()
      if (outcome === 'network-error') pendingFetch.reject(new Error('late connection failure'))
      else pendingFetch.resolve(reply(outcome === 'http-error'
        ? { code: 'studio_unauthorised', details: {} } : { items: [] }, outcome === 'http-error' ? 401 : 200))
      await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    })

    it.each(['success', 'http-error', 'body-error'])(`${method} discards stale body %s`, async outcome => {
      const scope = session(), pendingBody = deferred<unknown>(), reading = deferred<void>()
      const response = reply({}, outcome === 'http-error' ? 409 : 200)
      response.json = vi.fn(() => { reading.resolve(); return pendingBody.promise })
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
      const pending = call(method, scope)
      await reading.promise
      scope.invalidate()
      if (outcome === 'body-error') pendingBody.reject(new Error('late JSON failure'))
      else pendingBody.resolve(outcome === 'http-error'
        ? { code: 'profile_revision_conflict', details: {} } : { items: [] })
      await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    })
  }
})

// VS-156: C0/C9 command contracts. All fetches are synthetic; no live I/O.
const catalogProject = {
  id: 'game.v2_1-a', display_name: '  Historical\nlabel\u0000  ',
  storage_epoch: '0123456789abcdef0123456789abcdef', metadata_revision: 3, created_at: '2026-11-16T10:00:00Z',
}
const createProjectInput = { id: catalogProject.id, display_name: '  New game  ', operation_id: 'Create.Op_1-2', expected_revision: 7 }
const unknownCreateCodes = ['create_unknown', 'transport_unavailable', 'invalid_response', 'version_mismatch',
  'redirect_refused', 'temporarily_unavailable', 'remote_error']
const rejectedCreateCodes = ['invalid_id', 'invalid_display_name', 'invalid_request', 'unauthorised',
  'project_not_found', 'metadata_conflict', 'snapshot_changed', 'payload_too_large', 'remote_error']
const catalogOutcomes = [
  { state: 'created', project: catalogProject, error_code: null },
  { state: 'found', project: catalogProject, error_code: null },
  { state: 'conflict', project: null, error_code: 'project_exists' },
  { state: 'absent', project: null, error_code: 'project_not_found' },
  ...unknownCreateCodes.map(error_code => ({ state: 'unknown', project: null, error_code })),
  ...rejectedCreateCodes.map(error_code => ({ state: 'rejected', project: null, error_code })),
]
const catalogCommands = [
  { name: 'createProject', suffix: '/projects', input: createProjectInput,
    call: (client: RemoteClient, input: unknown = createProjectInput, id = profileId, _operation?: unknown) =>
      client.createProject(id, input as Parameters<RemoteClient['createProject']>[1]) },
  { name: 'observe', suffix: `/projects/operations/${createProjectInput.operation_id}/observe`, input: revisionInput,
    call: (client: RemoteClient, input: unknown = revisionInput, id = profileId, operation: unknown = createProjectInput.operation_id) =>
      client.observe(id, operation as string, input as RemoteRevisionInput) },
] as const
function catalogStatus(value: { state: unknown }) { return value.state === 'created' ? 201 : 200 }
function commandOutcome(name: string) { return catalogOutcomes[name === 'createProject' ? 0 : 1]! }
function withoutField(value: object, field: string) {
  const copy: Record<string, unknown> = { ...value }
  delete copy[field]
  return copy
}
const invalidCatalogIds = ['', 'Game', '-game', '_game', '.game', 'a'.repeat(65), 'a/b', 'a\\b',
  'a b', 'game\n', 'game\r', 'game\u0000', 'игра', 'g%61me', 'game?x=1', 'game#x', 1, null, true, undefined]
const invalidOperations = ['', 'a'.repeat(129), 'op/1', 'op\\1', ' op', 'op ', 'op\n', 'op\r',
  'op\u0000', 'оп', '%2e', 'op?x=1', 'op#x', null, 1, true, undefined]
const invalidCatalogRevisions = [0, -1, 1.5, NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER + 1,
  '1', null, undefined, true, false]
const malformedCatalogProjects: [string, unknown][] = [
  ['null', null], ['array', []], ['empty', {}], ['primitive', 'project'],
  ...Object.keys(catalogProject).map((field): [string, unknown] => [`missing ${field}`, withoutField(catalogProject, field)]),
  ['extra field', { ...catalogProject, token: credentialCanary }],
  ...invalidCatalogIds.map((id): [string, unknown] => [`id ${JSON.stringify(id)}`, { ...catalogProject, id }]),
  ...[null, true, 1, [], {}].map((display_name): [string, unknown] => ['display_name type', { ...catalogProject, display_name }]),
  ...['', 'f'.repeat(31), 'f'.repeat(33), 'F'.repeat(32), 'g'.repeat(32), `${'f'.repeat(32)}\n`, null, 32]
    .map((storage_epoch): [string, unknown] => [`storage_epoch ${storage_epoch}`, { ...catalogProject, storage_epoch }]),
  ...invalidCatalogRevisions.map((metadata_revision): [string, unknown] => [`revision ${String(metadata_revision)}`, { ...catalogProject, metadata_revision }]),
  ...['', null, 1, true, [], {}].map((created_at): [string, unknown] => ['created_at type/empty', { ...catalogProject, created_at }]),
]

describe('explicit Remote catalog Create/observe commands (C0/C9)', () => {
  for (const command of catalogCommands) {
    const outcomes = catalogOutcomes.filter(value => command.name === 'createProject' || ['found', 'absent'].includes(value.state))
    it.each(outcomes)(`${command.name} preserves $state/$error_code with one exact request and no implicit follow-up`, async value => {
      const input = { ...command.input }, initial = { ...input }, scope = session()
      const binding = scope.scope, generation = scope.generation, fetcher = mockReply(value, catalogStatus(value))
      const result = await command.call(createRemoteApiClient(scope), input)
      expect(result).toEqual(value)
      expect(Object.keys(result).sort()).toEqual(['error_code', 'project', 'state'])
      expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/remote/servers/${profileId}${command.suffix}`, {
        method: 'POST', cache: 'no-store', headers: { Authorization: 'Bearer studio-test-token', 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
      })
      expect(input).toEqual(initial)
      expect(scope.scope).toBe(binding)
      expect(scope.generation).toBe(generation)
    })
    it(`${command.name} uses identical global URLs for A/B/unbound/legacy without rebinding`, async () => {
      const value = commandOutcome(command.name)
      const fetcher = vi.fn().mockImplementation(async () => reply(value, catalogStatus(value)))
      vi.stubGlobal('fetch', fetcher)
      for (const scope of [session('A'), session('B'), new ProjectSession('unbound'), new ProjectSession()]) {
        const binding = scope.scope, generation = scope.generation
        await command.call(createRemoteApiClient(scope))
        expect(scope.scope).toBe(binding)
        expect(scope.generation).toBe(generation)
      }
      expect(fetcher.mock.calls.map(([url]) => url)).toEqual(Array(4).fill(`/api/remote/servers/${profileId}${command.suffix}`))
    })
    for (const value of outcomes) {
      it.each([200, 201, 202, 204, 205, 206, 299].filter(status => status !== catalogStatus(value)))(
        `${command.name} ${value.state}/${value.error_code} rejects wrong HTTP %s`, async status => {
          const response = [204, 205].includes(status) ? new Response(null, { status }) : reply(value, status)
          const json = vi.spyOn(response, 'json'), fetcher = vi.fn().mockResolvedValue(response)
          vi.stubGlobal('fetch', fetcher)
          await expectInvalidResponse(command.call(createRemoteApiClient()))
          expect(fetcher).toHaveBeenCalledTimes(1)
          if (![200, 201].includes(status) || command.name === 'observe') expect(json).not.toHaveBeenCalled()
        })
      it.each(['state', 'project', 'error_code'])(`${command.name} ${value.state}/${value.error_code} requires %s even nullable`, async field => {
        mockReply(withoutField(value, field), catalogStatus(value))
        await expectInvalidResponse(command.call(createRemoteApiClient()))
        expect(fetch).toHaveBeenCalledTimes(1)
      })
      it.each(['token', 'request_id', 'selected', 'ownership', 'retry', 'message'])(
        `${command.name} ${value.state}/${value.error_code} forbids extra %s even null`, async field => {
          mockReply({ ...value, [field]: null }, catalogStatus(value))
          await expectInvalidResponse(command.call(createRemoteApiClient()))
          expect(fetch).toHaveBeenCalledTimes(1)
        })
    }
    // Exhaust mandatory nullable combinations and cross-state code vocabulary.
    const codes: unknown[] = [null, ...new Set(['project_exists', ...unknownCreateCodes, ...rejectedCreateCodes]),
      'identity_mismatch', 'future-code', credentialCanary, '', 1, true, {}, []]
    const invalidCombinations = ['created', 'found', 'conflict', 'unknown', 'rejected', 'absent', 'future', null, 1].flatMap(state =>
      [null, catalogProject].flatMap(project => codes.flatMap(error_code => {
        const supported = command.name === 'createProject' || state === 'found' || state === 'absent'
        const valid = supported && ((state === 'created' || state === 'found') && project !== null && error_code === null
          || project === null && (state === 'conflict' && error_code === 'project_exists'
            || state === 'absent' && error_code === 'project_not_found'
            || state === 'unknown' && unknownCreateCodes.includes(error_code as string)
            || state === 'rejected' && rejectedCreateCodes.includes(error_code as string)))
        return valid ? [] : [{ state, project, error_code }]
      })))
    it.each(invalidCombinations)(`${command.name} rejects invalid outcome combination %#`, async value => {
      mockReply(value, catalogStatus(value))
      const error = await command.call(createRemoteApiClient()).catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(RemoteApiError)
      expect(error).toMatchObject({ status: 502, code: 'invalid_response', details: {} })
      expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
      expect(fetch).toHaveBeenCalledTimes(1)
    })
    it.each([null, [], {}, true, 'created', 1, { code: 'remote_error', details: {} }, { result: commandOutcome(command.name) }])(
      `${command.name} rejects non-envelope success %j`, async value => {
        mockReply(value)
        await expectInvalidResponse(command.call(createRemoteApiClient()))
        expect(fetch).toHaveBeenCalledTimes(1)
      })
    for (const state of command.name === 'createProject' ? ['created', 'found'] : ['found']) {
      it.each(malformedCatalogProjects)(`${command.name} ${state} rejects malformed project %s`, async (_label, project) => {
        mockReply({ state, project, error_code: null }, catalogStatus({ state }))
        await expectInvalidResponse(command.call(createRemoteApiClient()))
        expect(fetch).toHaveBeenCalledTimes(1)
      })
      it.each(['a', '0', `z${'._-a09'.repeat(10)}._-`, 'a'.repeat(64)])(
        `${command.name} ${state} preserves valid ID %s, literal label and max safe revision`, async id => {
          const value = { state, project: { ...catalogProject, id, metadata_revision: Number.MAX_SAFE_INTEGER }, error_code: null }
          mockReply(value, catalogStatus(value))
          await expect(command.call(createRemoteApiClient(), command.name === 'createProject'
            ? { ...createProjectInput, id, expected_revision: Number.MAX_SAFE_INTEGER } : { expected_revision: Number.MAX_SAFE_INTEGER }))
            .resolves.toEqual(value)
        })
    }
    it.each([...backendErrorStatuses, ['studio_unauthorised', 401], ['studio_forbidden', 403],
      ['method_not_allowed', 405], ['service_unavailable', 503]] as [string, number][])(
      `${command.name} preserves typed boundary %s/%s separately from outcomes without retry`, async (code, status) => {
        const details = { reason: 'changed', field: 'expected_revision' }, fetcher = mockReply({ code, details }, status)
        const error = await command.call(createRemoteApiClient()).catch((cause: unknown) => cause)
        expect(error).toBeInstanceOf(ApiError)
        expect(error).toBeInstanceOf(RemoteApiError)
        expect(error).toMatchObject({ code, status, details, isAuthFailure: status === 401 })
        expect(fetcher).toHaveBeenCalledTimes(1)
      })
    it.each([null, [], {}, { code: 'invalid_request' }, { details: {} }, { code: 1, details: {} },
      { code: 'invalid_request', details: null }, { code: 'invalid_request', details: [] },
      { code: 'invalid_request', details: { field: 1 } }, { code: 'invalid_request', details: {}, token: credentialCanary },
      { code: credentialCanary, details: {} }, { state: 'absent', project: null, error_code: 'project_not_found' }])(
      `${command.name} rejects malformed HTTP error %# without secret echo`, async value => {
        mockReply(value, 401)
        const error = await command.call(createRemoteApiClient()).catch((cause: unknown) => cause)
        expect(error).toBeInstanceOf(RemoteApiError)
        expect(error).toMatchObject({ status: 401, code: 'invalid_response', details: {}, isAuthFailure: true })
        expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
        expect(fetch).toHaveBeenCalledTimes(1)
      })
    it.each([200, 401, 503])(`${command.name} maps non-JSON HTTP %s without retry or echo`, async status => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(credentialCanary, { status })))
      const error = await command.call(createRemoteApiClient()).catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(RemoteApiError)
      expect(error).toMatchObject({ status: status === 200 ? 502 : status, code: 'invalid_response', details: {} })
      expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
      expect(fetch).toHaveBeenCalledTimes(1)
    })
    it(`${command.name} sanitizes a network error without retry`, async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error(credentialCanary)))
      const error = await command.call(createRemoteApiClient()).catch((cause: unknown) => cause)
      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({ status: 0, isAuthFailure: false })
      expect(String(error) + JSON.stringify(error)).not.toContain(credentialCanary)
      expect(fetch).toHaveBeenCalledTimes(1)
    })
    it.each([null, [], {}, true, 'input', { ...command.input, extra: true },
      ...Object.keys(command.input).map(field => withoutField(command.input, field)),
      ...['url', 'token', 'profile_id', ...(command.name === 'observe' ? ['id', 'operation_id', 'display_name'] : [])]
        .map(field => ({ ...command.input, [field]: credentialCanary }))])(
      `${command.name} rejects malformed/extra input %# before fetch`, async input => {
        await expect(command.call(createRemoteApiClient(), input)).rejects.toMatchObject({ status: 422, code: 'invalid_request', details: {} })
        expect(fetch).not.toHaveBeenCalled()
      })
    it.each(['', 'invalid', profileId.toUpperCase(), profileId.replaceAll('-', ''), `{${profileId}}`, `urn:uuid:${profileId}`,
      `${profileId}\n`, `${profileId}/extra`, `%63${profileId.slice(1)}`, `${profileId}?x=1`, `${profileId}#x`, null, 1, true])(
      `${command.name} rejects noncanonical profile %j`, async id => {
        await expect(command.call(createRemoteApiClient(), command.input, id as string)).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
        expect(fetch).not.toHaveBeenCalled()
      })
    it.each(invalidCatalogRevisions)(`${command.name} rejects invalid CAS %j before fetch`, async expected_revision => {
      await expect(command.call(createRemoteApiClient(), { ...command.input, expected_revision }))
        .rejects.toMatchObject({ status: 422, code: 'invalid_request' })
      expect(fetch).not.toHaveBeenCalled()
    })
    it.each(invalidOperations)(`${command.name} rejects invalid operation %# before fetch`, async operation => {
      const client = createRemoteApiClient()
      // Do not invoke this test helper's default for undefined input.
      const pending = command.name === 'createProject'
        ? client.createProject(profileId, { ...createProjectInput, operation_id: operation as string })
        : client.observe(profileId, operation as string, revisionInput)
      await expect(pending).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
      expect(fetch).not.toHaveBeenCalled()
    })
    it.each(['A', '0', 'Op._-1', 'a'.repeat(128)])(`${command.name} preserves exact operation %s`, async operation => {
      const value = commandOutcome(command.name), fetcher = mockReply(value, catalogStatus(value))
      const input = command.name === 'createProject' ? { ...createProjectInput, operation_id: operation } : revisionInput
      await command.call(createRemoteApiClient(), input, profileId, operation)
      expect(fetcher).toHaveBeenCalledTimes(1)
      expect(fetcher.mock.calls[0]![0]).toBe(`/api/remote/servers/${profileId}${command.name === 'createProject'
        ? '/projects' : `/projects/operations/${operation}/observe`}`)
      expect(JSON.parse(fetcher.mock.calls[0]![1].body)).toEqual(input)
    })
    it(`${command.name} rejects an already disposed renderer before fetch`, async () => {
      const scope = session()
      scope.invalidate()
      await expect(command.call(createRemoteApiClient(scope))).rejects.toBeInstanceOf(StaleProjectResponseError)
      expect(fetch).not.toHaveBeenCalled()
    })
    it.each(['success', 'http-error', 'network-error'])(`${command.name} fences stale fetch %s before any body read`, async outcome => {
      const scope = session(), waiting = deferred<Response>(), value = commandOutcome(command.name)
      const response = outcome === 'http-error' ? reply({ code: 'studio_unauthorised', details: {} }, 401) : reply(value, catalogStatus(value))
      const json = vi.spyOn(response, 'json'), text = vi.spyOn(response, 'text'), fetcher = vi.fn(() => waiting.promise)
      vi.stubGlobal('fetch', fetcher)
      const pending = command.call(createRemoteApiClient(scope))
      scope.invalidate()
      if (outcome === 'network-error') waiting.reject(new Error(credentialCanary))
      else waiting.resolve(response)
      await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
      expect(json).not.toHaveBeenCalled()
      expect(text).not.toHaveBeenCalled()
      expect(fetcher).toHaveBeenCalledTimes(1)
    })
    it.each(['success', 'http-error', 'success-body-error', 'http-body-error'])(`${command.name} fences stale body %s`, async outcome => {
      const scope = session(), waiting = deferred<unknown>(), reading = deferred<void>()
      const value = commandOutcome(command.name), isError = outcome.startsWith('http')
      const response = reply({}, isError ? 409 : catalogStatus(value))
      response.json = vi.fn(() => { reading.resolve(); return waiting.promise })
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
      const pending = command.call(createRemoteApiClient(scope))
      await reading.promise
      scope.invalidate()
      if (outcome.endsWith('body-error')) waiting.reject(new Error(credentialCanary))
      else waiting.resolve(isError ? { code: 'operation_conflict', details: {} } : value)
      await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
      expect(response.json).toHaveBeenCalledTimes(1)
      expect(fetch).toHaveBeenCalledTimes(1)
    })
  }
  it.each(['created', 'found'])('Create %s rejects a valid but different response ID', async state => {
    mockReply({ state, project: { ...catalogProject, id: 'other-game' }, error_code: null }, catalogStatus({ state }))
    await expectInvalidResponse(createRemoteApiClient().createProject(profileId, createProjectInput))
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it.each(['fetch', 'body'])('Create snapshots expected ID against caller mutation during %s await', async phase => {
    const waiting = deferred<unknown>(), reading = deferred<void>(), input = { ...createProjectInput }
    const response = reply({}, 201)
    response.json = vi.fn(() => { reading.resolve(); return phase === 'body' ? waiting.promise : Promise.resolve({
      state: 'created', project: { ...catalogProject, id: 'mutated-game' }, error_code: null,
    }) })
    const fetcher = phase === 'fetch' ? vi.fn(() => waiting.promise) : vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetcher)
    const pending = createRemoteApiClient().createProject(profileId, input)
    if (phase === 'body') await reading.promise
    input.id = 'mutated-game'
    waiting.resolve(phase === 'fetch' ? response : { state: 'created', project: { ...catalogProject, id: input.id }, error_code: null })
    await expectInvalidResponse(pending)
    expect(JSON.parse(fetcher.mock.calls[0]![1].body).id).toBe(catalogProject.id)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('Create accepts original response ID even if caller mutates input while awaiting', async () => {
    const waiting = deferred<Response>(), input = { ...createProjectInput }, fetcher = vi.fn(() => waiting.promise)
    vi.stubGlobal('fetch', fetcher)
    const pending = createRemoteApiClient().createProject(profileId, input)
    input.id = 'mutated-game'
    waiting.resolve(reply(catalogOutcomes[0], 201))
    await expect(pending).resolves.toEqual(catalogOutcomes[0])
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('observe uses reservation ID from backend, not a cached preceding Create ID', async () => {
    const found = { state: 'found', project: { ...catalogProject, id: 'reserved-other-game' }, error_code: null }
    const fetcher = vi.fn().mockResolvedValueOnce(reply(catalogOutcomes[0], 201)).mockResolvedValueOnce(reply(found))
    vi.stubGlobal('fetch', fetcher)
    const client = createRemoteApiClient()
    await client.createProject(profileId, createProjectInput)
    expect(fetcher).toHaveBeenCalledTimes(1)
    await expect(client.observe(profileId, 'other-operation', revisionInput)).resolves.toEqual(found)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(JSON.parse(fetcher.mock.calls[1]![1].body)).toEqual(revisionInput)
  })
  it.each(invalidCatalogIds)('Create rejects noncanonical input project ID %# before fetch', async id => {
    await expect(createRemoteApiClient().createProject(profileId, { ...createProjectInput, id: id as string }))
      .rejects.toMatchObject({ status: 422, code: 'invalid_request' })
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each([null, 1, true, [], {}, undefined])('Create validates display_name type %# before fetch', async display_name => {
    await expect(createRemoteApiClient().createProject(profileId, { ...createProjectInput, display_name: display_name as string }))
      .rejects.toMatchObject({ status: 422, code: 'invalid_request' })
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each(['', ' \n ', '\u0000label', 'x'.repeat(1000), '  Игpa  '])('Create leaves label normalization to backend %#', async display_name => {
    const value = { state: 'rejected', project: null, error_code: 'invalid_display_name' }, fetcher = mockReply(value)
    await expect(createRemoteApiClient().createProject(profileId, { ...createProjectInput, display_name })).resolves.toEqual(value)
    expect(JSON.parse(fetcher.mock.calls[0]![1].body).display_name).toBe(display_name)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it.each(['.', '..'])('both commands reject unobservable dot-segment %s before reserving or rewriting it', async operation => {
    await expect(createRemoteApiClient().observe(profileId, operation, revisionInput)).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
    await expect(createRemoteApiClient().createProject(profileId, { ...createProjectInput, operation_id: operation }))
      .rejects.toMatchObject({ status: 422, code: 'invalid_request' })
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each(catalogCommands)('$name rejects undefined input without defaults before fetch', async command => {
    const client = createRemoteApiClient()
    const pending = command.name === 'createProject'
      ? client.createProject(profileId, undefined as unknown as Parameters<RemoteClient['createProject']>[1])
      : client.observe(profileId, createProjectInput.operation_id, undefined as unknown as RemoteRevisionInput)
    await expect(pending).rejects.toMatchObject({ status: 422, code: 'invalid_request' })
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each(['', '  ', '\n', '  Historical\u0000label  '])('keeps historical response label literal %# without normalization', async display_name => {
    for (const command of catalogCommands) {
      const value = { ...commandOutcome(command.name), project: { ...catalogProject, display_name, metadata_revision: 1, created_at: ' ' } }
      mockReply(value, catalogStatus(value))
      await expect(command.call(createRemoteApiClient())).resolves.toEqual(value)
      expect(fetch).toHaveBeenCalledTimes(1)
    }
  })
})
