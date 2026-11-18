/**
 * The one answer the client reads as a stream: a folder rename's progress (block-editor spec
 * §5.2).
 *
 * What is pinned is what a network does to a stream and a JSON parser does not expect — lines
 * cut in half, a character cut in half — and the two ways a rename can fail that a status code
 * alone does not tell apart: refused before anything moved, and stopped after the folder did.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DocFolderRenameFrame, DocumentFence } from '@/api/types'
import { ProjectSession, StaleProjectResponseError } from '@/session/project-session'
import { setLocale } from '@/i18n'

vi.mock('@/api/auth', () => ({
  authHeaders: () => ({}),
  withTokenParam: (url: string) => url,
}))

const { api, ApiError, createApiClient, readNdjson, renameDocFolder } = await import('./client')

/** A body that arrives in exactly these pieces, the way a network may cut it. */
function streamOf(...chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
      }
      controller.close()
    },
  })
}

function ndjson(...frames: object[]): string {
  return frames.map((frame) => `${JSON.stringify(frame)}\n`).join('')
}

const RENAMED = {
  type: 'renamed',
  path: 'mechanics',
  previous_path: 'features',
  moved: 2,
  documents: [{ path: 'qa/plan.md', previous_path: null, title: 'Plan', links: 3 }],
}
const DOCUMENT = {
  type: 'document',
  path: 'qa/plan.md',
  previous_path: null,
  title: 'Plan',
  links: 3,
  status: 'updated',
  error: null,
}
const DONE = {
  type: 'done',
  path: 'mechanics',
  previous_path: 'features',
  moved: 2,
  updated: 1,
  links: 3,
  failed: 0,
}

afterEach(async () => {
  vi.unstubAllGlobals()
  await setLocale('en')
})

describe('task summary language (§10.4)', () => {
  it('captures the current reader language when creating, releasing, running or reviewing', async () => {
    const fetcher = vi.fn(async () => new Response('{}', {
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetcher)

    for (const language of ['ru', 'en'] as const) {
      await setLocale(language)
      fetcher.mockClear()
      await api.createTask('team', { assignee_id: 'agent', title: 'A task' })
      await api.updateTask('team', 'task', { status: 'pending' })
      await api.startTask('team', 'task')
      await api.startAllTasks('team')
      await api.requestChanges('team', 'task', 'Try again')
      expect(fetcher.mock.calls).toHaveLength(5)
      for (const [, init] of vi.mocked(fetch).mock.calls) {
        expect(JSON.parse(String(init?.body)).summary_locale).toBe(language)
      }
    }
  })

  it('does not change a task language for an unrelated edit', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
      headers: { 'Content-Type': 'application/json' },
    })))
    await setLocale('ru')
    await api.updateTask('team', 'task', { title: 'New title' })
    const [, init] = vi.mocked(fetch).mock.calls[0]!
    expect(JSON.parse(String(init?.body))).toEqual({ title: 'New title' })
  })
})

describe('reading newline-delimited JSON', () => {
  it('puts back together a line and a character that arrived in two pieces', async () => {
    const values: unknown[] = []
    const bytes = new TextEncoder().encode('{"name":"Тоа"}\n')
    // Split inside the two-byte `о`, and separately inside the next line.
    await readNdjson(
      streamOf(bytes.slice(0, 12), bytes.slice(12), '{"n":', '1}\n\n{"n":2}'),
      (value) => values.push(value),
    )
    expect(values).toEqual([{ name: 'Тоа' }, { n: 1 }, { n: 2 }])
  })
})

