import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import type { DesktopTabsBridge, DesktopTabsState, DesktopTabSnapshot } from '@/desktop'
import { checkedDesktopBootstrap, createDesktopShellController, createDesktopTabGestures, type DesktopShellState } from './tabs'

function tab(id: string): DesktopTabSnapshot {
  return { tabId: id, ownerWindowId: 'w1', revision: 1, transferGeneration: 0, state: 'ready',
    project: { root: `D:/${id}`, project_id: id, epoch: 'epoch1' } }
}
function shell(revision = 1, active = 'a'): DesktopShellState {
  return { role: 'shell', window: { windowId: 'w1', revision, activeTabId: active,
    tabIds: ['a', 'b'], insets: { top: 44, left: 0, right: 0, bottom: 0 } }, tabs: [tab('a'), tab('b')] }
}
function fixture() {
  let current = shell()
  let notify: (state: DesktopTabsState) => void = () => {}
  const unsubscribe = vi.fn()
  const bridge: DesktopTabsBridge = {
    bootstrap: vi.fn(async () => current), getState: vi.fn(async () => current),
    openProject: vi.fn(async () => tab('b')),
    activateTab: vi.fn(async request => { current = shell(2, request.tabId); return tab(request.tabId) }),
    beginDrag: vi.fn(async () => ({ token: 'token', expiresAt: Date.now() + 30_000 })),
    cancelDrag: vi.fn(async () => {}), setDragGeometry: vi.fn(async () => {}),
    detachTab: vi.fn(async () => tab('a')), reorderTab: vi.fn(async () => tab('a')),
    prepareClose: vi.fn(async () => ({ prepared: false })),
    onStateChange: vi.fn(callback => { notify = callback; return unsubscribe }),
    setMenuOpen: vi.fn(async () => {}), recentProjects: vi.fn(async () => []),
    inspectPath: vi.fn(async path => ({ path, name: path, exists: true, is_dir: true, is_project: false })),
    browseDirectory: vi.fn(async path => ({ path, name: path, parent: '', is_project: false, entries: [], truncated: false })),
    pickerStatus: vi.fn(async () => ({ available: true, reason: '', home: 'D:/', default_start: 'D:/' })),
    pickDirectory: vi.fn(async () => ({ available: true, cancelled: true, reason: '', path: null })),
    forgetRecent: vi.fn(async () => {}),
  }
  return { bridge, unsubscribe, notify: (value: DesktopTabsState) => notify(value) }
}

