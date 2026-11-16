import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api, listeners, recoverRuntime, waitForVersion, reload, binding, disconnect, hydrateDocs } = vi.hoisted(() => ({
  api: { project: vi.fn(), projectTaskCounts: vi.fn(), status: vi.fn(), openProject: vi.fn(),
    sync: vi.fn(), updateSync: vi.fn() },
  listeners: new Map<string, (payload?: unknown, envelope?: unknown) => void>(),
  binding: { isLegacy: true, matches: vi.fn(), capture: vi.fn(), invalidate: vi.fn() },
  disconnect: vi.fn(), hydrateDocs: vi.fn(),
  recoverRuntime: vi.fn(), waitForVersion: vi.fn(), reload: vi.fn(),
}))

vi.mock('@/session/project-session', () => ({ rendererSession: binding }))
vi.mock('@/api/client', () => ({ api, recoverRuntime, ApiError: class extends Error {
  constructor(readonly status: number, detail: string) { super(detail) }
  get isAuthFailure() { return this.status === 401 }
} }))
vi.mock('@/lib/runtime-version', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/runtime-version')>(), waitForBackendVersion: waitForVersion,
}))
vi.mock('@/ws/connection', async () => {
  const { ref } = await import('vue')
  return {
    ws: { state: ref('open'), connect: vi.fn(), disconnect },
    onEvent: (event: string, callback: (payload?: unknown, envelope?: unknown) => void) => listeners.set(event, callback),
  }
})
vi.mock('./theme', () => ({ useThemeStore: () => ({ hydrate: vi.fn() }) }))
vi.mock('./docs', () => ({ useDocsStore: () => ({ hydrate: hydrateDocs, restoreFolds: vi.fn() }) }))

import { useSessionStore } from './session'
import { useSyncStore } from './sync'
import { ApiError } from '@/api/client'
import { FRONTEND_VERSION } from '@/lib/runtime-version'
import { locale, setLocale } from '@/i18n'

let pinia: Pinia
beforeEach(async () => {
  await setLocale('en')
  pinia = createPinia()
  setActivePinia(pinia)
  listeners.clear()
  vi.resetAllMocks()
  binding.isLegacy = true
  binding.matches.mockReturnValue(true)
  binding.capture.mockReturnValue(() => {})
  const storage = new Map<string, string>()
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value) })
  vi.stubGlobal('window', { location: { reload } })
  api.project.mockResolvedValue({ root: 'D:/game', config: {}, version: { version: FRONTEND_VERSION } })
  api.projectTaskCounts.mockResolvedValue({ in_progress: 2, needs_review: 1 })
})
afterEach(async () => { disposePinia(pinia); vi.unstubAllGlobals(); await setLocale('en') })

describe('application language', () => {
  it('applies the user config preference over the browser startup language', async () => {
    await setLocale('ru')
    api.project.mockResolvedValue({
      root: 'D:/game', config: { language: 'en' }, version: { version: FRONTEND_VERSION },
    })
    await useSessionStore().load()
    expect(locale.value).toBe('en')
  })

  it('keeps the startup language when no user preference has been saved', async () => {
    await setLocale('ru')
    await useSessionStore().load()
    expect(locale.value).toBe('ru')
  })
})

