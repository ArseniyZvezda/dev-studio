/** Project-bound overview recovery after incomplete WS history (dev spec §5.3.2). */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AutomationInput, AutomationRun, AutomationsOverview } from '@/api/types'
import type { ProjectSession as Session } from '@/session/project-session'
import type { EventEnvelope, EventPayloads, EventType } from '@/ws/events'

type Listener = (payload: never, envelope: EventEnvelope) => void | Promise<void>
const mocks = vi.hoisted(() => ({
  api: {
    automations: vi.fn(), createAutomation: vi.fn(), updateAutomation: vi.fn(),
    setAutomationEnabled: vi.fn(), deleteAutomation: vi.fn(), clearAutomationRuns: vi.fn(),
    runAutomation: vi.fn(),
  },
  session: null as Session | null,
  listeners: new Map<string, Listener[]>(),
  onEvent: vi.fn(),
}))
vi.mock('@/api/client', () => ({ api: mocks.api }))
vi.mock('@/session/project-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/session/project-session')>()
  return {
    ...actual,
    // Exercise real scope/capture rules while selecting a fresh session for every test.
    rendererSession: {
      get isLegacy() { return mocks.session!.isLegacy },
      matches: (scope: Parameters<Session['matches']>[0]) => mocks.session!.matches(scope),
      capture: () => mocks.session!.capture(),
    },
  }
})
vi.mock('@/ws/connection', () => ({ onEvent: mocks.onEvent }))

import { ProjectSession } from '@/session/project-session'
import { AUTOMATIONS_REFRESH_DEBOUNCE_MS, MAX_RUNS, useAutomationsStore } from './automations'

const scope = { project_id: 'project-a', epoch: 'epoch-a' }
const input: AutomationInput = {
  name: 'Notify', enabled: true, trigger: { kind: 'task.created' },
  action: { kind: 'notify', title: '', message: '' },
}

function run(id: string, status: AutomationRun['status'] = 'running', second = 0): AutomationRun {
  return {
    id, automation_id: 'notify', automation_name: 'Notify', status,
    cause: 'manual', detail: status === 'running' ? '' : `finished: ${status}`,
    data: { trigger: 'manual', action: 'notify' },
    created_at: `2026-11-13T10:00:${String(second).padStart(2, '0')}Z`,
  }
}

