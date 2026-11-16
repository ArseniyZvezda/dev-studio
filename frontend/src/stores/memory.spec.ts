/** Server inventories survive scoped WS gaps without owning the Memory page's editor (§5.3.2). */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MemoryOverview } from '@/api/types'
import type { EventEnvelope, EventPayloads, EventType } from '@/ws/events'

const mocks = vi.hoisted(() => ({
  api: { memory: vi.fn(), memoryEntry: vi.fn(), saveMemoryEntry: vi.fn() },
  session: null as import('@/session/project-session').ProjectSession | null,
  handlers: new Map<string, (payload: any, envelope: any) => void | Promise<void>>(),
  onEvent: vi.fn(),
}))
vi.mock('@/api/client', () => ({ api: mocks.api }))
vi.mock('@/session/project-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/session/project-session')>()
  return {
    ...actual,
    rendererSession: {
      get isLegacy() { return mocks.session!.isLegacy },
      matches: (envelope: EventEnvelope) => mocks.session!.matches(envelope),
    },
  }
})
vi.mock('@/ws/connection', () => ({ onEvent: mocks.onEvent }))

import { ProjectSession } from '@/session/project-session'
import { MEMORY_REFRESH_DEBOUNCE_MS, useMemoryStore } from './memory'

const scope = { project_id: 'project-a', epoch: 'epoch-1' }
function emit<T extends EventType>(type: T, data: EventPayloads[T], binding: object = scope) {
  return mocks.handlers.get(type)?.(data, { type, data, ...binding })
}
function gap(binding: object = scope, resync = true) {
  return emit('system.dropped', { count: 1, resync }, binding)
}
function changed(...paths: string[]) {
  return emit('explorer.file_changed', {
    changes: paths.map((path) => ({ change: 'modified', path, is_dir: false })),
    truncated: false,
  })
}
function page(...names: string[]): MemoryOverview {
  return {
    path: '.voxarium_studio/memory', enabled: true, index_chars: 10000,
    index: { text: names.join('\n'), chars: names.join('\n').length, total: names.length, shown: names.length },
    entries: names.map((name) => ({
      name, description: name, author: null, author_member: null,
      updated_at: '2026-11-13T10:00:00Z', chars: 10, warning: null,
    })),
    invalid: [],
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function names() { return useMemoryStore().overview?.entries.map((entry) => entry.name) }

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mocks.handlers.clear()
  mocks.session = new ProjectSession(scope.project_id)
  mocks.session.bind(scope)
  mocks.onEvent.mockImplementation((type, handler) => {
    mocks.handlers.set(type, handler)
    return () => mocks.handlers.delete(type)
  })
})
afterEach(() => { vi.useRealTimers() })

describe('memory inventory reads', () => {
  it('loads lazily and shares a current in-flight request', async () => {
    const store = useMemoryStore()
    expect(store.overview).toBeNull()
    const answer = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(answer.promise)
    const first = store.load()
    const second = store.load()
    expect(store.loading).toBe(true)
    answer.resolve(page('fact'))
    await Promise.all([first, second])
    expect(mocks.api.memory).toHaveBeenCalledTimes(1)
    expect(names()).toEqual(['fact'])
    expect(store.loading).toBe(false)
  })

  it('drops an answer overtaken by a local write and refreshes after entry-only saves', async () => {
    const store = useMemoryStore()
    const old = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page('saved'))
    const initial = store.load()
    const refresh = store.refresh()
    await refresh
    old.resolve(page('old'))
    await initial
    expect(names()).toEqual(['saved'])
  })

  it('keeps ordinary memory and watcher updates debounced and lazy', async () => {
    vi.useFakeTimers()
    const store = useMemoryStore()
    store.subscribe()
    mocks.api.memory.mockResolvedValue(page('fact'))
    emit('team.memory.changed', { name: 'fact', author: null })
    changed('.voxarium_studio/memory/fact.md')
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS * 2)
    expect(mocks.api.memory).not.toHaveBeenCalled()
    await store.load()
    mocks.api.memory.mockClear()
    changed('Assets/fact.md', '.voxarium_studio/memory-old/fact.md')
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS * 2)
    expect(mocks.api.memory).not.toHaveBeenCalled()
    emit('team.memory.changed', { name: 'fact', author: null })
    changed('.voxarium_studio/memory/fact.md')
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS * 2)
    expect(mocks.api.memory).toHaveBeenCalledTimes(1)
  })
})

