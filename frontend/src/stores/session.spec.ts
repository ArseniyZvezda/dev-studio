import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api, listeners, recoverRuntime, waitForVersion, reload } = vi.hoisted(() => ({
  api: { project: vi.fn(), projectTaskCounts: vi.fn(), status: vi.fn() },
  listeners: new Map<string, () => void>(),
  recoverRuntime: vi.fn(), waitForVersion: vi.fn(), reload: vi.fn(),
}))

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
    ws: { state: ref('open'), connect: vi.fn() },
    onEvent: (event: string, callback: () => void) => listeners.set(event, callback),
  }
})
vi.mock('./theme', () => ({ useThemeStore: () => ({ hydrate: vi.fn() }) }))
vi.mock('./docs', () => ({ useDocsStore: () => ({ hydrate: vi.fn(), restoreFolds: vi.fn() }) }))

import { useSessionStore } from './session'
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
