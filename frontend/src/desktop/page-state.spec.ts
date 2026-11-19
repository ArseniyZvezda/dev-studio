import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { effectScope } from 'vue'
import { checkedPageDescriptor, connectDesktopPageState, createPageStateTracker, useDesktopPageTarget } from './page-state'
import { rendererSession } from '@/session/project-session'
import type { DesktopPageRestoreRequest, DesktopTabsBridge, DesktopTabsState } from '@/desktop'

function runtime(id: string) {
  return createRouter({ history: createMemoryHistory(`/p/${id}`), routes: [
    ...['/explorer', '/tasks', '/documents', '/chats', '/producer', '/settings', '/tools/plugin-page']
      .map(path => ({ path, component: {} })),
    { path: '/pipelines/:id', component: {} },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: {} },
  ] })
}

describe('durable desktop pages (§5.3.6)', () => {
  it('keeps consumed Explorer/Tasks selectors across a snapshot and fresh runtime, but clears closed selections', async () => {
    const old = runtime('old'), tracker = createPageStateTracker(old)
    await old.push('/explorer?path=Assets/mesh.fbx&token=secret&epoch=old')
    tracker.setSelector('/explorer', 'path', 'Assets/mesh.fbx')
    await old.replace('/explorer') // the view consumed its query
    const snapshot = JSON.parse(JSON.stringify(tracker.snapshot()))
    expect(snapshot).toEqual({ path: '/explorer', query: { path: 'Assets/mesh.fbx' } })

    const fresh = runtime('fresh'), restored = createPageStateTracker(fresh)
    await fresh.push('/settings')
    const checkTarget = vi.fn().mockResolvedValue(true)
    expect(await restored.restore(snapshot, { checkTarget })).toBe('restored')
    expect(fresh.currentRoute.value.fullPath).toBe('/explorer?path=Assets/mesh.fbx')
    expect(fresh.resolve(snapshot).href).toContain('/p/fresh/')
    expect(fresh.resolve(snapshot).href).not.toContain('/p/old/')

    await old.push('/tasks?task=task_1&assign=member_1')
    tracker.setSelector('/tasks', 'task', 'task_1')
    tracker.setSelector('/tasks', 'assign', 'member_1')
    await old.replace('/tasks')
    expect(tracker.snapshot()).toEqual({ path: '/tasks', query: { task: 'task_1', assign: 'member_1' } })
    expect(await restored.restore(tracker.snapshot(), { checkTarget })).toBe('restored')
    tracker.setSelector('/tasks', 'task', null)
    tracker.setSelector('/tasks', 'assign', null)
    expect(tracker.snapshot()).toEqual({ path: '/tasks' })
    await old.push('/settings')
    await old.push('/tasks')
    expect(tracker.snapshot()).toEqual({ path: '/tasks' })
  })

  it('uses the registered router for built-in, document/chat/pipeline and plugin pages', async () => {
    const router = runtime('new'), tracker = createPageStateTracker(router)
    await router.push('/explorer')
    const pages = [
      { path: '/settings' }, { path: '/documents', query: { path: 'plans/design.md' } },
      { path: '/chats', query: { chat: 'chat_1' } }, { path: '/producer', query: { chat: 'chat_2' } },
      { path: '/pipelines/build' }, { path: '/tools/plugin-page' },
    ]
    for (const page of pages) {
      expect(await tracker.restore(page, { checkTarget: async () => true })).toBe('restored')
      expect(tracker.snapshot()).toEqual(page)
    }
  })

  it('rejects malformed/foreign routes and reports missing targets without navigating elsewhere', async () => {
    const router = runtime('A'), tracker = createPageStateTracker(router)
    await router.push('/settings')
    const checkTarget = vi.fn().mockResolvedValue(false)
    expect(await tracker.restore({ path: '/documents', query: { path: 'deleted.md' } }, { checkTarget })).toBe('unavailable')
    expect(await tracker.restore({ path: '/removed-plugin' }, { checkTarget: async () => true })).toBe('unavailable')
    expect(await tracker.restore({ path: '/p/B/documents' }, { checkTarget })).toBe('invalid')
    expect(checkedPageDescriptor({ path: 'https://example.org' })).toBeNull()
    expect(checkedPageDescriptor({ path: '/%70/B' })).toBeNull()
    expect(checkedPageDescriptor({ path: '/documents', query: { path: '../private.md' } })).toBeNull()
    expect(checkedPageDescriptor({ path: '/tasks', query: { token: 'secret' } })).toBeNull()
    expect(checkedPageDescriptor({ path: '/tasks', project_id: 'B', epoch: 'old' })).toBeNull()
    expect(checkedPageDescriptor({ path: '/explorer', query: { path: 'notes/100%#plan.md' } }))
      .toEqual({ path: '/explorer', query: { path: 'notes/100%#plan.md' } })
    expect(router.currentRoute.value.fullPath).toBe('/settings')
    let available!: (value: boolean) => void
    const pending = tracker.restore({ path: '/documents' }, { checkTarget: () => new Promise(resolve => { available = resolve }) })
    await router.push('/chats') // a slow preflight must not pull the user back
    available(true)
    expect(await pending).toBe('stale')
    expect(router.currentRoute.value.fullPath).toBe('/chats')
    expect(await tracker.restore({ path: '/documents' }, { checkTarget: async () => { throw new Error('offline') } })).toBe('failed')
    expect(router.currentRoute.value.fullPath).toBe('/chats')
  })

  it('wires scoped target checks and ACKs, while transfer state only refreshes the IPC identity', async () => {
    vi.spyOn(rendererSession, 'projectId', 'get').mockReturnValue('fresh')
    class ApiError extends Error { constructor(readonly status: number) { super('missing') } }
    const doc = vi.fn().mockResolvedValue({ path: 'design.md' })
    vi.doMock('@/api/client', () => ({ api: { project: vi.fn().mockResolvedValue({}), doc }, ApiError }))
    vi.doMock('@/stores/chat', () => ({ useChatStore: () => ({ view: 'list', activeChatId: null }) }))
    vi.doMock('@/stores/docs', () => ({ useDocsStore: () => ({ open: null }) }))
    const router = runtime('fresh')
    await router.push('/settings')
    let state: DesktopTabsState = { role: 'project', windowId: '1', windowRevision: 1,
      binding: { root: 'D:/A', project_id: 'fresh', epoch: 'new' },
      tab: { tabId: 'tab', revision: 1, transferGeneration: 0, ownerWindowId: '1', state: 'ready',
        project: { root: 'D:/A', project_id: 'fresh', epoch: 'new' } } }
    let receive!: (request: DesktopPageRestoreRequest) => void
    let stateChanged!: (state: DesktopTabsState) => void
    const reportPage = vi.fn().mockResolvedValue(undefined), ack = vi.fn().mockResolvedValue(undefined)
    const bridge = { reportPage, acknowledgePageRestore: ack, bootstrap: async () => state,
      onRestorePage: (cb: typeof receive) => { receive = cb; return () => {} },
      onStateChange: (cb: typeof stateChanged) => { stateChanged = cb; return () => {} },
    } as unknown as DesktopTabsBridge
    connectDesktopPageState(router, bridge)
    await vi.waitFor(() => expect(reportPage).toHaveBeenCalled())
    const request = { requestId: 'r1', tabId: 'tab', tabRevision: 1, transferGeneration: 0,
      page: { path: '/documents', query: { path: 'design.md' } } }
    receive(request)
    await vi.waitFor(() => expect(doc).toHaveBeenCalledWith('design.md'))
    expect(ack).not.toHaveBeenCalled() // router-ready is not page-ready
    const scope = effectScope()
    let opened!: (value: boolean) => void
    const apply = vi.fn(() => new Promise<boolean>(resolve => { opened = resolve }))
    scope.run(() => useDesktopPageTarget('/documents', apply))
    await vi.waitFor(() => expect(apply).toHaveBeenCalled())
    expect(ack).not.toHaveBeenCalled()
    opened(true)
    await vi.waitFor(() => expect(ack).toHaveBeenCalledWith(expect.objectContaining({ requestId: 'r1', status: 'restored' })))
    expect(doc).toHaveBeenCalledWith('design.md')
    const before = router.currentRoute.value.fullPath
    state = { ...state, windowId: '2', tab: { ...state.tab, revision: 2, transferGeneration: 1, ownerWindowId: '2' } }
    stateChanged(state)
    await vi.waitFor(() => expect(reportPage).toHaveBeenLastCalledWith(expect.objectContaining({ tabRevision: 2, transferGeneration: 1 })))
    expect(router.currentRoute.value.fullPath).toBe(before)
    scope.stop()
    receive({ ...request, requestId: 'old' })
    await vi.waitFor(() => expect(ack).toHaveBeenCalledWith(expect.objectContaining({ requestId: 'old', status: 'stale' })))
    doc.mockRejectedValue(new ApiError(404))
    receive({ ...request, requestId: 'deleted', tabRevision: 2, transferGeneration: 1 })
    await vi.waitFor(() => expect(ack).toHaveBeenCalledWith(expect.objectContaining({ requestId: 'deleted', status: 'unavailable' })))
    expect(router.currentRoute.value.fullPath).toBe(before)
    vi.restoreAllMocks()
    vi.doUnmock('@/api/client'); vi.doUnmock('@/stores/chat'); vi.doUnmock('@/stores/docs')
  })

  it('never attests the transient initial route: reports wait for the settled first navigation', async () => {
    vi.spyOn(rendererSession, 'projectId', 'get').mockReturnValue('boot')
    vi.doMock('@/stores/chat', () => ({ useChatStore: () => ({ view: 'list', activeChatId: null }) }))
    vi.doMock('@/stores/docs', () => ({ useDocsStore: () => ({ open: null }) }))
    const router = runtime('boot')
    let release!: () => void
    router.beforeEach(() => new Promise<boolean>(resolve => { release = () => resolve(true) }))
    const state: DesktopTabsState = { role: 'project', windowId: '1', windowRevision: 1,
      binding: { root: 'D:/A', project_id: 'boot', epoch: 'new' },
      tab: { tabId: 'tab', revision: 1, transferGeneration: 0, ownerWindowId: '1', state: 'ready',
        project: { root: 'D:/A', project_id: 'boot', epoch: 'new' } } }
    let stateChanged!: (value: DesktopTabsState) => void
    const reportPage = vi.fn().mockResolvedValue(undefined)
    const bridge = { reportPage, acknowledgePageRestore: vi.fn().mockResolvedValue(undefined),
      bootstrap: async () => state, onRestorePage: () => () => {},
      onStateChange: (cb: typeof stateChanged) => { stateChanged = cb; return () => {} },
    } as unknown as DesktopTabsBridge
    // The initial navigation is in flight (a lazy section chunk still loads), so
    // currentRoute remains the START location '/'. Main gates its page restore on
    // the first report: attesting '/' would release the restore into a race with
    // the settling navigation, which the tracker then judges "stale" (§5.3.6).
    const initial = router.push('/explorer')
    connectDesktopPageState(router, bridge)
    await vi.waitFor(() => expect(typeof release).toBe('function')) // guard reached, navigation in flight
    stateChanged(state)
    expect(reportPage).not.toHaveBeenCalled()
    release()
    await initial
    await vi.waitFor(() => expect(reportPage).toHaveBeenCalledTimes(1))
    expect(reportPage.mock.calls[0][0]).toMatchObject({ tabId: 'tab', tabRevision: 1,
      transferGeneration: 0, page: { path: '/explorer' } })
    vi.restoreAllMocks()
    vi.doUnmock('@/stores/chat'); vi.doUnmock('@/stores/docs')
  })
})
