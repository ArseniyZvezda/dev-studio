/**
 * The version control store (spec §9.6).
 *
 * Four things are under test, and none is HTTP. The first is the lookup the Explorer
 * calls once per visible row — exact for a file, ancestor-aware for a directory — because
 * getting that wrong colours the wrong rows and nothing in a screenshot says so. The
 * second is the vocabulary those rows are drawn with (§6.2): now that the tree is the
 * only surface showing a file's state, a kind with no mark is a file with no decoration.
 * The third is how often the backend is asked: a pipeline run writing assets emits file
 * changes in bursts, and each refresh is a status subprocess, so a burst must coalesce
 * into one read and two reads must never run at once. The fourth is scoped WS gap
 * recovery (§5.3.2): server metadata and marks must catch up without reseeding forms.
 *
 * The API is a stub. Pinia needs no DOM, which is what lets this run in the same node
 * environment as the rest of the suite.
 */

import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import type { VcsChangeKind, VcsFileChange, VcsInfo, VcsOverview, VcsStatus } from '@/api/types'
import type { ProjectSession as ProjectSessionType, ProjectScope } from '@/session/project-session'
import type { EventEnvelope, EventPayloads, EventType } from '@/ws/events'

const session = vi.hoisted(() => ({ current: null as ProjectSessionType | null }))
vi.mock('@/session/project-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/session/project-session')>()
  return { ...actual, get rendererSession() { return session.current ?? actual.rendererSession } }
})

const api = {
  vcs: vi.fn(),
  vcsStatus: vi.fn(),
  updateVcs: vi.fn(),
  vcsCommit: vi.fn(),
  vcsDiff: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

/** One socket, owned by the app; the store only subscribes to it. */
type EventHandler = (payload: EventPayloads[EventType], envelope: EventEnvelope) => void
const listeners = new Map<string, EventHandler>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: EventHandler) => {
    listeners.set(type, handler)
    return () => listeners.delete(type)
  }),
}))

const { onEvent } = await import('@/ws/connection')
const { ProjectSession } = await import('@/session/project-session')
const { useVcsStore, VCS_MARKS, REFRESH_DEBOUNCE_MS, vcsIndicator } = await import('./vcs')

const PROJECT_SCOPE: ProjectScope = { project_id: 'project_a', epoch: 'epoch_a' }

function emit<T extends EventType>(type: T, data: EventPayloads[T], scope: Partial<ProjectScope> = PROJECT_SCOPE): void {
  listeners.get(type)?.(data, { type, data, seq: 42, ts: '2026-11-13T00:00:00Z', ...scope })
}

function gap(scope: Partial<ProjectScope> = PROJECT_SCOPE, resync = true): void {
  emit('system.dropped', { count: 5, resync, reason: 'history_gap' }, scope)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((land, fail) => { resolve = land; reject = fail })
  return { promise, resolve, reject }
}

function change(path: string, kind: VcsChangeKind = 'modified', staged = false): VcsFileChange {
  return { path, kind, staged, origin: null }
}

function status(changes: VcsFileChange[], backend: string | null = 'git'): VcsStatus {
  return {
    backend,
    root: 'D:/games/mygame',
    branch: backend ? 'main' : null,
    revision: backend ? 'c3fd2b1' : null,
    clean: changes.length === 0,
    changes,
  }
}

function overview(): VcsOverview {
  return {
    active: {
      id: 'git',
      name: 'Git',
      root: 'D:/games/mygame',
      available: true,
      reason: '',
      supports_isolation: true,
    },
    configured: 'auto',
    backends: [{
      id: 'git', name: 'Git', root: 'D:/games/mygame', available: true, reason: '',
      supports_isolation: true, program: 'git', executable: 'git',
      settings: { executable: 'git' }, settings_fields: [{ key: 'executable', type: 'path' }],
      detected: true, active: true,
    }],
    branch: 'main',
    revision: 'c3fd2b1',
  }
}

/** What the Studio's own watcher emits while a pipeline writes assets. */
function fileChanged(...paths: string[]): void {
  emit('explorer.file_changed', {
    changes: paths.map((path) => ({ change: 'modified', path, is_dir: false })),
    truncated: false,
  })
}

