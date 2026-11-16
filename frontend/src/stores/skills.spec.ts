/**
 * The skills store (§10.10).
 *
 * What is under test is when the folder is read, not how it is drawn: callers share a request,
 * an answer a write has overtaken is dropped rather than put back on screen, and the file
 * watcher's reports cost one re-read per burst — and none at all for a change outside the
 * folder, or before anything has asked for the folder.
 *
 * The API and the socket are stubs; Pinia needs no DOM.
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SkillsOverview } from '@/api/types'
import { ProjectSession } from '@/session/project-session'

const session = vi.hoisted(() => ({ current: null as ProjectSession | null }))
vi.mock('@/session/project-session', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/session/project-session')>(),
  get rendererSession() { return session.current },
}))

const api = {
  skills: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

type Scope = { project_id?: string; epoch?: string }
type Listener = (payload: unknown, envelope?: Scope) => void | Promise<void>
const listeners = new Map<string, Listener>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: Listener) => {
    listeners.set(type, handler)
    return () => listeners.delete(type)
  }),
}))

const { useSkillsStore, SKILLS_REFRESH_DEBOUNCE_MS } = await import('./skills')
const { onEvent } = await import('@/ws/connection')

function gap(scope: Scope = { project_id: 'project-a', epoch: 'epoch-a' }, resync = true) {
  return listeners.get('system.dropped')?.({ count: 1, resync }, scope)
}

function page(...names: string[]): SkillsOverview {
  return {
    path: '.voxarium_studio/skills',
    absolute_path: 'D:/game/.voxarium_studio/skills',
    skills: names.map((name) => ({
      name,
      path: `.voxarium_studio/skills/${name}`,
      description: '',
      warning: null,
      files: [],
      file_count: 0,
      file_count_capped: false,
      updated_at: '2026-09-16T10:00:00+00:00',
      used_by: [],
    })),
    invalid: [],
    missing: [],
  }
}

function changed(...paths: string[]) {
  listeners.get('explorer.file_changed')?.({
    changes: paths.map((path) => ({ change: 'modified', path, is_dir: false })),
    truncated: false,
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => (resolve = done))
  return { promise, resolve }
}

beforeEach(() => {
  setActivePinia(createPinia())
  session.current = new ProjectSession('project-a')
  session.current.bind({ project_id: 'project-a', epoch: 'epoch-a' })
  listeners.clear()
  api.skills.mockReset()
  vi.mocked(onEvent).mockClear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('reading the folder', () => {
  it('refreshes package skills when plugins change', async () => {
    const store = useSkillsStore()
    store.subscribe()
    api.skills.mockResolvedValue(page('graphify', 'voxarium_studio:studio'))
    await store.load()
    api.skills.mockResolvedValue(page('voxarium_studio:studio'))
    listeners.get('plugin.changed')?.({})
    await vi.waitFor(() => expect(store.skills?.map((skill) => skill.name)).toEqual(['voxarium_studio:studio']))
    expect(api.skills).toHaveBeenCalledTimes(2)
  })

  it('says nothing about the folder until it has been read', async () => {
    const store = useSkillsStore()
    expect(store.skills).toBeNull()
    api.skills.mockResolvedValue(page('tester'))
    await store.load()
    expect(store.skills?.map((skill) => skill.name)).toEqual(['tester'])
    expect(store.byName.get('tester')?.path).toBe('.voxarium_studio/skills/tester')
  })

  it('lets callers share a request in flight', async () => {
    const store = useSkillsStore()
    const answer = deferred<SkillsOverview>()
    api.skills.mockReturnValue(answer.promise)
    const first = store.load()
    const second = store.load()
    answer.resolve(page('tester'))
    await Promise.all([first, second])
    expect(api.skills).toHaveBeenCalledTimes(1)
  })

  it('drops an answer a write has overtaken, and a caller after the write asks again', async () => {
    const store = useSkillsStore()
    const stale = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(stale.promise)
    const before = store.load()

    store.adopt(page('tester', 'writer'))
    api.skills.mockResolvedValueOnce(page('tester', 'writer', 'designer'))
    const after = store.load()

    stale.resolve(page())
    await Promise.all([before, after])
    expect(api.skills).toHaveBeenCalledTimes(2)
    expect(store.skills?.map((skill) => skill.name)).toEqual(['tester', 'writer', 'designer'])
  })
})

describe('following the file watcher', () => {
  it('re-reads once per burst inside the folder, and never for anything else', async () => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.subscribe()
    api.skills.mockResolvedValue(page('tester'))

    changed('.voxarium_studio/skills/tester/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).not.toHaveBeenCalled()

    await store.load()
    api.skills.mockClear()

    changed('Assets/hero.png', '.voxarium_studio/skills-old/x/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).not.toHaveBeenCalled()

    changed('.voxarium_studio/skills/tester/scripts/a.py')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS / 2)
    changed('.voxarium_studio/skills/tester/scripts/b.py', '.voxarium_studio/skills/tester/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).toHaveBeenCalledTimes(1)
  })
})

describe('project-bound gap recovery (§5.3.2)', () => {
  it('does not fetch an inventory nobody has requested', async () => {
    const store = useSkillsStore()
    store.subscribe()
    await gap()
    expect(api.skills).not.toHaveBeenCalled()
    expect(store.overview).toBeNull()
  })

  it('subscribes once and coalesces synchronous gaps without clearing the inventory', async () => {
    const store = useSkillsStore()
    store.subscribe()
    store.subscribe()
    expect(vi.mocked(onEvent).mock.calls.map(([type]) => type).sort()).toEqual([
      'explorer.file_changed', 'plugin.changed', 'system.dropped',
    ])
    store.adopt(page('open-editor'))
    const original = store.overview
    const answer = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(answer.promise)

    const first = gap()
    const second = gap()
    await Promise.resolve()
    expect(api.skills).toHaveBeenCalledTimes(1)
    expect(store.overview).toBe(original)
    expect(store.loading).toBe(true)
    answer.resolve(page('open-editor', 'missed-change'))
    await Promise.all([first, second])
    expect(store.skills?.map((skill) => skill.name)).toEqual(['open-editor', 'missed-change'])
    expect(store.loading).toBe(false)
  })

  it.each([
    ['foreign project', { project_id: 'project-b', epoch: 'epoch-a' }, true],
    ['foreign epoch', { project_id: 'project-a', epoch: 'epoch-b' }, true],
    ['unscoped envelope', {}, true],
    ['non-resync', { project_id: 'project-a', epoch: 'epoch-a' }, false],
  ])('ignores %s notices', async (_, scope, resync) => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('unchanged'))
    await gap(scope, resync)
    expect(api.skills).not.toHaveBeenCalled()
    expect(store.byName.has('unchanged')).toBe(true)
  })

  it('does not run scoped recovery in the legacy adapter', async () => {
    session.current = new ProjectSession()
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('unchanged'))
    await gap({}) // A legacy envelope matches a legacy session, but must not recover.
    await gap()
    expect(api.skills).not.toHaveBeenCalled()
  })

  it('replaces an initial read started before the gap, rejecting its late answer', async () => {
    const store = useSkillsStore()
    store.subscribe()
    const before = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(before.promise).mockResolvedValueOnce(page('recovered'))
    const initial = store.load()
    await gap()
    expect(api.skills).toHaveBeenCalledTimes(2)
    expect(store.byName.has('recovered')).toBe(true)
    before.resolve(page('stale'))
    await initial
    expect(store.byName.has('recovered')).toBe(true)
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('invalidates an ordinary refresh that was already reading when the gap arrived', async () => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    const before = deferred<SkillsOverview>()
    const after = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(before.promise).mockReturnValueOnce(after.promise)
    const refresh = store.refresh()
    const recovery = gap()
    await Promise.resolve()
    before.resolve(page('stale'))
    await refresh
    expect(store.byName.has('original')).toBe(true)
    expect(store.loading).toBe(true)
    after.resolve(page('recovered'))
    await recovery
    expect(store.byName.has('recovered')).toBe(true)
  })

  it('queues one final read for repeated gaps during recovery, dropping the overtaken answer', async () => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    const first = deferred<SkillsOverview>()
    const final = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(first.promise).mockReturnValueOnce(final.promise)
    const recovery = gap()
    await Promise.resolve()
    gap()
    gap()
    expect(api.skills).toHaveBeenCalledTimes(1)
    first.resolve(page('stale'))
    await vi.waitFor(() => expect(api.skills).toHaveBeenCalledTimes(2))
    expect(store.byName.has('original')).toBe(true)
    final.resolve(page('latest'))
    await recovery
    expect(store.byName.has('latest')).toBe(true)
    expect(store.loading).toBe(false)
  })

  it.each(['adopt', 'refresh'] as const)('does not roll back a newer local %s or run an older queued recovery', async (write) => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    const stale = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(stale.promise).mockResolvedValueOnce(page('local'))
    const recovery = gap()
    await Promise.resolve()
    gap() // This retry must not outlive the newer local write/refresh.
    if (write === 'adopt') store.adopt(page('local'))
    else await store.refresh()
    stale.resolve(page('stale'))
    await recovery
    expect(store.byName.has('local')).toBe(true)
    expect(api.skills).toHaveBeenCalledTimes(write === 'adopt' ? 1 : 2)
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('lets an immediate local adoption supersede a gap before its recovery starts', async () => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    const recovery = gap()
    store.adopt(page('local'))
    await recovery
    expect(api.skills).not.toHaveBeenCalled()
    expect(store.byName.has('local')).toBe(true)
  })

  it('does not surface an older read error after recovery succeeded', async () => {
    const store = useSkillsStore()
    store.subscribe()
    let reject!: (cause: Error) => void
    api.skills.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
      .mockResolvedValueOnce(page('recovered'))
    const initial = store.load()
    await gap()
    reject(new Error('obsolete failure'))
    await initial
    expect(store.error).toBeNull()
    expect(store.byName.has('recovered')).toBe(true)
  })

  it('retains the previous inventory on recovery failure and can recover on a later gap', async () => {
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    api.skills.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page('recovered'))
    await gap()
    expect(store.byName.has('original')).toBe(true)
    expect(store.error).toBe('offline')
    expect(store.loading).toBe(false)
    await gap()
    expect(store.byName.has('recovered')).toBe(true)
    expect(store.error).toBeNull()
  })

  it('absorbs an existing watcher debounce into the recovery read', async () => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    changed('.voxarium_studio/skills/original/SKILL.md')
    api.skills.mockResolvedValueOnce(page('recovered'))
    await gap()
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).toHaveBeenCalledTimes(1)
    expect(store.byName.has('recovered')).toBe(true)
  })

  it.each([false, true])('does not lose a watcher change during recovery (initial load: %s)', async (initial) => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.subscribe()
    const before = deferred<SkillsOverview>()
    const stale = deferred<SkillsOverview>()
    let loading: Promise<void> | undefined
    if (initial) {
      api.skills.mockReturnValueOnce(before.promise)
      loading = store.load()
    } else store.adopt(page('visible'))
    api.skills.mockReturnValueOnce(stale.promise).mockResolvedValueOnce(page('final'))
    const recovery = gap()
    await Promise.resolve()
    changed('.voxarium_studio/skills/final/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS)
    stale.resolve(page('snapshot-before-change'))
    before.resolve(page('initial-stale'))
    await Promise.all([loading, recovery])
    expect(api.skills).toHaveBeenCalledTimes(initial ? 3 : 2)
    expect(store.skills?.map((skill) => skill.name)).toEqual(['final'])
  })

  it('covers plugin changes before the initial inventory recovery has answered', async () => {
    const store = useSkillsStore()
    store.subscribe()
    const before = deferred<SkillsOverview>()
    const stale = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(before.promise).mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page('final-plugin'))
    const initial = store.load()
    const recovery = gap()
    await Promise.resolve()
    listeners.get('plugin.changed')?.({})
    stale.resolve(page('stale-plugin'))
    before.resolve(page('initial-stale'))
    await Promise.all([initial, recovery])
    expect(store.skills?.map((skill) => skill.name)).toEqual(['final-plugin'])
    expect(api.skills).toHaveBeenCalledTimes(3)
  })

  it('does not let a queued watcher read supersede a newer local write', async () => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.subscribe()
    store.adopt(page('original'))
    changed('.voxarium_studio/skills/original/SKILL.md')
    store.adopt(page('local'))
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS)
    expect(api.skills).not.toHaveBeenCalled()
    expect(store.byName.has('local')).toBe(true)
  })
})