describe('renaming a folder over the stream', () => {
  function answer(response: Response): void {
    vi.stubGlobal('fetch', vi.fn(async () => response))
  }

  it('hands over every frame as it comes and resolves with the last', async () => {
    answer(new Response(streamOf(ndjson(RENAMED), ndjson(DOCUMENT, DONE)), { status: 200 }))
    const heard: DocFolderRenameFrame['type'][] = []

    const done = await renameDocFolder('features', 'mechanics', (frame) => heard.push(frame.type))

    expect(heard).toEqual(['renamed', 'document', 'done'])
    expect(done).toEqual(DONE)
    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/docs/folders/rename')
    expect(JSON.parse(String(init?.body))).toEqual({ path: 'features', name: 'mechanics' })
  })

  it('throws the reason, and hands over nothing, when the rename is refused', async () => {
    answer(
      new Response(JSON.stringify({ detail: "'qa' already exists" }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const onFrame = vi.fn()

    const refused = renameDocFolder('features', 'qa', onFrame)

    await expect(refused).rejects.toBeInstanceOf(ApiError)
    await expect(refused).rejects.toMatchObject({ status: 409, detail: "'qa' already exists" })
    expect(onFrame).not.toHaveBeenCalled()
  })

  it('throws when the stream ends without saying the rename is done', async () => {
    answer(new Response(streamOf(ndjson(RENAMED)), { status: 200 }))
    const onFrame = vi.fn()

    await expect(renameDocFolder('features', 'mechanics', onFrame)).rejects.toThrow(
      'stopped before it said it was done',
    )
    expect(onFrame, 'the folder had already moved, and the caller heard so').toHaveBeenCalledTimes(1)
  })

  it('throws what the backend said when mending the links failed', async () => {
    answer(new Response(streamOf(ndjson(RENAMED, { type: 'error', error: 'disk full' })), { status: 200 }))

    await expect(renameDocFolder('features', 'mechanics', () => {})).rejects.toThrow('disk full')
  })
})

function scopedClient() {
  const session = new ProjectSession('runtime-A')
  session.bind({ project_id: 'runtime-A', epoch: 'runtime-epoch' })
  return { session, client: createApiClient(session) }
}

const FENCE: DocumentFence = {
  backend_epoch: 'lease-epoch', expected_binding_generation: 7,
  editor_id: 'editor-A', client_instance_id: 'client-A', sid: 'room-A', version: 11,
}

describe('captured document mutation transport (studio-design §5.3)', () => {
  const cases = [
    ['save', 'PUT', '/docs/a%20b/note.md', { content: 'text', author: null,
      expected_updated_at: 'read-version', collab_session: 'room-A' },
      (client: ReturnType<typeof createApiClient>, fence?: DocumentFence) =>
        client.saveDoc('a b/note.md', 'text', null, 'read-version', 'room-A', fence)],
    ['create', 'PUT', '/docs/a%20b/note.md', { content: 'text', exclusive: true },
      (client: ReturnType<typeof createApiClient>, fence?: DocumentFence) =>
        client.createDoc('a b/note.md', 'text', fence)],
    ['move', 'POST', '/docs/move', { source: 'a.md', target: 'b.md' },
      (client: ReturnType<typeof createApiClient>, fence?: DocumentFence) => client.moveDoc('a.md', 'b.md', fence)],
    ['folder delete', 'POST', '/docs/folders/delete', { path: 'features' },
      (client: ReturnType<typeof createApiClient>, fence?: DocumentFence) => client.deleteDocFolder('features', fence)],
    ['folder style', 'POST', '/docs/folders/style', { path: 'features', icon: null, color: 'blue' },
      (client: ReturnType<typeof createApiClient>, fence?: DocumentFence) =>
        client.setDocFolderStyle({ path: 'features', icon: null, color: 'blue' }, fence)],
  ] as const

  it.each(cases)('%s serializes the explicit fence before awaiting', async (_name, method, path, body, send) => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const { client } = scopedClient()
    const fence = { ...FENCE }
    const pending = send(client, fence)
    fence.expected_binding_generation = 999
    fence.backend_epoch = 'new-epoch'
    const [url, init] = vi.mocked(fetch).mock.calls[0]!
    expect(String(url)).toBe(`/api/projects/runtime-A${path}?project_id=runtime-A&epoch=runtime-epoch`)
    expect(init?.method).toBe(method)
    expect(JSON.parse(String(init?.body))).toEqual({ ...body, ...FENCE })
    finish(Response.json({}))
    await pending
    expect(fetch).toHaveBeenCalledTimes(1) // no metadata lookup or generation upgrade
  })

  it.each(cases)('%s without a fence stays a legacy request, never fetches current G', async (_name, _method, _path, body, send) => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({})))
    await send(scopedClient().client)
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]?.body))).toEqual(body)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('DELETE passes exactly the admitted query fields, including zero G, not a JSON body/version', async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const fence = { ...FENCE, expected_binding_generation: 0 }
    const pending = scopedClient().client.deleteDoc('a b/note.md', fence)
    fence.expected_binding_generation = 999
    const [url, init] = vi.mocked(fetch).mock.calls[0]!
    const parsed = new URL(String(url), 'http://studio.invalid')
    expect(parsed.pathname).toBe('/api/projects/runtime-A/docs/a%20b/note.md')
    expect(Object.fromEntries(parsed.searchParams)).toEqual({
      backend_epoch: 'lease-epoch', expected_binding_generation: '0', editor_id: 'editor-A',
      client_instance_id: 'client-A', sid: 'room-A', project_id: 'runtime-A', epoch: 'runtime-epoch',
    })
    expect(init?.method).toBe('DELETE')
    expect(init?.body).toBeUndefined()
    finish(Response.json({}))
    await pending
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('legacy DELETE has no implicit fence query', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({})))
    await scopedClient().client.deleteDoc('a.md')
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/api/projects/runtime-A/docs/a.md?project_id=runtime-A&epoch=runtime-epoch')
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('streams rename with the same explicit captured fence', async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const { client } = scopedClient()
    const fence = { ...FENCE }
    const onFrame = vi.fn()
    const pending = client.renameDocFolder('features', 'mechanics', onFrame, fence)
    fence.expected_binding_generation++
    const [url, init] = vi.mocked(fetch).mock.calls[0]!
    expect(String(url)).toBe('/api/projects/runtime-A/docs/folders/rename?project_id=runtime-A&epoch=runtime-epoch')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ path: 'features', name: 'mechanics', ...FENCE })
    finish(new Response(streamOf(ndjson(DONE))))
    await expect(pending).resolves.toEqual(DONE)
    expect(onFrame).toHaveBeenCalledWith(DONE)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('keeps the server save receipt rather than synthesizing an acknowledgement', async () => {
    const receipt = { editor_id: 'editor-A', backend_epoch: 'lease-epoch', binding_generation: 7,
      version: 11, last_saved_version: 42 }
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(receipt)))
    await expect(scopedClient().client.saveDoc('a.md', 'text', null, null, null, FENCE)).resolves.toEqual(receipt)
  })

  it('rejects a save receipt after renderer disposal', async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const { session, client } = scopedClient()
    const pending = client.saveDoc('a.md', 'text', null, 'read-version', null, FENCE)
    session.invalidate()
    finish(Response.json({ last_saved_version: 42 }))
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('rejects late streamed frames without handing them to a disposed renderer', async () => {
    const { session, client } = scopedClient()
    let controller!: ReadableStreamDefaultController<Uint8Array>
    const cancelled = vi.fn()
    const body = new ReadableStream<Uint8Array>({ start(value) { controller = value }, cancel: cancelled })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
    let heard!: () => void
    const firstFrame = new Promise<void>(resolve => { heard = resolve })
    const onFrame = vi.fn(() => heard())
    const pending = client.renameDocFolder('features', 'mechanics', onFrame, FENCE)
    controller.enqueue(new TextEncoder().encode(ndjson(RENAMED)))
    await firstFrame
    session.invalidate()
    controller.enqueue(new TextEncoder().encode(ndjson(DONE)))
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    expect(onFrame).toHaveBeenCalledExactlyOnceWith(RENAMED)
    expect(cancelled).toHaveBeenCalledTimes(1)
  })
})

