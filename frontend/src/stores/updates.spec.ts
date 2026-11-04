/**
 * The update store (§12.1).
 *
 * What is worth pinning here is the store's judgement, not HTTP: what the button says, when
 * it is offered at all, and what happens to a refusal. That last one is why the store exists
 * rather than the dialog calling the API itself — a 409 is the Studio holding something back,
 * and the message has to survive long enough to be read and overruled.
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { UpdateStatus } from '@/api/types'

const api = {
  updates: vi.fn(),
  checkForUpdates: vi.fn(),
  applyUpdate: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

// One socket, owned by the app; the store only subscribes. The handlers are kept so a check
// can play an event the way the socket would — `mock` prefix because `vi.mock` is hoisted.
const mockWsHandlers = new Map<string, (payload: never) => void>()
vi.mock('@/ws/connection', () => ({
  onEvent: (name: string, handler: (payload: never) => void) => {
    mockWsHandlers.set(name, handler)
  },
}))

const { useUpdatesStore } = await import('./updates')

function status(patch: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    supported: true,
    reason: null,
    state: 'idle',
    enabled: true,
    interval_minutes: 180,
    current: 'v1.0',
    latest: 'v1.0',
    available: false,
    checked_at: '2026-09-08T10:00:00',
    branch: 'main',
    upstream: 'origin/main',
    dirty: false,
    step: null,
    error: null,
    log: [],
    ...patch,
  }
}

beforeEach(() => {
  vi.unstubAllGlobals()
  setActivePinia(createPinia())
  mockWsHandlers.clear()
  for (const fn of Object.values(api)) fn.mockReset()
})

describe('packaged desktop updates', () => {
  it('uses the desktop bridge and ignores backend git status', async () => {
    const data = { ...status(), downloaded: false, progress: 0 }
    let changed: (value: typeof data) => void = () => {}
    const bridge = {
      getState: vi.fn().mockResolvedValue(data),
      check: vi.fn().mockResolvedValue({ ...data, latest: 'v2.0', available: true }),
      download: vi.fn().mockResolvedValue({ ...data, available: true, downloaded: true, progress: 100 }),
      install: vi.fn().mockResolvedValue({ ...data, state: 'restarting', downloaded: true }),
      onStateChange: vi.fn((callback) => { changed = callback }),
    }
    vi.stubGlobal('window', { voxariumUpdates: bridge })
    const store = useUpdatesStore()
    await store.load()
    expect(store.desktop).toBe(true)
    expect(api.updates).not.toHaveBeenCalled()
    await store.startFromButton()
    expect(store.dialogOpen).toBe(true)
    expect(bridge.check).toHaveBeenCalledOnce()
    expect(bridge.download).not.toHaveBeenCalled()
    changed({ ...data, state: 'updating', progress: 42 })
    expect(store.progress).toBe(42)
    await store.apply()
    expect(store.downloaded).toBe(true)
    await store.startFromButton()
    expect(bridge.download).toHaveBeenCalledOnce()
    await store.installDesktop()
    expect(store.state).toBe('restarting')
    expect(api.applyUpdate).not.toHaveBeenCalled()
  })

  it('keeps the git updater when Electron runs from source', async () => {
    vi.stubGlobal('window', { voxariumUpdates: { getState: vi.fn().mockResolvedValue(null) } })
    api.updates.mockResolvedValue(status())
    const store = useUpdatesStore()
    await store.load()
    expect(store.desktop).toBe(false)
    expect(api.updates).toHaveBeenCalledOnce()
  })
})

describe('what the rail is told', () => {
  it('says nothing while the Studio is on the newest release', async () => {
    api.updates.mockResolvedValue(status())
    const store = useUpdatesStore()
    await store.load()

    expect(store.available).toBe(false)
    expect(store.running).toBe(false)
  })

  it('names the release in the button, and nothing else', async () => {
    api.updates.mockResolvedValue(status({ latest: 'v1.1', available: true }))
    const store = useUpdatesStore()
    await store.load()

    expect(store.available).toBe(true)
    expect(store.label).toBe('Update → v1.1')
  })

  it('stays quiet when the backend cannot answer at all', async () => {
    api.updates.mockRejectedValue(new ApiError(0, 'Network error'))
    const store = useUpdatesStore()
    await store.load()

    expect(store.status).toBeNull()
    expect(store.available).toBe(false)
  })
})

describe('a refusal', () => {
  it('is kept for the dialog to show beside the choice it offers', async () => {
    api.updates.mockResolvedValue(status({ latest: 'v1.1', available: true, dirty: true }))
    api.applyUpdate.mockRejectedValue(
      new ApiError(409, "The Studio's checkout has uncommitted changes."),
    )
    const store = useUpdatesStore()
    await store.load()
    await store.apply(false)

    expect(store.refusal).toContain('uncommitted changes')
    expect(api.applyUpdate).toHaveBeenCalledWith(false)
  })

  it('is cleared by the retry that overrules it', async () => {
    api.applyUpdate.mockRejectedValueOnce(new ApiError(409, 'still running'))
    api.applyUpdate.mockResolvedValueOnce(status({ state: 'updating' }))
    const store = useUpdatesStore()

    await store.apply(false)
    expect(store.refusal).toBe('still running')

    await store.apply(true)
    expect(store.refusal).toBeNull()
    expect(store.state).toBe('updating')
    expect(api.applyUpdate).toHaveBeenLastCalledWith(true)
  })
})

describe('while an update runs', () => {
  it('follows the backend rather than guessing at the sequence', async () => {
    api.updates.mockResolvedValue(status())
    const store = useUpdatesStore()
    await store.load()

    mockWsHandlers.get('update.status')?.(
      status({ state: 'updating', step: 'git pull' }) as never,
    )
    expect(store.running).toBe(true)
    expect(store.status?.step).toBe('git pull')
  })

  it('appends the transcript line by line as the commands print it', async () => {
    api.updates.mockResolvedValue(status())
    const store = useUpdatesStore()
    await store.load()

    const log = mockWsHandlers.get('update.log')
    log?.({ line: '$ git pull --ff-only', step: 'git pull' } as never)
    log?.({ line: 'Updating 8b74257..a1b2c3d', step: 'git pull' } as never)

    expect(store.log).toEqual(['$ git pull --ff-only', 'Updating 8b74257..a1b2c3d'])
  })

  it('adopts the backend transcript only when it holds more than this client saw', async () => {
    // A client that arrives mid-update has nothing; one that was here all along has every
    // line already, and replacing its list with a truncated tail would lose the beginning.
    api.updates.mockResolvedValue(status())
    const store = useUpdatesStore()
    await store.load()

    mockWsHandlers.get('update.log')?.({ line: 'one', step: null } as never)
    mockWsHandlers.get('update.log')?.({ line: 'two', step: null } as never)
    mockWsHandlers.get('update.status')?.(status({ state: 'updating', log: ['two'] }) as never)

    expect(store.log).toEqual(['one', 'two'])
  })
})
