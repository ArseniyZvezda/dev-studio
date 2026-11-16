// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectSession, StaleProjectResponseError } from '@/session/project-session'
import { createApiClient } from './client'
import { isGlobalApiPath, scopedApiUrl, scopedWsUrl } from './project-scope'

vi.mock('./auth', () => ({ authHeaders: () => ({ Authorization: 'Bearer shared' }), withTokenParam: (url: string) => `${url}${url.includes('?') ? '&' : '?'}token=shared` }))

// Every request in this file must opt into a synthetic response, never real I/O.
beforeEach(() => vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected fetch') })))
afterEach(() => vi.unstubAllGlobals())

function session(id: string) {
  const result = new ProjectSession(id)
  result.bind({ project_id: id, epoch: 'epoch-1' })
  return result
}
const reply = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })

describe('Remote global URL classification (studio-design §5.1/§6, offline slice E)', () => {
  const profile = 'c503da2b-f8fe-4fb0-b945-86bc119495b3'
  const paths = ['/remote/servers', `/remote/servers/${profile}/verify`, `/remote/servers/${profile}/projects`]

  it('keeps exactly the existing Remote routes global across A/B bindings and tab selection', async () => {
    const a = session('A'), b = new ProjectSession('B')
    b.bind({ project_id: 'B', epoch: 'epoch-2' })
    const original = window.location.href
    const fetcher = vi.fn(async (_url: string) => reply({ items: [] }))
    vi.stubGlobal('fetch', fetcher)
    try {
      for (const active of ['A', 'B']) {
        window.history.replaceState({}, '', `/p/${active}/settings`)
        for (const path of paths) {
          expect(isGlobalApiPath(path)).toBe(true)
          for (const scope of [a, b, new ProjectSession('unbound')]) {
            const url = scopedApiUrl(scope, path)
            expect(url).toBe(`/api${path}`)
            await fetch(url) // Synthetic fetch only; no Remote client method or wiring.
          }
        }
      }
      expect(fetcher).toHaveBeenCalledTimes(18)
      expect(fetcher.mock.calls.every(([url]) => !String(url).includes('project_id=') && !String(url).includes('epoch='))).toBe(true)
      expect(a.scope).toEqual({ project_id: 'A', epoch: 'epoch-1' })
      expect(b.scope).toEqual({ project_id: 'B', epoch: 'epoch-2' })
    } finally { window.history.replaceState({}, '', original) }
  })

  it('preserves ordinary queries without injecting or interpreting project scope', () => {
    // The router accepts canonical UUIDs without constraining version/variant.
    for (const id of ['00000000-0000-0000-0000-000000000000', 'ffffffff-ffff-ffff-ffff-ffffffffffff']) {
      for (const action of ['verify', 'projects']) {
        const path = `/remote/servers/${id}/${action}`
        expect(isGlobalApiPath(path)).toBe(true)
        expect(scopedApiUrl(session('A'), path)).toBe(`/api${path}`)
      }
    }
    for (const path of paths) {
      for (const scope of [session('A'), session('B')]) {
        expect(scopedApiUrl(scope, path, { cursor: 'a b/+?', limit: 2, flag: false, empty: '', absent: null, omitted: undefined }))
          .toBe(`/api${path}?cursor=a+b%2F%2B%3F&limit=2&flag=false&empty=`)
      }
    }
  })

  it('does not globalize similar, extra, encoded or noncanonical route spellings', () => {
    const similar = [
      '/remote', '/remote/servers/', '/remote/servers-extra', '/remote/servers/new',
      `/remote/servers/${profile}`, `/remote/servers/${profile}/projects/extra`,
      `/remote/servers/${profile}/verify/`, `/remote/servers/${profile}/create`,
      `/remote/servers/${profile}/credentials`, `/remote/servers/${profile}/projects-extra`,
      `/remote/servers/${profile.toUpperCase()}/projects`, '/remote/servers/not-a-uuid/projects',
      `/remote/servers/${profile.replaceAll('-', '')}/verify`,
      `/remote/servers/{${profile}}/projects`, `/remote/servers/urn:uuid:${profile}/projects`,
      `/remote/servers/${profile}/%70rojects`, `/remote/servers/%63${profile.slice(1)}/verify`,
      `/remote/servers/${profile}/%252e%252e`, '/remote/servers/%252e%252e/projects',
      '/remote/servers/%252f/projects', '/Remote/servers', '/api/remote/servers',
    ]
    for (const path of similar) {
      expect(isGlobalApiPath(path), path).toBe(false)
      expect(scopedApiUrl(session('A'), path), path)
        .toBe(`/api/projects/A${path}?project_id=A&epoch=epoch-1`)
    }
  })

  it('never globalizes escape paths and rejects them before URL construction', () => {
    const escapes = [
      '//remote/servers', 'https://example.invalid/remote/servers',
      `/remote/servers/${profile}/projects?cursor=x`, `/remote/servers/${profile}/verify#x`,
      `/remote/servers/${profile}/../projects`, '/remote/servers/./projects',
      '/remote/servers/%2e%2e/projects', '/remote/servers/%2E/projects',
      '/remote/servers/%2f/projects', '/remote/servers/%5C/projects',
      '/remote/servers/\\/projects', '/remote/servers/%/projects',
    ]
    for (const path of escapes) {
      expect(isGlobalApiPath(path), path).toBe(false)
      expect(() => scopedApiUrl(session('A'), path), path).toThrow()
    }
    for (const path of paths) {
      for (const ending of ['\n', '\r', '\r\n', '\u2028', '\u2029']) {
        expect(isGlobalApiPath(`${path}${ending}`)).toBe(false)
        expect(scopedApiUrl(session('A'), `${path}${ending}`))
          .toBe(`/api/projects/A${path}${ending}?project_id=A&epoch=epoch-1`)
      }
    }
  })

  it('retains project REST/WS scope and rejects stale sessions even for global Remote URLs', () => {
    const a = session('A'), b = session('B')
    for (const scope of [a, b]) {
      expect(scopedApiUrl(scope, '/sync', { cursor: 'x' }))
        .toBe(`/api/projects/${scope.projectId}/sync?cursor=x&project_id=${scope.projectId}&epoch=epoch-1`)
      expect(scopedWsUrl(scope, true, 7))
        .toBe(`/ws/projects/${scope.projectId}/collab?project_id=${scope.projectId}&epoch=epoch-1&since_seq=7`)
      expect(() => scopedApiUrl(scope, '/sync', { epoch: 'foreign' })).toThrow('Conflicting')
      scope.invalidate()
      for (const path of paths) expect(() => scopedApiUrl(scope, path)).toThrow(StaleProjectResponseError)
      expect(() => scopedWsUrl(scope)).toThrow(StaleProjectResponseError)
    }
  })
})

