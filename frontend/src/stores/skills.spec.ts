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

const listeners = new Map<string, (payload: unknown) => void>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: (payload: unknown) => void) => {
    listeners.set(type, handler)
    return () => listeners.delete(type)
  }),
}))

const { useSkillsStore, SKILLS_REFRESH_DEBOUNCE_MS } = await import('./skills')

function page(...names: string[]): SkillsOverview {
  return {
    path: 'voxarium_studio/skills',
    absolute_path: 'D:/game/voxarium_studio/skills',
    skills: names.map((name) => ({
      name,
      path: `voxarium_studio/skills/${name}`,
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
  listeners.clear()
  api.skills.mockReset()
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
    expect(store.byName.get('tester')?.path).toBe('voxarium_studio/skills/tester')
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

    changed('voxarium_studio/skills/tester/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).not.toHaveBeenCalled()

    await store.load()
    api.skills.mockClear()

    changed('Assets/hero.png', 'voxarium_studio/skills-old/x/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).not.toHaveBeenCalled()

    changed('voxarium_studio/skills/tester/scripts/a.py')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS / 2)
    changed('voxarium_studio/skills/tester/scripts/b.py', 'voxarium_studio/skills/tester/SKILL.md')
    await vi.advanceTimersByTimeAsync(SKILLS_REFRESH_DEBOUNCE_MS * 2)
    expect(api.skills).toHaveBeenCalledTimes(1)
  })
})