function page(runs: AutomationRun[] = [], name = 'Notify'): AutomationsOverview {
  return {
    path: '.voxarium_studio/automations.yaml', exists: true, error: null,
    automations: [{ ...input, id: 'notify', name, problem: null }],
    invalid: [], plugin_actions: [], runs,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function emit<T extends EventType>(
  type: T, data: EventPayloads[T], eventScope: Partial<typeof scope> = scope,
): Promise<void> {
  const handlers = mocks.listeners.get(type) ?? []
  expect(handlers, `subscribed handler for ${type}`).toHaveLength(1)
  const envelope = { type, data, seq: 10, ts: '2026-11-13T10:00:00Z', ...eventScope }
  return Promise.all(handlers.map((handler) => handler(data as never, envelope))).then(() => {})
}

function gap(eventScope: Partial<typeof scope> = scope, resync = true) {
  return emit('system.dropped', { count: 1, resync, reason: 'history_gap' }, eventScope)
}

function opened(...runs: AutomationRun[]) {
  const store = useAutomationsStore()
  store.adopt(page(runs))
  store.subscribe()
  return store
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mocks.listeners.clear()
  mocks.session = new ProjectSession(scope.project_id)
  mocks.session.bind(scope)
  mocks.onEvent.mockImplementation((type: string, handler: Listener) => {
    const handlers = mocks.listeners.get(type) ?? []
    handlers.push(handler)
    mocks.listeners.set(type, handlers)
    return () => mocks.listeners.set(type, handlers.filter((entry) => entry !== handler))
  })
  mocks.api.automations.mockImplementation(async () => page())
})

afterEach(() => vi.useRealTimers())

describe('project-bound gap recovery', () => {
  it.each(['ok', 'error', 'cancelled'] as const)(
    'repairs a missed terminal run_updated (%s) from REST without clearing busy', async (status) => {
      const store = opened(run('missed'))
      const action = deferred<unknown>()
      mocks.api.runAutomation.mockReturnValueOnce(action.promise)
      const runningAction = store.runNow('notify')
      mocks.api.automations.mockResolvedValueOnce(page([run('missed', status)], 'Refreshed'))

      await gap()

      expect(store.runs).toEqual([run('missed', status)])
      expect(store.lastRun.get('notify')?.status).toBe(status)
      expect(store.automations[0]?.name).toBe('Refreshed')
      expect(store.busy).toBe('notify')
      expect(mocks.api.automations).toHaveBeenCalledTimes(1)
      expect(store.loading).toBe(false)
      action.resolve(undefined)
      expect(await runningAction).toBe(true)
      expect(store.busy).toBeNull()
    },
  )

  it.each([
    ['foreign project', { project_id: 'project-b', epoch: scope.epoch }, true],
    ['foreign epoch', { project_id: scope.project_id, epoch: 'epoch-b' }, true],
    ['unscoped envelope', {}, true],
    ['missing epoch', { project_id: scope.project_id }, true],
    ['non-resync notice', scope, false],
  ] as const)('ignores %s', async (_name, envelopeScope, resync) => {
    const store = opened(run('unchanged'))
    await gap(envelopeScope, resync)
    expect(mocks.api.automations).not.toHaveBeenCalled()
    expect(store.runs).toEqual([run('unchanged')])
  })

  it('ignores legacy gaps even when the legacy session matches the envelope', async () => {
    mocks.session = new ProjectSession()
    opened(run('legacy'))
    expect(mocks.session.matches({})).toBe(true)
    await gap({})
    await gap(scope)
    expect(mocks.api.automations).not.toHaveBeenCalled()
  })

  it('subscribes once, including live rows and the gap listener', async () => {
    const store = opened()
    const subscriptions = mocks.onEvent.mock.calls.length
    store.subscribe()
    store.subscribe()
    expect(mocks.onEvent).toHaveBeenCalledTimes(subscriptions)
    for (const type of ['automation.fired', 'automation.run_updated', 'system.dropped']) {
      expect(mocks.listeners.get(type)).toHaveLength(1)
    }
    await gap()
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
  })

  it('coalesces gaps during each REST read into a serial follow-up and awaits the whole recovery', async () => {
    const store = opened(run('missed'))
    const first = deferred<AutomationsOverview>()
    const second = deferred<AutomationsOverview>()
    const third = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise)

    let settled = false
    const recovery = gap().then(() => { settled = true })
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    const moreGaps = Array.from({ length: 6 }, () => gap())
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
    first.resolve(page([run('missed')]))
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(2))
    expect(settled).toBe(false)
    const finalGap = gap()
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
    second.resolve(page([run('missed')]))
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(3))
    expect(settled).toBe(false)
    third.resolve(page([run('missed', 'ok')]))
    await Promise.all([recovery, ...moreGaps, finalGap])
    expect(store.runs[0]?.status).toBe('ok')
    expect(mocks.api.automations).toHaveBeenCalledTimes(3)
    expect(store.loading).toBe(false)
  })

  it('does not lose a gap when an ordinary load was already in flight', async () => {
    const store = opened(run('missed'))
    const oldRead = deferred<AutomationsOverview>()
    const recoveryRead = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(recoveryRead.promise)
    const loading = store.load()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    let recovered = false
    const recovery = gap().then(() => { recovered = true })
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
    oldRead.resolve(page([run('missed')]))
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(2))
    expect(recovered).toBe(false)
    recoveryRead.resolve(page([run('missed', 'ok')]))
    await Promise.all([loading, recovery])
    expect(store.runs[0]?.status).toBe('ok')
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
  })

  it('overlays concurrent fired/run_updated rows, but corrects untouched running rows from REST', async () => {
    const store = opened(run('untouched'), run('updated', 'running', 1))
    const read = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    await emit('automation.fired', run('new', 'running', 2))
    await emit('automation.run_updated', run('updated', 'error', 1))
    read.resolve(page([run('updated', 'running', 1), run('untouched', 'ok')], 'From REST'))
    await recovery

    expect(store.runs).toEqual([
      run('new', 'running', 2), run('updated', 'error', 1), run('untouched', 'ok'),
    ])
    expect(store.automations[0]?.name).toBe('From REST')
    expect(store.lastRun.get('notify')?.id).toBe('new')
  })

  it('keeps live events arriving before the first overview response', async () => {
    const store = useAutomationsStore()
    store.subscribe()
    const read = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
    const loading = store.load()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    await emit('automation.fired', run('new', 'running', 2))
    await emit('automation.run_updated', run('new', 'ok', 2))
    read.resolve(page([run('older', 'ok')]))
    await loading
    expect(store.runs).toEqual([run('new', 'ok', 2), run('older', 'ok')])
  })

  it('finishes recovery after a pending write, not before its stale overview can land', async () => {
    const store = opened(run('missed'))
    const write = deferred<AutomationsOverview>()
    mocks.api.updateAutomation.mockReturnValueOnce(write.promise)
    const saving = store.update('notify', { ...input, name: 'Saved' })
    await vi.waitFor(() => expect(mocks.api.updateAutomation).toHaveBeenCalledTimes(1))
    mocks.api.automations.mockResolvedValue(page([run('missed', 'ok')], 'Saved'))
    let recovered = false
    const recovery = gap().then(() => { recovered = true })
    await Promise.resolve()
    expect(recovered).toBe(false)
    expect(store.busy).toBe('notify')
    write.resolve(page([run('missed')], 'Saved'))
    expect(await saving).toBe(true)
    await recovery
    expect(store.runs[0]?.status).toBe('ok')
    expect(store.automations[0]?.name).toBe('Saved')
    expect(store.busy).toBeNull()
  })

  it('does not adopt an in-flight recovery after the renderer session was invalidated', async () => {
    const store = opened(run('current'))
    const read = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    mocks.session!.invalidate()
    read.resolve(page([run('must-not-land', 'ok')]))
    await recovery
    expect(store.runs).toEqual([run('current')])
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
    await gap()
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
  })
})