describe('scoped gap recovery', () => {
  it('recovers missed changes in a loaded inventory, retaining rows while loading and not reading/writing editor details', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('fact', 'removed'))
    const answer = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(answer.promise)
    const recovery = gap()
    await Promise.resolve()
    expect(names()).toEqual(['fact', 'removed'])
    expect(store.loading).toBe(true)
    answer.resolve(page('fact', 'new'))
    await recovery
    expect(names()).toEqual(['fact', 'new'])
    expect(mocks.api.memoryEntry).not.toHaveBeenCalled()
    expect(mocks.api.saveMemoryEntry).not.toHaveBeenCalled()
    expect(store.loading).toBe(false)
  })

  it('does not fetch untouched inventories or react to legacy/foreign/stale epoch/non-resync envelopes', async () => {
    const store = useMemoryStore()
    store.subscribe()
    await gap()
    store.adopt(page('fact'))
    await gap(scope, false)
    await gap({ ...scope, project_id: 'project-b' })
    await gap({ ...scope, epoch: 'epoch-0' })
    await gap({})
    mocks.session = new ProjectSession()
    await gap({})
    expect(mocks.api.memory).not.toHaveBeenCalled()
  })

  it('ignores gaps after the renderer binding has been invalidated', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('fact'))
    mocks.session!.invalidate()
    await gap()
    expect(mocks.api.memory).not.toHaveBeenCalled()
  })

  it('subscribes only once and coalesces simultaneous gap notices', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.subscribe()
    expect(mocks.onEvent.mock.calls.map(([type]) => type).sort())
      .toEqual(['explorer.file_changed', 'system.dropped', 'team.memory.changed'])
    store.adopt(page('old'))
    mocks.api.memory.mockResolvedValue(page('new'))
    await Promise.all([gap(), gap(), gap()])
    expect(mocks.api.memory).toHaveBeenCalledTimes(1)
    expect(names()).toEqual(['new'])
  })

  it('starts a post-gap read rather than joining the initial request, dropping its later response', async () => {
    const store = useMemoryStore()
    store.subscribe()
    const old = deferred<MemoryOverview>()
    const fresh = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const initial = store.load()
    const recovery = gap()
    await Promise.resolve()
    expect(mocks.api.memory).toHaveBeenCalledTimes(2)
    fresh.resolve(page('fresh'))
    await recovery
    old.resolve(page('old'))
    await initial
    expect(names()).toEqual(['fresh'])
  })

  it('invalidates a recovery read immediately and re-reads for a gap arriving during it', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('visible'))
    const first = deferred<MemoryOverview>()
    const second = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const recovery = gap()
    await Promise.resolve()
    const repeated = gap()
    first.resolve(page('outdated'))
    await vi.waitFor(() => expect(mocks.api.memory).toHaveBeenCalledTimes(2))
    expect(names()).toEqual(['visible'])
    second.resolve(page('latest'))
    await Promise.all([recovery, repeated])
    expect(names()).toEqual(['latest'])
  })

  it.each(['adopt', 'refresh'] as const)('does not roll back a newer local %s, including an older queued recovery', async (write) => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('old'))
    const answer = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(answer.promise).mockResolvedValueOnce(page('local-write'))
    const recovery = gap()
    await Promise.resolve()
    gap()
    if (write === 'adopt') store.adopt(page('local-write'))
    else await store.refresh()
    answer.resolve(page('stale'))
    await recovery
    expect(names()).toEqual(['local-write'])
    expect(mocks.api.memory).toHaveBeenCalledTimes(write === 'adopt' ? 1 : 2)
  })

  it('lets a local write supersede a gap before recovery starts', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('old'))
    const recovery = gap()
    store.adopt(page('local-write'))
    await recovery
    expect(names()).toEqual(['local-write'])
    expect(mocks.api.memory).not.toHaveBeenCalled()
  })

  it('does not let stale recovery errors overwrite successful local writes', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('old'))
    const answer = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(answer.promise)
    const recovery = gap()
    await Promise.resolve()
    store.adopt(page('local-write'))
    answer.reject(new Error('obsolete failure'))
    await recovery
    expect(names()).toEqual(['local-write'])
    expect(store.error).toBeNull()
  })

  it('retains newer entry-only save refreshes when recovery resolves last', async () => {
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('old'))
    const answer = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(answer.promise).mockResolvedValueOnce(page('saved'))
    const recovery = gap()
    await Promise.resolve()
    await store.refresh()
    answer.resolve(page('stale'))
    await recovery
    expect(names()).toEqual(['saved'])
  })

  it('absorbs a queued watcher refresh and can recover again after a failed request', async () => {
    vi.useFakeTimers()
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('old'))
    changed('.voxarium_studio/memory/fact.md')
    mocks.api.memory.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page('recovered'))
    await gap()
    expect(store.error).toBe('offline')
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS * 2)
    expect(mocks.api.memory).toHaveBeenCalledTimes(1)
    await gap()
    expect(names()).toEqual(['recovered'])
    expect(store.error).toBeNull()
  })

  it.each(['watcher', 'team.memory.changed'] as const)('does not lose %s changes before the initial recovery answers', async (event) => {
    vi.useFakeTimers()
    const store = useMemoryStore()
    store.subscribe()
    const before = deferred<MemoryOverview>()
    const stale = deferred<MemoryOverview>()
    mocks.api.memory.mockReturnValueOnce(before.promise).mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page('final'))
    const initial = store.load()
    const recovery = gap()
    await Promise.resolve()
    if (event === 'watcher') changed('.voxarium_studio/memory/final.md')
    else emit('team.memory.changed', { name: 'final', author: null })
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS)
    stale.resolve(page('snapshot-before-change'))
    before.resolve(page('initial-stale'))
    await Promise.all([initial, recovery])
    expect(mocks.api.memory).toHaveBeenCalledTimes(3)
    expect(names()).toEqual(['final'])
  })

  it('does not let a queued watcher read supersede a newer local write', async () => {
    vi.useFakeTimers()
    const store = useMemoryStore()
    store.subscribe()
    store.adopt(page('original'))
    changed('.voxarium_studio/memory/original.md')
    store.adopt(page('local'))
    await vi.advanceTimersByTimeAsync(MEMORY_REFRESH_DEBOUNCE_MS)
    expect(mocks.api.memory).not.toHaveBeenCalled()
    expect(names()).toEqual(['local'])
  })
})