let pinia: Pinia
beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  session.current = new ProjectSession(PROJECT_SCOPE.project_id)
  session.current.bind(PROJECT_SCOPE)
  listeners.clear()
  vi.mocked(onEvent).mockClear()
  for (const stub of Object.values(api)) stub.mockReset()
})

afterEach(() => {
  disposePinia(pinia)
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('what the Explorer asks per row', () => {
  it('matches a file exactly, and nothing around it', async () => {
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(
      status([
        change('Content/Props/rock.uasset', 'modified'),
        change('Content/Props/tree.uasset', 'untracked'),
        change('README.md', 'added', true),
      ]),
    )
    await store.refresh()

    expect(store.kindFor('Content/Props/rock.uasset')).toBe('modified')
    expect(store.kindFor('Content/Props/tree.uasset')).toBe('untracked')
    expect(store.kindFor('README.md')).toBe('added')

    // The directory holding a change did not itself change…
    expect(store.kindFor('Content/Props')).toBeNull()
    expect(store.kindFor('Content')).toBeNull()
    // …and neither did a sibling, or a path that merely starts the same way.
    expect(store.kindFor('Content/Props/rock.uasset.bak')).toBeNull()
    expect(store.kindFor('Content/Props/stone.uasset')).toBeNull()
  })

  it('lets a conflict outrank the same path reported staged as well', async () => {
    // A file can appear twice — staged, and changed again since. Whatever else it is, a
    // conflict is the one a user has to act on, so it is the one shown.
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(
      status([
        change('Config/DefaultEngine.ini', 'modified', true),
        change('Config/DefaultEngine.ini', 'conflicted'),
      ]),
    )
    await store.refresh()

    expect(store.kindFor('Config/DefaultEngine.ini')).toBe('conflicted')
  })

  it('reports a directory dirty however deep the change is', async () => {
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(status([change('Content/Props/Rocks/LOD0/rock.uasset')]))
    await store.refresh()

    expect(store.dirtyUnder('Content/Props/Rocks/LOD0')).toBe(true)
    expect(store.dirtyUnder('Content/Props/Rocks')).toBe(true)
    expect(store.dirtyUnder('Content/Props')).toBe(true)
    expect(store.dirtyUnder('Content')).toBe(true)
    // The project root, which is how the tree's top row is coloured.
    expect(store.dirtyUnder('')).toBe(true)

    expect(store.dirtyUnder('Source')).toBe(false)
    expect(store.dirtyUnder('Content/Maps')).toBe(false)
    // The changed file is a file: it has nothing under it.
    expect(store.dirtyUnder('Content/Props/Rocks/LOD0/rock.uasset')).toBe(false)
  })

  it('counts what a collapsed folder is hiding, each path once', async () => {
    // The count is what the folder's dot says in words. A path reported twice — staged,
    // and changed again since — is still one changed file, or a folder claims more work
    // than it holds.
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(
      status([
        change('Content/Props/rock.uasset', 'modified', true),
        change('Content/Props/rock.uasset', 'modified'),
        change('Content/Props/tree.uasset', 'untracked'),
        change('Content/Maps/arena.umap', 'added'),
        change('README.md', 'modified'),
      ]),
    )
    await store.refresh()

    expect(store.changesUnder('Content/Props')).toBe(2)
    expect(store.changesUnder('Content/Maps')).toBe(1)
    expect(store.changesUnder('Content')).toBe(3)
    expect(store.changesUnder('')).toBe(4)
    // A path with nothing under it, and one nobody touched.
    expect(store.changesUnder('Content/Props/rock.uasset')).toBe(0)
    expect(store.changesUnder('Source')).toBe(0)
    // And the same number the Commit section shows, which counts entries, not paths.
    expect(store.changeCount).toBe(5)
  })

  it('leaves every directory clean when nothing changed', async () => {
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(status([]))
    await store.refresh()

    expect(store.dirtyUnder('')).toBe(false)
    expect(store.changesUnder('')).toBe(0)
    expect(store.dirtyUnder('Content')).toBe(false)
    expect(store.kindFor('README.md')).toBeNull()
  })
})

describe('how a changed row is drawn', () => {
  const KINDS: VcsChangeKind[] = [
    'added',
    'modified',
    'deleted',
    'renamed',
    'copied',
    'untracked',
    'conflicted',
    'unknown',
  ]

  it('has a mark for every kind the backend can send', () => {
    // `CHANGE_KINDS` in `vcs/base.py` is a closed set and anything outside it arrives as
    // `unknown`, so this table is total by construction — a kind added on one side and
    // not the other is a file the tree draws as `undefined`.
    expect(Object.keys(VCS_MARKS).sort()).toEqual([...KINDS].sort())
  })

  it('says the same thing without colour', () => {
    // §6.2: colour is never the only signal. Every mark carries a letter and a word, and
    // no two adjacent meanings share a letter.
    const letters = KINDS.map((kind) => VCS_MARKS[kind].letter)
    expect(new Set(letters).size).toBe(KINDS.length)
    for (const kind of KINDS) {
      expect(VCS_MARKS[kind].letter).not.toBe('')
      expect(VCS_MARKS[kind].label).not.toBe('')
    }
  })

  it('keeps §6.2 colours: green appears, amber moves, red is lost, a conflict shouts', () => {
    // Utilities, never a literal colour (§7) — these names are the semantic tokens, so
    // both themes follow from one place.
    expect(VCS_MARKS.added.class).toBe('text-success')
    expect(VCS_MARKS.untracked.class).toBe('text-success')
    expect(VCS_MARKS.copied.class).toBe('text-success')
    expect(VCS_MARKS.modified.class).toBe('text-warning')
    expect(VCS_MARKS.renamed.class).toBe('text-warning')
    expect(VCS_MARKS.deleted.class).toBe('text-danger')
    expect(VCS_MARKS.conflicted.class).toContain('text-danger')
    expect(VCS_MARKS.conflicted.class).toContain('font-semibold')
    // Not a guessed colour for a state the backend could not name.
    expect(VCS_MARKS.unknown.class).toBe('text-muted')
  })

  it('draws a file through the same lookup the tree uses', async () => {
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(
      status([change('Content/Props/rock.uasset', 'deleted', true), change('LICENSE', 'untracked')]),
    )
    await store.refresh()

    const kind = store.kindFor('Content/Props/rock.uasset')
    expect(kind && VCS_MARKS[kind].letter).toBe('D')
    expect(store.kindFor('LICENSE')).toBe('untracked')
    // An unchanged file has no kind, and therefore no decoration at all.
    expect(store.kindFor('Content/Props/tree.uasset')).toBeNull()
  })
})

describe('what the rail says about version control', () => {
  const plastic: VcsInfo = {
    id: 'plastic',
    name: 'Plastic SCM (UVC)',
    root: 'D:/Games/SampleGame',
    available: true,
    reason: '',
    supports_isolation: false,
  }

  it('is green and names the system when its tool answers', () => {
    const line = vcsIndicator(plastic, { branch: '/main', revision: 'cs:21' })
    expect(line.color).toBe('var(--vxs-success)')
    expect(line.label).toBe(plastic.name)
    // The tooltip carries what the rail has no room for: where the workspace sits.
    expect(line.title).toContain('/main · cs:21')
    expect(line.title).toContain('D:/Games/SampleGame')
  })

  it('is red, and says why, when a backend is chosen but its tool is not usable', () => {
    const line = vcsIndicator({ ...plastic, available: false, reason: 'cm was not found on PATH' })
    expect(line.color).toBe('var(--vxs-danger)')
    expect(line.label).toBe(plastic.name)
    expect(line.title).toContain('cm was not found on PATH')
  })

  it('is grey and says "none" under nothing recognised — a state, not a fault', () => {
    for (const info of [null, undefined]) {
      const line = vcsIndicator(info)
      expect(line.color).toBe('var(--vxs-text-faint)')
      expect(line.label).toBe('none')
      expect(line.title).toContain('Version Control page')
    }
  })

  it('speaks only in tokens, never a literal colour (§7)', () => {
    const lines = [vcsIndicator(plastic), vcsIndicator({ ...plastic, available: false }), vcsIndicator(null)]
    for (const line of lines) expect(line.color).toMatch(/^var\(--vxs-/)
  })
})

describe('reading the working copy', () => {
  it('adopts the full overview on an explicit load, including changed settings', async () => {
    const store = useVcsStore()
    api.vcs.mockResolvedValueOnce(overview())
    api.vcsStatus.mockResolvedValue(status([]))
    await store.load()
    const firstOverview = store.overview
    const next: VcsOverview = {
      ...overview(), configured: 'git', branch: 'another-branch',
      backends: [{ ...overview().backends[0]!, executable: 'custom-git', settings: { executable: 'custom-git' } }],
    }
    api.vcs.mockResolvedValueOnce(next)

    await store.load()

    expect(api.vcs).toHaveBeenCalledTimes(2)
    expect(api.vcsStatus).toHaveBeenCalledTimes(2)
    expect(store.overview).not.toBe(firstOverview)
    expect(store.overview).toEqual(next)
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('takes "under nothing recognised" as an answer, not an error', async () => {
    // `GET /api/vcs/status` answers 200 with `backend: null` for a project that is not a
    // repository at all — the common case for a new project, and not a failure to report.
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(status([], null))
    await store.refresh()

    expect(store.error).toBeNull()
    expect(store.backendId).toBeNull()
    expect(store.changeCount).toBe(0)
    // Nothing to decorate the tree with, and nothing that reads as a failure: the
    // Explorer draws its rows exactly as it would for a clean repository.
    expect(store.kindFor('README.md')).toBeNull()
    expect(store.dirtyUnder('')).toBe(false)
    expect(store.changesUnder('')).toBe(0)
  })

  it('keeps the last known status when a read fails', async () => {
    const store = useVcsStore()
    api.vcsStatus.mockResolvedValue(status([change('README.md')]))
    await store.refresh()

    api.vcsStatus.mockRejectedValue(new ApiError(500, 'git is not on PATH'))
    await store.refresh()

    expect(store.error).toContain('git is not on PATH')
    // Still coloured from the last good answer: losing every colour because one poll
    // failed is worse than a picture that is a few seconds old.
    expect(store.kindFor('README.md')).toBe('modified')
    expect(store.loading).toBe(false)
  })

  it('never runs two reads at once', async () => {
    const store = useVcsStore()
    let land: (value: VcsStatus) => void = () => {}
    api.vcsStatus.mockReturnValue(new Promise<VcsStatus>((resolve) => (land = resolve)))

    const first = store.refresh()
    const second = store.refresh()
    // The second caller joined the read already running. (It cannot be asserted by
    // identity: Pinia wraps every action, so each call gets its own outer promise.)
    expect(api.vcsStatus).toHaveBeenCalledTimes(1)

    land(status([change('README.md')]))
    await Promise.all([first, second])

    expect(api.vcsStatus).toHaveBeenCalledTimes(1)
    expect(store.kindFor('README.md')).toBe('modified')
  })
})

describe('the debounce over file-change events', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('coalesces a burst of writes into one read', async () => {
    // A pipeline run generating assets emits these in bursts. Each read is a status
    // subprocess, so one per event would put the backend under a fork storm.
    const store = useVcsStore()
    store.subscribe()
    api.vcsStatus.mockResolvedValue(status([change('Content/Generated/rock.uasset', 'untracked')]))

    fileChanged('Content/Generated/rock.uasset')
    await vi.advanceTimersByTimeAsync(150)
    fileChanged('Content/Generated/tree.uasset')
    await vi.advanceTimersByTimeAsync(150)
    fileChanged('Content/Generated/bush.uasset', 'Content/Generated/hut.uasset')
    await vi.advanceTimersByTimeAsync(399)
    expect(api.vcsStatus, 'still within the quiet period').not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(api.vcsStatus).toHaveBeenCalledTimes(1)
    expect(store.kindFor('Content/Generated/rock.uasset')).toBe('untracked')
  })

  it('waits for a read in flight rather than starting a second beside it', async () => {
    const store = useVcsStore()
    store.subscribe()

    let land: (value: VcsStatus) => void = () => {}
    api.vcsStatus.mockReturnValueOnce(new Promise<VcsStatus>((resolve) => (land = resolve)))
    const opening = store.refresh()

    fileChanged('Content/Generated/rock.uasset')
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    expect(api.vcsStatus, 'the first read has not answered yet').toHaveBeenCalledTimes(1)

    api.vcsStatus.mockResolvedValue(status([change('Content/Generated/rock.uasset', 'untracked')]))
    land(status([]))
    await opening
    await vi.advanceTimersByTimeAsync(1)

    // …and the events are not simply dropped: they get their own read, after the first.
    expect(api.vcsStatus).toHaveBeenCalledTimes(2)
    expect(store.kindFor('Content/Generated/rock.uasset')).toBe('untracked')
  })

  it('subscribes once however many callers ask', async () => {
    // The page and the Explorer's tree both call it, and the tree calls it on every mount.
    const store = useVcsStore()
    store.subscribe()
    store.subscribe()
    store.subscribe()

    expect(vi.mocked(onEvent).mock.calls.map(([type]) => type).sort())
      .toEqual(['explorer.file_changed', 'system.dropped'])

    api.vcsStatus.mockResolvedValue(status([]))
    fileChanged('README.md')
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    expect(api.vcsStatus).toHaveBeenCalledTimes(1)
  })
})

describe('recovering the renderer after a scoped WS gap (dev spec §5.3.2)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('rereads server metadata and restores Explorer marks in the same store without reseeding forms', async () => {
    const store = useVcsStore()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([change('Content/Old/asset.uasset')]))
    store.subscribe()
    await store.load()

    const originalOverview = store.overview
    // Match VcsView's local state, seed() and non-deep overview watcher. Cards derive
    // from server backends, but changing them does not itself seed the local drafts.
    const choice = ref('auto')
    const settings = reactive<Record<string, string>>({})
    const seedForm = vi.fn(() => {
      const data = store.overview
      if (!data) return
      choice.value = data.configured
      for (const backend of data.backends) {
        for (const field of backend.settings_fields) {
          const key = `${backend.id}.${field.key}`
          if (!(key in settings)) settings[key] = backend.settings[field.key] ?? ''
        }
      }
    })
    const stopForm = watch(() => store.overview, () => seedForm(), { immediate: true })
    expect(choice.value).toBe('auto')
    expect(settings['git.executable']).toBe('git')
    choice.value = 'plastic'
    settings['git.executable'] = 'D:/draft/git.exe'
    const recovered: VcsOverview = {
      ...overview(), configured: 'git', branch: 'recovered', revision: 'new-revision',
      backends: [{
        ...overview().backends[0]!, executable: 'D:/server/git.exe',
        settings: { executable: 'D:/server/git.exe' },
      }],
      active: { ...overview().active!, available: false, reason: 'git temporarily missing', supports_isolation: false },
    }
    const nextStatus = { ...status([change('Content/New/asset.uasset', 'untracked')]), branch: 'recovered', revision: 'new-revision' }
    api.vcs.mockResolvedValue(recovered)
    api.vcsStatus.mockResolvedValue(nextStatus)

    gap()
    await vi.runAllTimersAsync()
    await nextTick()

    expect(api.vcs).toHaveBeenCalledTimes(2)
    expect(api.vcsStatus).toHaveBeenCalledTimes(2)
    expect(useVcsStore()).toBe(store)
    expect(store.overview).toBe(originalOverview)
    expect(store.overview?.configured).toBe('git')
    expect(store.overview?.backends).toEqual(recovered.backends)
    expect(store.overview?.backends[0]?.settings.executable).toBe('D:/server/git.exe')
    expect(store.overview?.active).toEqual(recovered.active)
    expect(store.overview?.branch).toBe('recovered')
    expect(store.overview?.revision).toBe('new-revision')
    expect(seedForm).toHaveBeenCalledTimes(1)
    expect(choice.value).toBe('plastic')
    expect(settings['git.executable']).toBe('D:/draft/git.exe')
    expect(store.status).toEqual(nextStatus)
    expect(store.kindFor('Content/Old/asset.uasset')).toBeNull()
    expect(store.dirtyUnder('Content/Old')).toBe(false)
    expect(store.kindFor('Content/New/asset.uasset')).toBe('untracked')
    expect(store.changesUnder('Content/New')).toBe(1)
    expect(store.changesUnder('')).toBe(1)
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
    expect(api.updateVcs).not.toHaveBeenCalled()
    stopForm()
  })

  it.each([
    ['another project', { ...PROJECT_SCOPE, project_id: 'project_b' }, true],
    ['another epoch', { ...PROJECT_SCOPE, epoch: 'epoch_old' }, true],
    ['legacy envelope', {}, true],
    ['missing epoch', { project_id: PROJECT_SCOPE.project_id }, true],
    ['missing project', { epoch: PROJECT_SCOPE.epoch }, true],
    ['non-resync drop', PROJECT_SCOPE, false],
  ] as const)('ignores %s', async (_name, scope, resync) => {
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([]))

    gap(scope, resync)
    await vi.runAllTimersAsync()

    expect(api.vcs).not.toHaveBeenCalled()
    expect(api.vcsStatus).not.toHaveBeenCalled()
  })

  it.each(['legacy', 'unbound', 'invalidated'] as const)('does not recover a %s renderer', async (kind) => {
    if (kind === 'legacy') session.current = new ProjectSession()
    if (kind === 'unbound') session.current = new ProjectSession(PROJECT_SCOPE.project_id)
    if (kind === 'invalidated') session.current!.invalidate()
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([]))

    gap(kind === 'legacy' ? {} : PROJECT_SCOPE)
    gap()
    await vi.runAllTimersAsync()

    expect(api.vcs).not.toHaveBeenCalled()
    expect(api.vcsStatus).not.toHaveBeenCalled()
  })

  it('coalesces repeated gaps and file events into one recovery', async () => {
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([change('Content/new.uasset', 'added')]))

    gap()
    fileChanged('Content/new.uasset')
    gap()
    gap()
    await vi.runAllTimersAsync()

    expect(api.vcs).toHaveBeenCalledTimes(1)
    expect(api.vcsStatus).toHaveBeenCalledTimes(1)
    expect(store.kindFor('Content/new.uasset')).toBe('added')
  })

  it.each(['refresh', 'load'] as const)('queues one fresh read after status already running in %s', async (action) => {
    const store = useVcsStore()
    store.subscribe()
    const oldStatus = deferred<VcsStatus>()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockReturnValueOnce(oldStatus.promise)
      .mockResolvedValue(status([change('Content/after-gap.uasset', 'untracked')]))
    const opening = store[action]()
    await vi.advanceTimersByTimeAsync(0)
    expect(api.vcsStatus).toHaveBeenCalledTimes(1)

    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    fileChanged('Content/after-gap.uasset')
    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    expect(api.vcsStatus, 'no parallel status subprocess while the old one is running').toHaveBeenCalledTimes(1)

    oldStatus.resolve(status([]))
    await opening
    await vi.runAllTimersAsync()

    expect(api.vcsStatus).toHaveBeenCalledTimes(2)
    expect(api.vcs).toHaveBeenCalledTimes(action === 'load' ? 2 : 1)
    expect(store.kindFor('Content/after-gap.uasset')).toBe('untracked')
    expect(store.loading).toBe(false)
  })

  it('does not finish with a pre-gap overview when load was still reading it', async () => {
    const store = useVcsStore()
    store.subscribe()
    const oldOverview = deferred<VcsOverview>()
    const freshOverview = { ...overview(), branch: 'after-gap', revision: 'after-gap-revision' }
    api.vcs.mockReturnValueOnce(oldOverview.promise).mockResolvedValue(freshOverview)
    api.vcsStatus.mockResolvedValue(status([change('Content/new.uasset')]))
    const opening = store.load()

    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    oldOverview.resolve(overview())
    await opening
    await vi.runAllTimersAsync()

    expect(api.vcs).toHaveBeenCalledTimes(2)
    expect(store.overview?.branch).toBe('after-gap')
    expect(store.overview?.revision).toBe('after-gap-revision')
    expect(store.kindFor('Content/new.uasset')).toBe('modified')
    expect(store.loading).toBe(false)
  })

  it('coalesces gaps arriving during recovery into one subsequent recovery', async () => {
    const store = useVcsStore()
    store.subscribe()
    const firstStatus = deferred<VcsStatus>()
    const firstOverview = deferred<VcsOverview>()
    api.vcs.mockReturnValueOnce(firstOverview.promise)
      .mockResolvedValue({ ...overview(), branch: 'latest' })
    api.vcsStatus.mockReturnValueOnce(firstStatus.promise)
      .mockResolvedValue(status([change('Content/latest.uasset')]))

    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    // An implementation can sequence overview before status, but neither may fan out.
    expect(api.vcs).toHaveBeenCalledTimes(1)
    expect(api.vcsStatus.mock.calls.length).toBeLessThanOrEqual(1)

    firstOverview.resolve(overview())
    firstStatus.resolve(status([]))
    await vi.runAllTimersAsync()

    expect(api.vcs).toHaveBeenCalledTimes(2)
    expect(api.vcsStatus).toHaveBeenCalledTimes(2)
    expect(store.overview?.branch).toBe('latest')
    expect(store.kindFor('Content/latest.uasset')).toBe('modified')
  })

  it.each(['overview', 'status'] as const)('keeps a settings save that overtakes the recovery %s response', async (pendingRead) => {
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([change('Content/known.uasset')]))
    await store.load()

    const stale = deferred<VcsOverview | VcsStatus>()
    const fresh = deferred<VcsOverview | VcsStatus>()
    const saved: VcsOverview = {
      ...overview(), configured: 'git', branch: 'saved-branch', revision: 'saved-revision',
      backends: [{
        ...overview().backends[0]!, executable: 'D:/saved/git.exe',
        settings: { executable: 'D:/saved/git.exe' },
      }],
    }
    const latestStatus = {
      ...status([change('Content/latest.uasset', 'added')]), branch: 'saved-branch', revision: 'saved-revision',
    }
    api.vcs.mockResolvedValue(saved)
    api.vcsStatus.mockResolvedValue(latestStatus)
    const readApi = pendingRead === 'overview' ? api.vcs : api.vcsStatus
    readApi.mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise)

    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    expect(readApi).toHaveBeenCalledTimes(2)

    api.updateVcs.mockResolvedValue(saved)
    await store.updateSettings({ settings: { git: { executable: 'D:/saved/git.exe' } } })
    const savedOverview = store.overview
    expect(store.overview).toEqual(saved)
    expect(store.saving).toBe(false)

    stale.resolve(pendingRead === 'overview'
      ? { ...overview(), configured: 'none', branch: 'stale-branch' }
      : { ...status([change('Content/stale.uasset', 'deleted')]), branch: 'stale-branch' })
    await vi.advanceTimersByTimeAsync(0)

    // Hold the new read so it cannot hide a transient overwrite by the old payload.
    expect(readApi).toHaveBeenCalledTimes(3)
    expect(store.overview).toBe(savedOverview)
    expect(store.overview).toEqual(saved)
    expect(store.kindFor('Content/stale.uasset')).toBeNull()

    fresh.resolve(pendingRead === 'overview' ? saved : latestStatus)
    await vi.runAllTimersAsync()

    expect(readApi).toHaveBeenCalledTimes(3)
    expect(store.overview).toEqual(saved)
    expect(store.status).toEqual(latestStatus)
    expect(store.kindFor('Content/known.uasset')).toBeNull()
    expect(store.kindFor('Content/latest.uasset')).toBe('added')
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
  })

  it.each(['overview', 'status'] as const)('does not apply a recovery %s response after session invalidation', async (pendingRead) => {
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([change('Content/known.uasset')]))
    await store.load()
    const previousOverview = store.overview
    const previousStatus = store.status
    const pending = deferred<VcsOverview | VcsStatus>()
    const readApi = pendingRead === 'overview' ? api.vcs : api.vcsStatus
    readApi.mockReturnValueOnce(pending.promise)

    gap()
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS)
    expect(readApi).toHaveBeenCalledTimes(2)
    session.current!.invalidate()
    pending.resolve(pendingRead === 'overview'
      ? { ...overview(), configured: 'none', backends: [], active: null, branch: 'stale-branch' }
      : status([change('Content/stale.uasset', 'deleted')]))
    await vi.runAllTimersAsync()

    expect(store.overview).toBe(previousOverview)
    expect(store.overview).toEqual(overview())
    expect(store.status).toBe(previousStatus)
    expect(store.kindFor('Content/known.uasset')).toBe('modified')
    expect(store.kindFor('Content/stale.uasset')).toBeNull()
    expect(store.loading).toBe(false)

    gap()
    await vi.runAllTimersAsync()
    expect(readApi).toHaveBeenCalledTimes(2)
  })

  it.each(['overview', 'status'] as const)('can recover after a failed %s read without losing the last good marks', async (failed) => {
    const store = useVcsStore()
    store.subscribe()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([change('Content/known.uasset')]))
    await store.load()
    const originalOverview = store.overview

    if (failed === 'overview') api.vcs.mockRejectedValueOnce(new ApiError(500, 'overview temporarily unavailable'))
    else api.vcsStatus.mockRejectedValueOnce(new ApiError(500, 'status temporarily unavailable'))
    gap()
    await vi.runAllTimersAsync()

    expect(store.error).toContain(`${failed} temporarily unavailable`)
    expect(store.loading).toBe(false)
    expect(store.kindFor('Content/known.uasset')).toBe('modified')
    expect(store.overview).toBe(originalOverview)

    api.vcs.mockResolvedValue({ ...overview(), branch: 'recovered' })
    api.vcsStatus.mockResolvedValue(status([change('Content/recovered.uasset', 'added')]))
    gap()
    await vi.runAllTimersAsync()

    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
    expect(store.overview?.branch).toBe('recovered')
    expect(store.kindFor('Content/known.uasset')).toBeNull()
    expect(store.kindFor('Content/recovered.uasset')).toBe('added')
  })
})