describe('immutable renderer project binding', () => {
  it('loads a scoped project and ignores legacy project.opened even for another root', async () => {
    binding.isLegacy = false
    api.project.mockResolvedValue({
      project_id: 'project-a', epoch: 'epoch-a', root: 'D:/game-a', config: {},
      version: { version: FRONTEND_VERSION },
    })
    const session = useSessionStore()
    await session.load()
    expect(binding.matches).toHaveBeenCalledWith(expect.objectContaining({ project_id: 'project-a' }))
    listeners.get('project.opened')!({ root: 'D:/game-b', project_id: 'project-b' })
    expect(session.project?.root).toBe('D:/game-a')
    expect(session.switching).toBe(false)
    expect(reload).not.toHaveBeenCalled()
    await expect(session.openProject('D:/game-b')).rejects.toThrow('cannot change its project binding')
    expect(api.openProject).not.toHaveBeenCalled()
    expect(binding.invalidate).not.toHaveBeenCalled()
    expect(disconnect).not.toHaveBeenCalled()
  })

  it('does not hydrate a foreign response or fall back after an invalid scoped handle', async () => {
    binding.isLegacy = false
    binding.matches.mockReturnValue(false)
    const session = useSessionStore()
    await session.load()
    expect(session.project).toBeNull()
    expect(session.error).toContain('renderer binding')
    api.project.mockRejectedValueOnce(new ApiError(404, 'Unknown project'))
    await session.load()
    expect(session.project).toBeNull()
    expect(session.error).toBe('Unknown project')
    expect(api.project).toHaveBeenCalledTimes(2)
    expect(api.openProject).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it('retires a legacy selection before reloading once for repeated project.opened', async () => {
    const session = useSessionStore()
    await session.load()
    listeners.get('project.opened')!({ root: 'D:/game' })
    expect(reload).not.toHaveBeenCalled()
    listeners.get('project.opened')!({ root: 'D:/game-b' })
    expect(reload).toHaveBeenCalledOnce()
    expect(binding.invalidate).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(binding.invalidate.mock.invocationCallOrder[0]).toBeLessThan(disconnect.mock.invocationCallOrder[0]!)
    expect(disconnect.mock.invocationCallOrder[0]).toBeLessThan(reload.mock.invocationCallOrder[0]!)
    listeners.get('project.opened')!({ root: 'D:/game-c' })
    expect(reload).toHaveBeenCalledOnce()
  })

  it('keeps legacy open behavior and retires the session before reload', async () => {
    const session = useSessionStore()
    await session.openProject('D:/game-c', false)
    expect(api.openProject).toHaveBeenCalledWith('D:/game-c', false)
    expect(reload).toHaveBeenCalledOnce()
    expect(binding.invalidate).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(binding.invalidate.mock.invocationCallOrder[0]).toBeLessThan(reload.mock.invocationCallOrder[0]!)
  })

  it.each(['resolve', 'reject'])('does not reload twice or undo a WS-triggered reload when open later %ss', async outcome => {
    let resolve!: () => void
    let reject!: (cause: Error) => void
    api.openProject.mockImplementationOnce(() => new Promise<void>((done, fail) => { resolve = done; reject = fail }))
    const session = useSessionStore()
    await session.load()
    const pending = session.openProject('D:/game-b')
    listeners.get('project.opened')!({ root: 'D:/game-b' })
    if (outcome === 'resolve') resolve()
    else reject(new Error('Stale project response'))
    await expect(pending).resolves.toBeUndefined()
    expect(session.switching).toBe(true)
    expect(reload).toHaveBeenCalledOnce()
    expect(binding.invalidate).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
  })

  it('restores the legacy switching flag if opening fails', async () => {
    api.openProject.mockRejectedValueOnce(new Error('Cannot open'))
    const session = useSessionStore()
    await expect(session.openProject('D:/missing')).rejects.toThrow('Cannot open')
    expect(session.switching).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('scoped replay gap recovery', () => {
  it('refetches only own snapshots once without resetting local drafts or reloading', async () => {
    binding.isLegacy = false
    const session = useSessionStore()
    await session.load()
    const hydrationCalls = hydrateDocs.mock.calls.length
    vi.clearAllMocks()
    let finish!: (value: unknown) => void
    const own = { project_id: 'project-a', epoch: 'epoch-a' }
    binding.matches.mockImplementation(scope => scope?.project_id === own.project_id && scope?.epoch === own.epoch)
    api.project.mockImplementationOnce(() => new Promise(done => { finish = done }))
    api.status.mockResolvedValueOnce({ database: true })
    api.sync.mockResolvedValueOnce({ state: 'connected' })
    const dropped = listeners.get('system.dropped')!
    dropped({ resync: true }, { project_id: 'project-b', epoch: 'epoch-a' })
    dropped({ resync: true }, { project_id: 'project-a', epoch: 'old-epoch' })
    dropped({ resync: false }, own)
    expect(api.project).not.toHaveBeenCalled()
    dropped({ resync: true }, own)
    dropped({ resync: true }, own)
    await Promise.resolve()
    expect(api.project).toHaveBeenCalledOnce()
    expect(api.status).toHaveBeenCalledOnce()
    expect(api.projectTaskCounts).toHaveBeenCalledOnce()
    expect(api.sync).toHaveBeenCalledOnce()
    finish({ ...own, root: 'D:/game-a', name: 'Fresh', version: { version: FRONTEND_VERSION } })
    await vi.waitFor(() => expect(session.project?.name).toBe('Fresh'))
    expect(session.backendVersion).toBe(FRONTEND_VERSION)
    expect(useSyncStore().live).toBe(true)
    // Startup hydration happened, but recovery must not call it again.
    expect(hydrationCalls).toBe(1)
    expect(hydrateDocs).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
    expect(disconnect).not.toHaveBeenCalled()
    expect(binding.invalidate).not.toHaveBeenCalled()
  })

  it('does not apply the desktop gap recovery policy to the legacy adapter', async () => {
    await useSessionStore().load()
    api.project.mockClear()
    listeners.get('system.dropped')!({ resync: true }, {})
    expect(api.project).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it('repeats metadata recovery when another gap arrives during a snapshot read', async () => {
    binding.isLegacy = false
    const own = { project_id: 'project-a', epoch: 'epoch-a' }
    const info = { ...own, root: 'D:/game-a', config: {}, version: { version: FRONTEND_VERSION } }
    api.project.mockResolvedValue(info)
    const session = useSessionStore()
    await session.load()
    await Promise.resolve()
    api.project.mockClear()
    let finish!: (value: typeof info) => void
    api.project.mockImplementationOnce(() => new Promise(done => { finish = done }))
    const dropped = listeners.get('system.dropped')!
    dropped({ resync: true }, own)
    await Promise.resolve()
    expect(api.project).toHaveBeenCalledOnce()
    dropped({ resync: true }, own)
    finish(info)
    await vi.waitFor(() => expect(api.project).toHaveBeenCalledTimes(2))
  })
})

describe('sync session lifetime', () => {
  it('does not apply a pending status after the renderer session is invalidated', async () => {
    let resolve!: (status: { state: string }) => void
    api.sync.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    binding.capture.mockReturnValueOnce(() => { throw new Error('Stale project response') })
    const sync = useSyncStore()
    const pending = sync.load()
    resolve({ state: 'connected' })
    await pending
    expect(sync.status).toBeNull()
    expect(sync.live).toBe(false)
  })

  it('rejects a stale settings response without applying it', async () => {
    api.updateSync.mockResolvedValueOnce({ state: 'connected' })
    binding.capture.mockReturnValueOnce(() => { throw new Error('Stale project response') })
    const sync = useSyncStore()
    await expect(sync.save({})).rejects.toThrow('Stale project response')
    expect(sync.status).toBeNull()
    expect(sync.saving).toBe(false)
  })
})

describe('frontend/backend compatibility', () => {
  it('uses the compiled frontend version and detects a changed backend on reconnect', async () => {
    const session = useSessionStore()
    await session.load()
    expect(session.versionMismatch).toBe(false)
    api.project.mockResolvedValue({ version: { version: '4.5.0' } })
    listeners.get('system.hello')!()
    await Promise.resolve()
    expect(session.versionMismatch).toBe(true)
    expect(session.frontendVersion).toBe(FRONTEND_VERSION)
    expect(session.backendVersion).toBe('4.5.0')
    api.project.mockRejectedValueOnce(new Error('offline'))
    await session.refreshVersion()
    expect(session.versionMismatch).toBe(true)
  })

  it('restarts once, waits for the target version, then reloads', async () => {
    api.project.mockResolvedValue({ root: 'D:/game', version: { version: '4.5.0' } })
    const session = useSessionStore()
    await session.load()
    recoverRuntime.mockResolvedValue({ action: 'restart', version: FRONTEND_VERSION })
    await session.recoverVersionMismatch()
    expect(recoverRuntime).toHaveBeenCalledWith(FRONTEND_VERSION, '4.5.0')
    expect(waitForVersion).toHaveBeenCalledWith(FRONTEND_VERSION)
    expect(reload).toHaveBeenCalledOnce()
    expect(session.recoveryState).toBe('reloading')
  })

  it('reloads a stale frontend without restarting the current backend', async () => {
    api.project.mockResolvedValue({ version: { version: '99.0.0' } })
    const session = useSessionStore()
    await session.load()
    recoverRuntime.mockResolvedValue({ action: 'reload', version: '99.0.0' })
    await session.recoverVersionMismatch()
    expect(waitForVersion).not.toHaveBeenCalled()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('does not loop across reloads and allows an explicit retry', async () => {
    api.project.mockResolvedValue({ version: { version: '4.5.0' } })
    recoverRuntime.mockRejectedValue(new ApiError(404, 'Not found'))
    const session = useSessionStore()
    await session.load()
    await session.recoverVersionMismatch()
    expect(session.recoveryError).toContain('too old')
    disposePinia(pinia)
    pinia = createPinia()
    setActivePinia(pinia)
    const afterReload = useSessionStore()
    await afterReload.load()
    await afterReload.recoverVersionMismatch()
    expect(recoverRuntime).toHaveBeenCalledOnce()
    expect(afterReload.recoveryError).toContain('already ran')
    await afterReload.recoverVersionMismatch(true)
    expect(recoverRuntime).toHaveBeenCalledTimes(2)
    expect(reload).not.toHaveBeenCalled()
  })

  it('treats missing version metadata as an error, without trying to kill an unknown process', async () => {
    api.project.mockResolvedValue({ root: 'D:/game' })
    const session = useSessionStore()
    await session.load()
    expect(session.versionMismatch).toBe(true)
    await session.recoverVersionMismatch()
    expect(session.recoveryState).toBe('failed')
    expect(recoverRuntime).not.toHaveBeenCalled()
  })
})

describe('backend readiness', () => {
  const status = {
    database: true, event_seq: 0, ws_clients: 1, jobs: [], vcs_backends: {},
    agent_backends: { codex: { available: true, reason: 'Ready' } },
  }

  it('clears stale readiness after a failed check and recovers on the next check', async () => {
    const session = useSessionStore()
    api.status.mockResolvedValue(status)
    await session.refreshStatus()
    expect(session.status?.agent_backends.codex?.available).toBe(true)

    api.status.mockRejectedValueOnce(new Error('offline'))
    await session.refreshStatus()
    expect(session.status).toBeNull()

    await session.refreshStatus()
    expect(session.status?.agent_backends.codex?.available).toBe(true)
  })

  it('shares an in-flight check between the navigation and task page', async () => {
    let resolve!: (value: typeof status) => void
    api.status.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const session = useSessionStore()
    const navigation = session.refreshStatus()
    const taskPage = session.refreshStatus()
    expect(api.status).toHaveBeenCalledOnce()
    resolve(status)
    await Promise.all([navigation, taskPage])
    expect(session.status).toEqual(status)
  })
})

describe('project task counts', () => {
  it('loads without visiting Tasks and refreshes on task changes and reconnect', async () => {
    const session = useSessionStore()
    await session.load()
    expect(session.taskCounts).toEqual({ in_progress: 2, needs_review: 1 })

    for (const event of ['team.task.created', 'team.task.status_changed', 'team.task.deleted', 'system.hello']) {
      api.projectTaskCounts.mockClear()
      listeners.get(event)!()
      await Promise.resolve()
      expect(api.projectTaskCounts).toHaveBeenCalledOnce()
    }
  })

  it('rereads after an event during a request instead of losing the new status', async () => {
    const session = useSessionStore()
    let resolve!: (counts: { in_progress: number; needs_review: number }) => void
    api.projectTaskCounts.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const first = session.refreshTaskCounts()
    await session.refreshTaskCounts()
    await session.refreshTaskCounts()
    expect(api.projectTaskCounts).toHaveBeenCalledOnce()
    resolve({ in_progress: 3, needs_review: 0 })
    await first
    expect(api.projectTaskCounts).toHaveBeenCalledTimes(2)
    expect(session.taskCounts).toEqual({ in_progress: 2, needs_review: 1 })
  })

  it('shows unavailable counts on failure and recovers on the next refresh', async () => {
    const session = useSessionStore()
    await session.refreshTaskCounts()
    api.projectTaskCounts.mockRejectedValueOnce(new Error('offline'))
    await session.refreshTaskCounts()
    expect(session.taskCounts).toBeNull()
    await session.refreshTaskCounts()
    expect(session.taskCounts).toEqual({ in_progress: 2, needs_review: 1 })
  })
})