describe('immutable renderer binding (§5.3.2)', () => {
  it('derives a fixed project route and never treats malformed handles as legacy', () => {
    expect(ProjectSession.fromPath('/p/A/documents').routeBase).toBe('/p/A')
    expect(ProjectSession.fromPath('/documents').isLegacy).toBe(true)
    for (const path of ['/p', '/p/', '/p//documents', '/p/%2e%2e/x', '/p/A%2fB/x', '/p/%/x']) {
      expect(() => ProjectSession.fromPath(path)).toThrow()
    }
    const a = session('A')
    expect(() => a.bind({ project_id: 'B', epoch: 'epoch-1' })).toThrow()
    expect(() => a.bind({ project_id: 'A', epoch: 'epoch-2' })).toThrow()
    expect(Object.isFrozen(a.scope)).toBe(true)
    expect(() => new ProjectSession('A').bind({ project_id: 'A', epoch: '' })).toThrow()
  })

  it('scopes resources and cursors, keeps global operations global, rejects escape paths', () => {
    const a = session('A')
    expect(scopedApiUrl(a, '/docs/shared.md')).toBe('/api/projects/A/docs/shared.md?project_id=A&epoch=epoch-1')
    expect(scopedWsUrl(a, false, 42)).toBe('/ws/projects/A?project_id=A&epoch=epoch-1&since_seq=42')
    expect(scopedWsUrl(a, true)).toBe('/ws/projects/A/collab?project_id=A&epoch=epoch-1')
    for (const path of ['/settings/application', '/project/recent', '/projects/open', '/updates', '/runtime/recover', '/fs/pick', '/plugins/x/install-env']) {
      expect(scopedApiUrl(a, path)).toBe(`/api${path}`)
    }
    for (const path of ['/project/open', '//evil/api', '/docs/../project', '/docs/%2e%2e/project', '/docs/%2f../project', '/projects/B/docs', '/docs?epoch=foreign']) {
      expect(() => scopedApiUrl(a, path)).toThrow()
    }
    expect(() => scopedApiUrl(a, '/docs', { epoch: 'foreign' })).toThrow()
    expect(() => scopedWsUrl(a, false, -1)).toThrow()
    expect(scopedApiUrl(new ProjectSession(), '/project/open')).toBe('/api/project/open')
  })

  it('bootstraps only its URL handle and fails closed for unknown or conflicting identities', async () => {
    const a = new ProjectSession('A')
    const client = createApiClient(a)
    const fetcher = vi.fn().mockResolvedValueOnce(reply({ project_id: 'A', epoch: 'epoch-1', root: 'A' }))
    vi.stubGlobal('fetch', fetcher)
    await expect(client.tree()).rejects.toThrow('bootstrapped')
    expect(fetcher).not.toHaveBeenCalled()
    await client.project()
    expect(fetcher.mock.calls[0]![0]).toBe('/api/projects/A/project')
    expect(a.scope).toEqual({ project_id: 'A', epoch: 'epoch-1' })
    fetcher.mockResolvedValueOnce(reply({ project_id: 'B', epoch: 'epoch-1' }))
    await expect(client.project()).rejects.toThrow('Conflicting')
    const unknown = createApiClient(new ProjectSession('unknown'))
    fetcher.mockResolvedValueOnce(new Response('{"detail":"Unknown or expired project id."}', { status: 404 }))
    await expect(unknown.project()).rejects.toMatchObject({ status: 404 })
    expect(fetcher.mock.calls.at(-1)![0]).toBe('/api/projects/unknown/project')
  })

  it('A/B JSON, uploads, raw/download and streams stay bound across a legacy switch', async () => {
    const a = createApiClient(session('A')), b = createApiClient(session('B'))
    const legacy = createApiClient(new ProjectSession())
    let finishA!: (response: Response) => void
    const fetcher = vi.fn((url: string) => {
      if (url.includes('/A/docs/shared.md')) return new Promise<Response>(resolve => { finishA = resolve })
      if (url.includes('/folders/rename')) return Promise.resolve(new Response('{"type":"done","path":"new"}\n'))
      if (url.includes('/explorer/raw')) return Promise.resolve(new Response(new Uint8Array([1, 2])))
      return Promise.resolve(reply({ content: 'B', name: 'file.png' }))
    })
    vi.stubGlobal('fetch', fetcher)
    const delayed = a.doc('shared.md')
    await legacy.openProject('B', false)
    await expect(b.doc('shared.md')).resolves.toMatchObject({ content: 'B' })
    finishA(reply({ content: 'A' }))
    await expect(delayed).resolves.toMatchObject({ content: 'A' })
    await a.uploadDocAsset('shared.md', new File(['a'], 'file.png'))
    const upload = fetcher.mock.calls.at(-1)!
    expect(upload[0]).toContain('/api/projects/A/docs/asset?')
    const init = vi.mocked(fetch).mock.calls.at(-1)![1]!
    expect(init.body).toBeInstanceOf(FormData)
    expect(init.headers).not.toHaveProperty('Content-Type')
    expect(a.rawFileUrl('same.vox')).toContain('/api/projects/A/explorer/raw?')
    expect(b.docAssetUrl('shared.md', 'file.png')).toContain('/api/projects/B/docs/asset?')
    expect(a.attachmentUrl('same/file.png')).toContain('/api/projects/A/attachments/same/file.png?')
    expect(a.taskAttachmentUrl('t', 'same', 'file.png')).toContain('/api/projects/A/teams/t/tasks/same/attachments/file.png?')
    expect(a.rawFileUrl('same.vox')).toContain('token=shared')
    expect(new Uint8Array(await a.rawFileBytes('same.vox'))).toEqual(new Uint8Array([1, 2]))
    const frames: unknown[] = []
    await a.renameDocFolder('old', 'new', frame => frames.push(frame))
    expect(frames).toHaveLength(1)
    expect(fetcher.mock.calls.at(-1)![0]).toContain('/api/projects/A/docs/folders/rename?')
    await a.updateApplicationSettings({ language: 'ru' })
    expect(fetcher.mock.calls.at(-2)![0]).toBe('/api/settings/application')
    expect(fetcher.mock.calls.at(-1)![0]).toContain('/api/projects/A/settings?')
    expect(vi.mocked(fetch).mock.calls.at(-2)![1]!.headers).toMatchObject({ Authorization: 'Bearer shared' })
  })

  it('global preference writes do not return the legacy selection project configuration', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(reply({ language: 'ru', project_name: 'B', theme: { accent: '#bbbbbb' } }))
      .mockResolvedValueOnce(reply({ language: 'ru', project_name: 'A', theme: { accent: '#aaaaaa' } })))
    const config = await createApiClient(session('A')).updateApplicationSettings({ language: 'ru' })
    expect(config).toMatchObject({ language: 'ru', project_name: 'A', theme: { accent: '#aaaaaa' } })
  })

  it('ensures a project globally without rebinding the current renderer or implicit creation', async () => {
    const a = session('A')
    vi.stubGlobal('fetch', vi.fn(async () => reply({ project_id: 'B', epoch: 'epoch-1', root: 'B' })))
    await expect(createApiClient(a).ensureProject('B')).resolves.toMatchObject({ project_id: 'B' })
    const [url, init] = vi.mocked(fetch).mock.calls[0]!
    expect(url).toBe('/api/projects/open')
    expect(JSON.parse(String(init?.body))).toEqual({ path: 'B', create: false })
    expect(a.scope?.project_id).toBe('A')
  })

  it('ignores raw body and stream read failures that arrive after invalidation', async () => {
    const rawScope = session('A'), client = createApiClient(rawScope)
    const response = new Response()
    response.arrayBuffer = vi.fn(async () => { rawScope.invalidate(); throw new Error('late read error') })
    vi.stubGlobal('fetch', vi.fn(async () => response))
    await expect(client.rawFileBytes('same.vox')).rejects.toBeInstanceOf(StaleProjectResponseError)

    const scope = session('A')
    let fail!: () => void
    const body = new ReadableStream<Uint8Array>({ start(controller) { fail = () => {
      scope.invalidate(); controller.error(new Error('late stream error'))
    } } })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
    const pending = createApiClient(scope).renameDocFolder('old', 'new', () => {})
    await Promise.resolve(); await Promise.resolve()
    fail()
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)
  })

  it('rejects delayed bodies and stops streamed callbacks after disposal', async () => {
    const scope = session('A'), client = createApiClient(scope)
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const pending = client.doc('shared.md')
    scope.invalidate()
    finish(reply({ content: 'late' }))
    await expect(pending).rejects.toBeInstanceOf(StaleProjectResponseError)

    const streamScope = session('A'), streamClient = createApiClient(streamScope)
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new TextEncoder().encode('{"type":"document"}\n{"type":"done"}\n'))
      controller.close()
    } })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
    const frames: unknown[] = []
    await expect(streamClient.renameDocFolder('old', 'new', frame => {
      frames.push(frame); streamScope.invalidate()
    })).rejects.toBeInstanceOf(StaleProjectResponseError)
    expect(frames).toHaveLength(1)
  })

  it('keeps named, absolute and plugin navigation inside each renderer history base', async () => {
    const original = window.location.href
    const histories: Array<{ destroy(): void }> = []
    try {
      const routers = []
      for (const id of ['A', 'B']) {
        window.history.replaceState({}, '', `/p/${id}/documents`)
        vi.resetModules()
        const { router } = await import('@/router')
        histories.push(router.options.history)
        routers.push(router)
        router.addRoute({ path: '/plugins/example', name: 'example', component: { render: () => null } })
        expect(router.options.history.base).toBe(`/p/${id}`)
        expect(router.resolve({ name: 'docs' }).href).toBe(`/p/${id}/documents`)
        expect(router.resolve('/settings').href).toBe(`/p/${id}/settings`)
        expect(router.resolve({ name: 'example' }).href).toBe(`/p/${id}/plugins/example`)
        await router.push('/p/foreign/documents')
        expect(router.currentRoute.value.path).toBe('/')
      }
      expect(routers[0]!.resolve('/tasks').href).toBe('/p/A/tasks')
      expect(routers[1]!.resolve('/tasks').href).toBe('/p/B/tasks')
    } finally {
      histories.forEach(history => history.destroy())
      window.history.replaceState({}, '', original)
      vi.resetModules()
    }
  })
})