describe('writing the settings back', () => {
  it('adopts the merged answer without a second read', async () => {
    // `PUT /api/vcs/settings` merges and answers with the same shape as `GET /api/vcs`.
    const store = useVcsStore()
    const merged: VcsOverview = {
      ...overview(), configured: 'git',
      backends: [{ ...overview().backends[0]!, executable: 'custom-git', settings: { executable: 'custom-git' } }],
    }
    api.updateVcs.mockResolvedValue(merged)

    const result = await store.updateSettings({ backend: 'git' })

    expect(api.updateVcs).toHaveBeenCalledWith({ backend: 'git' })
    expect(result).toEqual(merged)
    expect(store.overview).toEqual(merged)
    expect(store.overview?.configured).toBe('git')
    expect(api.vcs).not.toHaveBeenCalled()
    expect(api.vcsStatus).not.toHaveBeenCalled()
    expect(store.saving).toBe(false)
  })

  it('rethrows a refused write so the form can keep what was typed', async () => {
    const store = useVcsStore()
    api.vcs.mockResolvedValue(overview())
    api.vcsStatus.mockResolvedValue(status([]))
    await store.load()

    api.updateVcs.mockRejectedValue(new ApiError(400, "'cm' is not on PATH"))
    await expect(
      store.updateSettings({ settings: { plastic: { executable: 'cm' } } }),
    ).rejects.toThrow("'cm' is not on PATH")

    // The stored overview is untouched — the page shows the user's draft over it.
    expect(store.overview?.configured).toBe('auto')
    expect(store.saving).toBe(false)
  })
})