describe('checked desktop bootstrap', () => {
  it('leaves browser URLs legacy, and uses only the authorized role for desktop', async () => {
    expect(await checkedDesktopBootstrap(undefined, '/')).toBeNull()
    const { bridge } = fixture()
    expect((await checkedDesktopBootstrap(bridge, '/'))?.role).toBe('shell')
    const project = { role: 'project', windowId: 'w1', windowRevision: 1, tab: tab('a'), binding: tab('a').project } as const
    vi.mocked(bridge.bootstrap).mockResolvedValue(project)
    expect(await checkedDesktopBootstrap(bridge, '/p/a/documents')).toBe(project)
    await expect(checkedDesktopBootstrap(bridge, '/p/b/documents')).rejects.toThrow()
    await expect(checkedDesktopBootstrap(bridge, '/')).rejects.toThrow()
    vi.mocked(bridge.bootstrap).mockRejectedValue(new Error('denied'))
    await expect(checkedDesktopBootstrap(bridge, '/')).rejects.toThrow('denied')
  })

  it('keeps project code behind dynamic bootstrap imports', () => {
    const source = readFileSync(fileURLToPath(new URL('../main.ts', import.meta.url)), 'utf8')
    const staticImports = source.split('\n').filter(line => line.startsWith('import ')).join('\n')
    expect(staticImports).not.toMatch(/App\.vue|pinia|router|stores|plugin|session|ws\//)
    const adapter = readFileSync(fileURLToPath(new URL('./tabs.ts', import.meta.url)), 'utf8')
    expect(adapter).not.toMatch(/from ['"][^'"]*(?:stores|api\/client|session|plugin-runtime|ws\/)/)
  })
})

describe('shell IPC adapter', () => {
  it('shows and clears main-owned initial-open errors without hiding local failures', async () => {
    const { bridge, notify } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    notify({ ...shell(2), error: 'seed unavailable' })
    expect(controller.error.value).toBe('seed unavailable')
    notify(shell(3))
    expect(controller.error.value).toBeNull()
    vi.mocked(bridge.openProject).mockRejectedValueOnce(new Error('open failed'))
    await controller.openProject('D:/c')
    notify(shell(4))
    expect(controller.error.value).toBe('open failed')
    controller.dispose()
  })
  it('activates A/B through IPC without assigning identity or loading either SPA', async () => {
    const { bridge } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    const bindings = controller.state.value?.tabs.map(item => item.project)
    await controller.activate('b')
    expect(bridge.activateTab).toHaveBeenCalledWith({ windowId: 'w1', windowRevision: 1, tabId: 'b', tabRevision: 1 })
    expect(controller.state.value?.window.activeTabId).toBe('b')
    expect(controller.state.value?.tabs.map(item => item.project)).toEqual(bindings)
    expect(bridge.openProject).not.toHaveBeenCalled()
    controller.dispose()
  })

  it('reserves native hit zones before displaying the menu and closes via IPC', async () => {
    const { bridge } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    let release!: () => void
    vi.mocked(bridge.setMenuOpen).mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const opening = controller.setMenuOpen(true)
    expect(controller.menuOpen.value).toBe(false)
    release(); await opening
    expect(controller.menuOpen.value).toBe(true)
    await controller.setMenuOpen(false)
    expect(bridge.setMenuOpen).toHaveBeenLastCalledWith({ windowId: 'w1', windowRevision: 1, open: false })
    expect(controller.menuOpen.value).toBe(false)
    controller.dispose()
  })

  it('keeps pending/error local and an incumbent A intact after failed B open', async () => {
    const { bridge } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    let reject!: (cause: Error) => void
    vi.mocked(bridge.openProject).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
    const opening = controller.openProject('D:/b')
    expect(controller.pending.value).toEqual(['D:/b'])
    reject(new Error('unavailable')); await opening
    expect(controller.error.value).toBe('unavailable')
    expect(controller.pending.value).toEqual([])
    expect(controller.state.value?.window.activeTabId).toBe('a')
    controller.dispose()
  })

  it('requires an inspected selection plus explicit create for nonprojects', async () => {
    const { bridge } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    await controller.openProject('D:/new', true)
    expect(bridge.openProject).not.toHaveBeenCalled()
    await controller.inspect('D:/new')
    expect(bridge.openProject).not.toHaveBeenCalled()
    await controller.openProject('D:/new', true)
    expect(bridge.openProject).toHaveBeenCalledWith({ windowId: 'w1', windowRevision: 1, root: 'D:/new', create: true })
    controller.dispose()
  })

  it('deduplicates an existing foreign-window open through main, not shell runtime identity', async () => {
    const { bridge } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    vi.mocked(bridge.openProject).mockResolvedValue({ ...tab('b'), ownerWindowId: 'w2' })
    await controller.setMenuOpen(true)
    await controller.openProject('D:/b')
    expect(bridge.openProject).toHaveBeenCalledWith({ windowId: 'w1', windowRevision: 1, root: 'D:/b', create: false })
    expect(controller.state.value?.window.activeTabId).toBe('a')
    expect(controller.menuOpen.value).toBe(false)
    controller.dispose()
  })

  it('ignores old state and out-of-order path inspections; unsubscribes on disposal', async () => {
    const { bridge, notify, unsubscribe } = fixture()
    const controller = createDesktopShellController(bridge)
    await controller.start()
    notify(shell(3, 'b')); notify(shell(2, 'a'))
    expect(controller.state.value?.window.activeTabId).toBe('b')
    let resolveOld!: (value: Awaited<ReturnType<DesktopTabsBridge['inspectPath']>>) => void
    vi.mocked(bridge.inspectPath).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const old = controller.inspect('D:/old')
    await controller.inspect('D:/new')
    resolveOld({ path: 'D:/old', name: 'old', exists: true, is_dir: true, is_project: true }); await old
    expect(controller.selected.value?.path).toBe('D:/new')
    controller.dispose(); expect(unsubscribe).toHaveBeenCalledOnce()
    notify(shell(4, 'a'))
    expect(controller.state.value?.window.activeTabId).toBe('b')
  })
})

describe('captured-pointer tab gesture adapter', () => {
  function setup() {
    const { bridge } = fixture()
    let state = shell()
    const activate = vi.fn(), error = vi.fn()
    const gestures = createDesktopTabGestures({ bridge, state: () => state, activate, error })
    return { bridge, gestures, activate, error, update(next: DesktopShellState) { state = next; gestures.sync() } }
  }
  const point = { x: 125.5, y: 61.25 }

  it('arms immediately with immutable source revisions and client CSS coordinates, not activation', () => {
    const f = setup()
    expect(f.gestures.begin('a', point)).toBe(true)
    expect(f.bridge.beginDrag).toHaveBeenCalledWith({ windowId: 'w1', windowRevision: 1, tabId: 'a', tabRevision: 1, point })
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.gestures.begin('b', point)).toBe(false)
    f.gestures.dispose()
  })

  it('never turns pointer-up/movement into transfer, detach or reorder success', async () => {
    const f = setup()
    f.gestures.begin('a', point)
    f.gestures.move({ x: 300, y: 600 })
    f.gestures.release({ x: 500, y: 600 })
    await f.gestures.click('a')
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    expect(f.bridge.reorderTab).not.toHaveBeenCalled()
    expect(f.bridge.cancelDrag).not.toHaveBeenCalled()
    f.gestures.dispose()
  })

  it('waits for arm and native cancellation acknowledgement before ordinary click activation', async () => {
    const f = setup()
    let arm!: (value: { token: string; expiresAt: number }) => void, disarm!: () => void
    vi.mocked(f.bridge.beginDrag).mockReturnValueOnce(new Promise(resolve => { arm = resolve }))
    vi.mocked(f.bridge.cancelDrag).mockReturnValueOnce(new Promise(resolve => { disarm = resolve }))
    f.gestures.begin('b', point)
    f.gestures.release(point)
    const clicking = f.gestures.click('b')
    expect(f.activate).not.toHaveBeenCalled()
    arm({ token: 'late', expiresAt: Date.now() + 30_000 }); await Promise.resolve(); await Promise.resolve()
    expect(f.bridge.cancelDrag).toHaveBeenCalledWith({ windowId: 'w1', windowRevision: 1, token: 'late' })
    expect(f.activate).not.toHaveBeenCalled()
    disarm(); await clicking
    expect(f.activate).toHaveBeenCalledExactlyOnceWith('b')
    f.gestures.dispose()
  })

  it.each(['Escape', 'pointercancel', 'lost capture', 'unmount'])('queues %s cancellation even before the token arrives', async reason => {
    const f = setup()
    let arm!: (value: { token: string; expiresAt: number }) => void
    vi.mocked(f.bridge.beginDrag).mockReturnValueOnce(new Promise(resolve => { arm = resolve }))
    f.gestures.begin('a', point)
    const cancelling = reason === 'unmount' ? (f.gestures.dispose(), Promise.resolve()) : f.gestures.cancel()
    arm({ token: 'late', expiresAt: Date.now() + 30_000 }); await cancelling; await Promise.resolve(); await Promise.resolve()
    await f.gestures.click('a')
    expect(f.bridge.cancelDrag).toHaveBeenCalledExactlyOnceWith({ windowId: 'w1', windowRevision: 1, token: 'late' })
    expect(f.activate).not.toHaveBeenCalled()
    f.gestures.dispose()
  })

  it.each(['transfer', 'detach', 'reorder', 'source closed'])('invalidates local clicks when main publishes %s', async outcome => {
    const f = setup()
    f.gestures.begin('a', point); f.gestures.release(point)
    let next = shell(2)
    if (outcome === 'transfer' || outcome === 'detach') next = { ...next, tabs: next.tabs.map(t => t.tabId === 'a' ? { ...t, ownerWindowId: 'w2', revision: 2 } : t) }
    if (outcome === 'source closed') next = { ...next, tabs: [] }
    f.update(next)
    await f.gestures.click('a'); await Promise.resolve()
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.bridge.cancelDrag).toHaveBeenCalledOnce()
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    expect(f.bridge.reorderTab).not.toHaveBeenCalled()
    f.gestures.dispose()
  })

  it('does not resurrect a late click after a newer gesture or disposal', async () => {
    const f = setup()
    let disarm!: () => void
    vi.mocked(f.bridge.cancelDrag).mockReturnValueOnce(new Promise(resolve => { disarm = resolve }))
    f.gestures.begin('a', point); f.gestures.release(point)
    const clicking = f.gestures.click('a'); await Promise.resolve()
    f.gestures.begin('b', point)
    disarm(); await clicking
    expect(f.activate).not.toHaveBeenCalled()
    f.gestures.dispose()
    await f.gestures.click('b', false)
    expect(f.activate).not.toHaveBeenCalled()
  })

  it('keeps click activation usable when native arming fails, without optimistic topology changes', async () => {
    const f = setup()
    vi.mocked(f.bridge.beginDrag).mockRejectedValueOnce(new Error('helper unavailable'))
    f.gestures.begin('a', point); f.gestures.release(point)
    await f.gestures.click('a')
    expect(f.error).toHaveBeenCalledOnce()
    expect(f.activate).toHaveBeenCalledExactlyOnceWith('a')
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    f.gestures.dispose()
  })

  it('does not move focus into project contents if disarm IPC fails', async () => {
    const f = setup()
    vi.mocked(f.bridge.cancelDrag).mockRejectedValueOnce(new Error('IPC failed'))
    f.gestures.begin('a', point); f.gestures.release(point)
    await f.gestures.click('a')
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.error).toHaveBeenCalledOnce()
    f.gestures.dispose()
  })

  it('allows keyboard activation after a moved gesture, but only after disarm', async () => {
    const f = setup()
    f.gestures.begin('a', point); f.gestures.release({ x: 300, y: 500 })
    await f.gestures.click('b', false)
    expect(f.bridge.cancelDrag).toHaveBeenCalledOnce()
    expect(f.activate).toHaveBeenCalledExactlyOnceWith('b')
    f.gestures.dispose()
  })

  it('does not let Escape followed by Enter overtake an in-flight cancellation', async () => {
    const f = setup()
    let disarm!: () => void
    vi.mocked(f.bridge.cancelDrag).mockReturnValueOnce(new Promise(resolve => { disarm = resolve }))
    f.gestures.begin('a', point)
    const cancelling = f.gestures.cancel()
    const keyboard = f.gestures.click('b', false)
    await Promise.resolve(); await Promise.resolve()
    expect(f.activate).not.toHaveBeenCalled()
    disarm(); await cancelling; await keyboard
    expect(f.activate).toHaveBeenCalledExactlyOnceWith('b')
    f.gestures.dispose()
  })

  it('rejects crashed/loading/foreign tabs and allows keyboard activation without a bridge', async () => {
    const f = setup()
    f.update({ ...shell(), tabs: [{ ...tab('a'), state: 'crashed' }, { ...tab('b'), ownerWindowId: 'w2' }] })
    expect(f.gestures.begin('a', point)).toBe(false)
    expect(f.gestures.begin('b', point)).toBe(false)
    expect(f.bridge.beginDrag).not.toHaveBeenCalled()
    f.gestures.dispose()
    const activate = vi.fn()
    const noDesktop = createDesktopTabGestures({ bridge: undefined, state: () => shell(), activate, error: vi.fn() })
    expect(noDesktop.begin('a', point)).toBe(false)
    await noDesktop.click('a', false)
    expect(activate).toHaveBeenCalledWith('a')
    noDesktop.dispose()
  })
})