describe('typed document admission failures', () => {
  it.each(['reload_required', 'stale_backend_epoch', 'stale_binding_generation', 'mutation_busy',
    'unknown_editor', 'editor_mismatch', 'editor_lease_expired', 'unknown_sid', 'pending_edits',
    'stale_editor_version', 'invalid_editor', 'use_remote_settings'])('%s never retries as an overwrite', async code => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code, details: { expected: 7 } }, { status: 409 })))
    const pending = scopedClient().client.saveDoc('a.md', 'unsaved text', null, 'read-version', null, FENCE)
    await expect(pending).rejects.toMatchObject({ status: 409, code, detail: code, details: { expected: 7 },
      isFenceConflict: true, isDiskConflict: false })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]?.body)).expected_updated_at).toBe('read-version')
  })

  it('parses the same typed early refusal before any NDJSON frame', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code: 'unknown_sid', details: {} }, { status: 409 })))
    const onFrame = vi.fn()
    await expect(scopedClient().client.renameDocFolder('a', 'b', onFrame, FENCE)).rejects.toMatchObject({
      code: 'unknown_sid', isFenceConflict: true, isDiskConflict: false,
    })
    expect(onFrame).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('preserves nested FastAPI errors, plain disk conflicts and legacy construction', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(Response.json({ detail: { code: 'stale_backend_epoch', details: { expected: 'old' } } }, { status: 409 }))
      .mockResolvedValueOnce(Response.json({ detail: 'Document changed on disk' }, { status: 409 }))
    vi.stubGlobal('fetch', fetcher)
    const { client } = scopedClient()
    await expect(client.deleteDoc('a.md', FENCE)).rejects.toMatchObject({
      code: 'stale_backend_epoch', details: { expected: 'old' }, isFenceConflict: true,
    })
    await expect(client.deleteDoc('a.md', FENCE)).rejects.toMatchObject({
      detail: 'Document changed on disk', code: null, isFenceConflict: false, isDiskConflict: true,
    })
    expect(new ApiError(401, 'No token')).toMatchObject({ status: 401, detail: 'No token', isAuthFailure: true })
  })

  it('unknown typed 409 is not a disk conflict or an overwrite invitation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code: 'future_refusal', details: { why: 'blocked' } }, { status: 409 })))
    await expect(scopedClient().client.createDoc('a.md', 'text', FENCE)).rejects.toMatchObject({
      code: 'future_refusal', isFenceConflict: false, isDiskConflict: false, details: { why: 'blocked' },
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([null, [], { detail: ['invalid'] }])('handles old/malformed error payload %j without inventing a code', async payload => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(payload, { status: 422, statusText: 'Invalid' })))
    await expect(scopedClient().client.createDoc('a.md', 'text', FENCE)).rejects.toMatchObject({
      status: 422, code: null, isFenceConflict: false, isDiskConflict: false,
    })
  })
})