describe('overview concurrency and retry', () => {
  it.each(['read', 'write'] as const)('does not regress a terminal %s snapshot with queued WS progress', async (kind) => {
    const store = opened(run('queued'))
    const response = deferred<AutomationsOverview>()
    let request: Promise<unknown>
    if (kind === 'read') {
      mocks.api.automations.mockReturnValueOnce(response.promise)
      request = gap()
      await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    } else {
      mocks.api.updateAutomation.mockReturnValueOnce(response.promise)
      request = store.update('notify', input)
    }
    await emit('automation.run_updated', run('queued'))
    response.resolve(page([run('queued', 'ok')]))
    await request
    expect(store.runs[0]?.status).toBe('ok')
    await emit('automation.run_updated', run('queued'))
    expect(store.runs[0]?.status).toBe('ok')
  })

  it('journals immediate events before the first load microtask, even without an overview', async () => {
    const store = useAutomationsStore()
    store.subscribe()
    const loading = store.load()
    const terminal = emit('automation.run_updated', run('immediate', 'ok'))
    const progress = emit('automation.run_updated', run('immediate'))
    await Promise.all([loading, terminal, progress])
    expect(store.runs).toEqual([run('immediate', 'ok')])
  })

  it('starts a fresh recovery for a gap in the microtask immediately after a read resolves', async () => {
    const store = opened(run('missed'))
    const first = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(page([run('missed', 'ok')]))
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    let next: Promise<void> | undefined
    first.resolve(page([run('missed')]))
    queueMicrotask(() => { next = gap() })
    await recovery
    await next
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
    expect(store.runs[0]?.status).toBe('ok')
  })

  it('repeats an invalidated recovery after its GET fails and the concurrent write settles', async () => {
    const store = opened(run('missed'))
    const read = deferred<AutomationsOverview>()
    const write = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
      .mockResolvedValueOnce(page([run('missed', 'ok')], 'Saved'))
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    mocks.api.updateAutomation.mockReturnValueOnce(write.promise)
    const saving = store.update('notify', { ...input, name: 'Saved' })
    read.reject(new Error('obsolete failure'))
    await Promise.resolve()
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
    write.resolve(page([run('missed')], 'Saved'))
    await Promise.all([saving, recovery])
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
    expect(store.automations[0]?.name).toBe('Saved')
    expect(store.runs[0]?.status).toBe('ok')
    expect(store.error).toBeNull()
  })

  it('does not roll back a newer write when an older write finishes last', async () => {
    const store = opened()
    const old = deferred<AutomationsOverview>()
    const newer = deferred<AutomationsOverview>()
    mocks.api.updateAutomation.mockReturnValueOnce(old.promise).mockReturnValueOnce(newer.promise)
    const first = store.update('notify', { ...input, name: 'Old' })
    const second = store.update('notify', { ...input, name: 'New' })
    newer.resolve(page([], 'New'))
    expect(await second).toBe(true)
    old.resolve(page([], 'Old'))
    expect(await first).toBe(true)
    expect(store.automations[0]?.name).toBe('New')
  })

  it('replaces a scheduled debounce with gap recovery without a duplicate read', async () => {
    vi.useFakeTimers()
    opened()
    await emit('automation.changed', {})
    await gap()
    await vi.advanceTimersByTimeAsync(AUTOMATIONS_REFRESH_DEBOUNCE_MS)
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
  })

  it('retains ordering and the log limit while merging concurrent firings', async () => {
    const store = opened()
    const read = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    for (let i = 0; i < MAX_RUNS + 2; i++) await emit('automation.fired', run(`live-${i}`, 'ok', i))
    read.resolve(page([run('old')]))
    await recovery
    expect(store.runs).toHaveLength(MAX_RUNS)
    expect(store.runs.map((entry) => entry.id)).toEqual(
      Array.from({ length: MAX_RUNS }, (_, i) => `live-${MAX_RUNS + 1 - i}`),
    )
  })

  it('deduplicates ordinary loads without turning them into gap follow-ups', async () => {
    const store = opened()
    const read = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(read.promise)
    const first = store.load()
    const second = store.load()
    expect(store.loading).toBe(true)
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    read.resolve(page([run('loaded', 'ok')]))
    await Promise.all([first, second])
    expect(store.runs).toEqual([run('loaded', 'ok')])
    expect(mocks.api.automations).toHaveBeenCalledTimes(1)
    expect(store.loading).toBe(false)
  })

  it.each(['adopt', 'write'] as const)('does not roll back a successful newer %s with an older recovery GET', async (kind) => {
    const store = opened()
    const stale = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(stale.promise)
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    const newer = page([run('newer', 'ok')], 'Saved name')
    // A serial post-write recovery read is also valid; the server now holds the write.
    mocks.api.automations.mockResolvedValue(newer)
    if (kind === 'adopt') store.adopt(newer)
    else {
      mocks.api.updateAutomation.mockResolvedValueOnce(newer)
      expect(await store.update('notify', { ...input, name: 'Saved name' })).toBe(true)
    }
    stale.resolve(page([run('old')], 'Old name'))
    await recovery
    expect(store.automations[0]?.name).toBe('Saved name')
    expect(store.runs).toEqual([run('newer', 'ok')])
    expect(store.error).toBeNull()
  })

  it('does not lose concurrent live rows when a delayed write response lands', async () => {
    const store = opened(run('existing'))
    const write = deferred<AutomationsOverview>()
    mocks.api.updateAutomation.mockReturnValueOnce(write.promise)
    const saving = store.update('notify', { ...input, name: 'Saved' })
    await emit('automation.fired', run('new', 'running', 2))
    await emit('automation.run_updated', run('existing', 'ok'))
    write.resolve(page([run('existing')], 'Saved'))
    expect(await saving).toBe(true)
    expect(store.automations[0]?.name).toBe('Saved')
    expect(store.runs).toEqual([run('new', 'running', 2), run('existing', 'ok')])
    expect(store.busy).toBeNull()
  })

  it('recovers after a rejected write without clearing its live rows or leaving busy stuck', async () => {
    const store = opened(run('existing'))
    const write = deferred<AutomationsOverview>()
    mocks.api.updateAutomation.mockReturnValueOnce(write.promise)
    const saving = store.update('notify', input)
    await emit('automation.fired', run('new', 'ok', 2))
    write.reject(new Error('save refused'))
    expect(await saving).toBe(false)
    expect(store.error).toBe('save refused')
    expect(store.busy).toBeNull()
    expect(store.runs).toEqual([run('new', 'ok', 2), run('existing')])
    mocks.api.automations.mockResolvedValueOnce(page([run('new', 'ok', 2), run('existing', 'ok')]))
    await gap()
    expect(store.runs).toEqual([run('new', 'ok', 2), run('existing', 'ok')])
    expect(store.error).toBeNull()
  })

  it('allows a new load after adopt even while an older load is pending', async () => {
    const store = opened()
    const stale = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(stale.promise)
    const before = store.load()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    store.adopt(page([], 'Saved'))
    mocks.api.automations.mockResolvedValueOnce(page([], 'Latest'))
    const after = store.load()
    stale.resolve(page([], 'Old'))
    await Promise.all([before, after])
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
    expect(store.automations[0]?.name).toBe('Latest')
  })

  it('keeps existing data on recovery failure and permits a later gap to retry', async () => {
    const store = opened(run('missed'))
    mocks.api.automations.mockRejectedValueOnce(new Error('offline'))
    await gap()
    expect(store.error).toBe('offline')
    expect(store.runs).toEqual([run('missed')])
    expect(store.loading).toBe(false)
    mocks.api.automations.mockResolvedValueOnce(page([run('missed', 'ok')]))
    await gap()
    expect(store.runs[0]?.status).toBe('ok')
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
  })

  it('honours a queued gap even if the read in flight fails', async () => {
    const store = opened(run('missed'))
    const failed = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(failed.promise)
      .mockResolvedValueOnce(page([run('missed', 'ok')]))
    const recovery = gap()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    const queued = gap()
    failed.reject(new Error('temporary failure'))
    await Promise.all([recovery, queued])
    expect(mocks.api.automations).toHaveBeenCalledTimes(2)
    expect(store.runs[0]?.status).toBe('ok')
    expect(store.error).toBeNull()
  })

  it('does not let an old read error replace a newer successful adoption', async () => {
    const store = opened()
    const stale = deferred<AutomationsOverview>()
    mocks.api.automations.mockReturnValueOnce(stale.promise)
    const loading = store.load()
    await vi.waitFor(() => expect(mocks.api.automations).toHaveBeenCalledTimes(1))
    store.adopt(page([], 'Saved'))
    mocks.api.automations.mockResolvedValue(page([], 'Saved'))
    stale.reject(new Error('stale read failure'))
    await loading
    expect(store.automations[0]?.name).toBe('Saved')
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
  })
})