describe('project Remote endpoint contracts (studio-design §5.2; future commands mock-only)', () => {
  const register = { document_path: 'a.md', client_instance_id: 'client-A',
    backend_epoch: 'lease-epoch', expected_binding_generation: 7, sid: 'room-A' }
  const heartbeat = { ...register, dirty: false, version: 11, last_saved_version: 42 }
  const materialization = { ...register, sid: 'room-A', version: 11, revision: 73 }
  const confirmation = { ...materialization, proof_id: 'proof-A', content: '# Мой snapshot\r\n' }
  const prepare = { backend_epoch: 'captured-backend-A', expected_revision: 2, config_fingerprint: 'fingerprint', profile_id: 'profile-A',
    persistent_id: 'persistent-new', url: 'https://remote.invalid/team', operation: 'select' as const,
    catalog_id: 'persistent-new', catalog_instance_id: 'storage-A', migration_plan_id: 'migration-A' }
  const commit = { expected_revision: 2, expected_generation: 7, config_fingerprint: 'fingerprint',
    confirmation: { url: 'https://remote.invalid/team', persistent_id: 'persistent-new' } }
  const profile = { config_fingerprint: 'fingerprint', profile_id: 'profile-A', name: 'Personal',
    import_legacy_credential: false, confirmation: { url: 'https://remote.invalid/team' } }
  const resolve = { profile_id: 'profile-A', expected_profile_revision: 3, config_fingerprint: 'fingerprint' }
  const previewBody = { expected_revision: 2, config_fingerprint: 'fingerprint', backend_epoch: 'captured-backend-A' }
  const enable = { expected_revision: 2, expected_generation: 7, preview_id: 'preview-A', fingerprint: 'preview-fingerprint',
    confirmation: { ...commit.confirmation, action: 'reconcile' as const }, config_fingerprint: 'fingerprint',
    backend_epoch: 'captured-backend-A' }
  const recover = { change_id: 'a'.repeat(32), expected_revision: 2, config_fingerprint: 'fingerprint',
    confirmation: { url: 'https://remote.invalid/team', persistent_id: null, action: 'recover' as const },
    backend_epoch: 'captured-backend-A' }
  const archive = { expected_revision: 2, expected_generation: 7, preview_id: 'preview-A', fingerprint: 'preview-fingerprint',
    confirmation: { ...commit.confirmation, action: 'archive_checkpoint' as const }, config_fingerprint: 'fingerprint',
    backend_epoch: 'captured-backend-A' }
  type Client = ReturnType<typeof createApiClient>
  const calls: [string, string, string, unknown, (client: Client) => Promise<unknown>][] = [
    ['remote GET', 'GET', '', undefined, client => client.projectRemote()],
    ['register editor', 'POST', '/editors', register, client => client.registerRemoteEditor(register)],
    ['heartbeat editor', 'POST', '/editors/editor-A/heartbeat', heartbeat, client => client.heartbeatRemoteEditor('editor-A', heartbeat)],
    ['materialization', 'POST', '/editors/editor-A/materialization', materialization,
      client => client.materializeRemoteEditor('editor-A', materialization)],
    ['materialization confirm', 'POST', '/editors/editor-A/materialization/confirm', confirmation,
      client => client.confirmRemoteEditorMaterialization('editor-A', confirmation)],
    ['delete editor', 'DELETE', '/editors/editor-A', heartbeat, client => client.deleteRemoteEditor('editor-A', heartbeat)],
    ['prepare', 'POST', '/prepare', prepare, client => client.prepareRemoteChange(prepare)],
    ['quiesce', 'POST', '/changes/change-A/quiesce', { expected_generation: 7 },
      client => client.quiesceRemoteChange('change-A', { expected_generation: 7 })],
    ['editor ack', 'POST', '/changes/change-A/editor-ack', { editor_id: 'editor-A', old_generation: 7, last_saved_version: 42 },
      client => client.ackRemoteEditor('change-A', { editor_id: 'editor-A', old_generation: 7, last_saved_version: 42 })],
    ['commit', 'POST', '/changes/change-A/commit', commit, client => client.commitRemoteChange('change-A', commit)],
    ['cancel', 'DELETE', '/changes/change-A', undefined, client => client.cancelRemoteChange('change-A')],
    ['preview', 'POST', '/reconcile-preview', previewBody, client => client.previewRemoteReconciliation(previewBody)],
    ['enable', 'POST', '/enable', enable, client => client.enableProjectRemote(enable)],
    ['recover', 'POST', '/recover', recover, client => client.recoverProjectRemote(recover)],
    ['archive checkpoint', 'POST', '/checkpoint/archive', archive, client => client.archiveProjectRemoteCheckpoint(archive)],
    ['migration GET', 'GET', '/migration', undefined, client => client.remoteMigration()],
    ['migration profile', 'POST', '/migration/migration-A/profile', profile, client => client.selectRemoteMigrationProfile('migration-A', profile)],
    ['migration resolve', 'POST', '/migration/migration-A/resolve', resolve, client => client.resolveRemoteMigration('migration-A', resolve)],
  ]

  it.each(calls)('%s uses only the bound project method/path/body', async (_label, method, path, body, send) => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ observed: true })))
    const { client, session } = scopedClient()
    const before = session.scope
    await expect(send(client)).resolves.toEqual({ observed: true })
    const [url, init] = vi.mocked(fetch).mock.calls[0]!
    expect(String(url)).toBe(`/api/projects/runtime-A/remote${path}?project_id=runtime-A&epoch=runtime-epoch`)
    expect(init?.method).toBe(method)
    expect(init?.cache).toBe('no-store')
    if (body === undefined) expect(init?.body).toBeUndefined()
    else {
      expect(JSON.parse(String(init?.body))).toEqual(body)
      expect(init?.headers).toMatchObject({ 'Content-Type': 'application/json' })
    }
    expect(fetch).toHaveBeenCalledTimes(1) // no Verify, Select, reload, or activation side effect
    expect(session.scope).toBe(before)
  })

  it.each(calls)('%s cannot fall back to the legacy/global Remote namespace', async (_label, _method, _path, _body, send) => {
    vi.stubGlobal('fetch', vi.fn())
    await expect(send(createApiClient(new ProjectSession()))).rejects.toMatchObject({ code: 'reload_required' })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('serializes nested confirmations before awaiting and never repeats a stale command', async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const body = { ...commit, confirmation: { ...commit.confirmation } }
    const pending = scopedClient().client.commitRemoteChange('change-A', body)
    body.expected_generation++
    body.confirmation.persistent_id = 'different-id'
    const [, init] = vi.mocked(fetch).mock.calls[0]!
    expect(JSON.parse(String(init?.body))).toEqual(commit)
    finish(Response.json({ code: 'stale_binding_generation', details: {} }, { status: 409 }))
    await expect(pending).rejects.toMatchObject({ code: 'stale_binding_generation', isFenceConflict: true })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('rejects late JSON after renderer disposal, without rebinding or reloading', async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const { session, client } = scopedClient()
    const pending = client.projectRemote()
    session.invalidate()
    finish(Response.json({ binding_generation: 99 }))
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
    expect(session.scope).toEqual({ project_id: 'runtime-A', epoch: 'runtime-epoch' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('guards the JSON/error body await too', async () => {
    let finish!: (value: unknown) => void
    const { session, client } = scopedClient()
    const response = Response.json({}, { status: 409 })
    vi.spyOn(response, 'json').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    vi.stubGlobal('fetch', vi.fn(async () => response))
    const pending = client.projectRemote()
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    session.invalidate()
    finish({ code: 'stale_backend_epoch', details: {} })
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
  })
})


describe('project commit receipt numeric and epoch namespaces', () => {
  it('returns the exact safe-integer receipt and original backend epoch without relabeling or a second request', async () => {
    const session = new ProjectSession('runtime-A')
    session.bind({ project_id: 'runtime-A', epoch: 'runtime-epoch' })
    const client = createApiClient(session)
    const result = { project_id: 'runtime-A', epoch: 'runtime-epoch', backend_epoch: 'backend-lifetime-A',
      binding_generation: Number.MAX_SAFE_INTEGER, committed: true, change_id: 'change-A',
      binding_change: { change_id: 'change-A', state: 'committed' }, persistent_id: 'new-id',
      identity_revision: 4, config_fingerprint: 'a'.repeat(64), enabled: false, state: 'disabled',
      url: '', has_token: false, active_rooms: 0, pending_writes: 0 }
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(result)))
    const received = await client.commitRemoteChange('change-A', { expected_revision: 3,
      expected_generation: Number.MAX_SAFE_INTEGER - 1, config_fingerprint: 'b'.repeat(64),
      confirmation: { persistent_id: 'new-id', url: '' } })
    expect(received).toEqual(result)
    expect(received.binding_generation).toBe(Number.MAX_SAFE_INTEGER)
    expect(received.backend_epoch).not.toBe(received.epoch)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
