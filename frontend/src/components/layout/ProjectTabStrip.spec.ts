// @vitest-environment jsdom
import { createApp, h, nextTick, shallowRef } from 'vue'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DesktopTabsBridge } from '@/desktop'
import type { DesktopShellState } from '@/desktop/tabs'
import ProjectTabStrip from './ProjectTabStrip.vue'

vi.mock('@/i18n', () => ({ t: (key: string) => key }))

const state: DesktopShellState = {
  role: 'shell', window: { windowId: 'w', revision: 1, tabIds: ['a', 'b'], activeTabId: 'a', insets: { top: 44, right: 0, bottom: 0, left: 0 } },
  tabs: ['a', 'b'].map((tabId, i) => ({ tabId, project: { root: `D:/Projects/${tabId}`, project_id: tabId, epoch: 'e' }, ownerWindowId: 'w', revision: 1, transferGeneration: 0, state: i ? 'crashed' : 'ready' })),
}

describe('project tab strip', () => {
  it.each([
    [2, 1], [0, 1], [2, 0], [0, 0],
  ])('shows %i running and %i review tasks instead of Ready', async (running, review) => {
    const host = document.createElement('div')
    const current = shallowRef<DesktopShellState>({ ...state, tabs: state.tabs.map(tab => ({
      ...tab, task_counts: { in_progress: running, needs_review: review },
    })) })
    const app = createApp({ render: () => h(ProjectTabStrip, { state: current.value, pending: [] }) })
    app.mount(host)
    try {
      const tab = host.querySelector('[role="tab"]')!
      const counts = tab.querySelectorAll('.project-tab-strip__count')
      expect([...counts].map(node => node.textContent?.trim())).toEqual([String(running), String(review)])
      expect(counts[0]?.querySelector('i')?.style.background).toBe(`var(--vxs-status-${running ? 'running' : 'todo'})`)
      expect(counts[1]?.querySelector('i')?.style.background).toBe(`var(--vxs-status-${review ? 'review' : 'todo'})`)
      expect(counts[0]?.getAttribute('aria-label')).toBe('project.taskCounts.inProgress')
      expect(counts[1]?.getAttribute('aria-label')).toBe('project.taskCounts.needsReview')
      expect(tab.textContent).not.toContain('desktop.tabs.ready')
      expect(tab.getAttribute('title')).toBe('D:/Projects/a')
      // Main publications update badges without replacing tabs or topology revisions.
      current.value = { ...current.value, tabs: current.value.tabs.map(item => ({
        ...item, task_counts: { in_progress: 0, needs_review: 0 },
      })) }
      await nextTick()
      expect(host.querySelector('[role="tab"]')).toBe(tab)
      expect([...tab.querySelectorAll('.project-tab-strip__count')].map(node => node.textContent?.trim())).toEqual(['0', '0'])
    } finally { app.unmount() }
  })

  it('does not disguise unavailable counts as zero tasks', () => {
    const host = document.createElement('div')
    const app = createApp(ProjectTabStrip, { state, pending: [] })
    app.mount(host)
    try {
      expect([...host.querySelectorAll('[role="tab"] .project-tab-strip__count')].map(node => node.textContent?.trim())).toEqual(['—', '—', '—', '—'])
      expect(host.querySelectorAll('[role="tab"] .project-tab-strip__dot')).toHaveLength(4)
    } finally { app.unmount() }
  })

  it('shows a pending root before main has created its project view', () => {
    const host = document.createElement('div')
    const app = createApp(ProjectTabStrip, { state, pending: ['D:/Projects/new'] })
    app.mount(host)
    try {
      expect(host.querySelector('[role="status"]')?.textContent).toContain('new')
      expect(host.querySelector('[role="status"]')?.getAttribute('aria-busy')).toBe('true')
      expect(host.querySelectorAll('[role="tab"]')).toHaveLength(2)
    } finally { app.unmount() }
  })
  it('renders accessible tabs, pending roots, active and crashed states without draggable gestures', () => {
    const host = document.createElement('div')
    const activate = vi.fn(), open = vi.fn()
    const app = createApp(ProjectTabStrip, { state, pending: ['D:/Projects/a'], onActivate: activate, onOpen: open })
    app.mount(host)
    try {
      const tabs = host.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      expect(tabs).toHaveLength(2)
      expect(tabs[0]?.getAttribute('aria-selected')).toBe('true')
      expect(tabs[0]?.getAttribute('aria-busy')).toBe('true')
      expect(tabs[0]?.textContent).toContain('desktop.tabs.pending')
      expect(tabs[1]?.classList.contains('is-crashed')).toBe(true)
      expect(host.querySelector('[draggable]')).toBeNull()
      tabs[1]?.click()
      expect(activate).toHaveBeenCalledWith('b')
      host.querySelector<HTMLButtonElement>('[aria-haspopup="dialog"]')?.click()
      expect(open).toHaveBeenCalledOnce()
    } finally { app.unmount() }
  })

  it('supports arrows, wraparound, Home and End with real keyboard focus', () => {
    const host = document.createElement('div')
    document.body.append(host)
    const activate = vi.fn()
    const app = createApp(ProjectTabStrip, { state, pending: [], onActivate: activate })
    app.mount(host)
    try {
      const tabs = host.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      tabs[0]!.focus()
      for (const [key, index] of [['ArrowLeft', 1], ['Home', 0], ['End', 1], ['ArrowRight', 0]] as const) {
        document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
        expect(document.activeElement).toBe(tabs[index])
        expect(activate).toHaveBeenLastCalledWith(index ? 'b' : 'a')
      }
    } finally { app.unmount(); host.remove() }
  })
})

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); delete window.voxariumTabs; vi.restoreAllMocks() })
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function mountGestures(pending: string[] = []) {
  const bridge = {
    beginDrag: vi.fn(async (_request: Parameters<DesktopTabsBridge['beginDrag']>[0]) => ({ token: 'token', expiresAt: Date.now() + 30_000 })),
    cancelDrag: vi.fn(async (_request: Parameters<DesktopTabsBridge['cancelDrag']>[0]) => {}),
    setDragGeometry: vi.fn(async (_request: Parameters<DesktopTabsBridge['setDragGeometry']>[0]) => {}),
    closeTab: vi.fn(async (_request: Parameters<NonNullable<DesktopTabsBridge['closeTab']>>[0]) => ({ closed: true })),
    detachTab: vi.fn(async (_request: Parameters<DesktopTabsBridge['detachTab']>[0]) => state.tabs[0]!),
    reorderTab: vi.fn(async (_request: Parameters<DesktopTabsBridge['reorderTab']>[0]) => state.tabs[0]!),
  }
  window.voxariumTabs = bridge as unknown as DesktopTabsBridge
  const current = shallowRef(state), roots = shallowRef(pending), activate = vi.fn(), open = vi.fn()
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render: () => h(ProjectTabStrip, { state: current.value, pending: roots.value, onActivate: activate, onOpen: open }) })
  app.mount(host)
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount(); host.remove() } }
  cleanups.push(unmount)
  const tabs = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
  for (const node of tabs) {
    let capture: number | null = null
    node.setPointerCapture = vi.fn(id => { capture = id })
    node.hasPointerCapture = vi.fn(id => capture === id)
    node.releasePointerCapture = vi.fn(id => { capture = null; pointer(node, 'lostpointercapture', { pointerId: id }) })
  }
  return { bridge, current, roots, activate, open, host, tabs, unmount }
}
function pointer(node: Element, name: string, fields: Record<string, unknown> = {}) {
  const event = new MouseEvent(name, { bubbles: true, cancelable: true, button: 0, clientX: 60, clientY: 20, ...fields })
  for (const [key, value] of Object.entries({ pointerId: 7, isPrimary: true, pointerType: 'mouse', ...fields })) {
    if (!['button', 'clientX', 'clientY'].includes(key)) Object.defineProperty(event, key, { value })
  }
  node.dispatchEvent(event)
  return event
}

// Bridge/DOM integration only: not evidence of physical OS drag.
describe('production captured-pointer strip integration', () => {
  it('holds shell focus, arms before activation, and disarms a click before activation', async () => {
    const f = mountGestures()
    let disarm!: () => void
    f.bridge.cancelDrag.mockImplementationOnce(() => new Promise(resolve => { disarm = resolve }))
    expect(pointer(f.tabs[0]!, 'pointerdown').defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(f.tabs[0])
    expect(f.tabs[0]!.setPointerCapture).toHaveBeenCalledWith(7)
    expect(f.bridge.beginDrag).toHaveBeenCalledWith({ windowId: 'w', windowRevision: 1, tabId: 'a', tabRevision: 1, point: { x: 60, y: 20 } })
    expect(f.activate).not.toHaveBeenCalled()
    pointer(f.tabs[0]!, 'pointerup'); await flush()
    expect(f.tabs[0]!.releasePointerCapture).toHaveBeenCalledWith(7)
    expect(f.bridge.cancelDrag).not.toHaveBeenCalled() // Normal lost capture must not race native terminal.
    f.tabs[0]!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })); await flush()
    expect(f.bridge.cancelDrag).toHaveBeenCalledOnce()
    expect(f.activate).not.toHaveBeenCalled()
    disarm(); await flush()
    expect(f.activate).toHaveBeenCalledExactlyOnceWith('a')
  })

  it('never turns pointer-up, dragend or dropEffect=none into detach/reorder', async () => {
    const f = mountGestures()
    pointer(f.tabs[0]!, 'pointerdown')
    pointer(f.tabs[0]!, 'pointermove', { clientX: 650, clientY: 480 })
    pointer(f.tabs[0]!, 'pointerup', { clientX: 650, clientY: 480 })
    const dragend = new Event('dragend', { bubbles: true })
    Object.defineProperty(dragend, 'dataTransfer', { value: { dropEffect: 'none' } })
    f.tabs[0]!.dispatchEvent(dragend)
    f.tabs[0]!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })); await flush()
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    expect(f.bridge.reorderTab).not.toHaveBeenCalled()
    expect(f.bridge.cancelDrag).not.toHaveBeenCalled()
    const dragstart = new Event('dragstart', { bubbles: true, cancelable: true })
    f.tabs[0]!.dispatchEvent(dragstart)
    expect(dragstart.defaultPrevented).toBe(true)
  })

  it.each(['Escape', 'pointercancel', 'lostpointercapture', 'unmount'])('cancels %s before delayed arm IPC returns', async reason => {
    const f = mountGestures()
    let arm!: (value: { token: string; expiresAt: number }) => void
    f.bridge.beginDrag.mockReturnValueOnce(new Promise(resolve => { arm = resolve }))
    pointer(f.tabs[0]!, 'pointerdown')
    if (reason === 'Escape') document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    else if (reason === 'unmount') f.unmount()
    else pointer(f.tabs[0]!, reason)
    arm({ token: 'late', expiresAt: Date.now() + 30_000 }); await flush()
    expect(f.bridge.cancelDrag).toHaveBeenCalledExactlyOnceWith({ windowId: 'w', windowRevision: 1, token: 'late' })
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    expect(f.activate).not.toHaveBeenCalled()
  })

  it('blocks pending/crashed tabs, secondary pointers/buttons and touch', async () => {
    const f = mountGestures(['D:/Projects/a'])
    pointer(f.tabs[0]!, 'pointerdown'); pointer(f.tabs[1]!, 'pointerdown')
    f.roots.value = []; await flush()
    pointer(f.tabs[0]!, 'pointerdown', { button: 2 })
    pointer(f.tabs[0]!, 'pointerdown', { isPrimary: false })
    pointer(f.tabs[0]!, 'pointerdown', { pointerType: 'touch' })
    expect(f.bridge.beginDrag).not.toHaveBeenCalled()
    expect(f.host.querySelector('[data-detach-tab-id="b"]')?.hasAttribute('disabled')).toBe(true)
  })

  it('reports clipped client-CSS geometry, revisions and no open/detach hit zones', async () => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const x = this.getAttribute('data-tab-id') === 'a' ? -20 : this.getAttribute('data-tab-id') === 'b' ? 168 : this.classList.contains('project-tab-strip__open') ? 280 : 0
      const width = this.hasAttribute('data-tab-id') ? 160 : this.classList.contains('project-tab-strip__open') ? 44 : this.classList.contains('project-tab-strip') ? 600 : 280
      return { x, y: 0, width, height: 44, left: x, top: 0, right: x + width, bottom: 44, toJSON() {} }
    })
    const f = mountGestures(); await flush()
    expect(f.bridge.setDragGeometry).toHaveBeenLastCalledWith({ windowId: 'w', windowRevision: 1, appendStart: 324, appendEnd: 600, tabs: [
      { tabId: 'a', x: 0, y: 0, width: 140, height: 44 }, { tabId: 'b', x: 168, y: 0, width: 112, height: 44 },
    ] })
    expect(f.host.querySelector('[data-detach-tab-id]')?.getAttribute('role')).not.toBe('tab')
    f.current.value = { ...state, window: { ...state.window, revision: 9 } }; await flush()
    expect(f.bridge.setDragGeometry.mock.calls.at(-1)?.[0]).toMatchObject({ windowRevision: 9 })
    const count = f.bridge.setDragGeometry.mock.calls.length
    f.host.querySelector('[role="tablist"]')!.dispatchEvent(new Event('scroll')); await flush()
    window.dispatchEvent(new Event('resize')); await flush()
    expect(f.bridge.setDragGeometry.mock.calls.length).toBeGreaterThan(count + 1)
    const source = readFileSync(resolve(process.cwd(), 'src/components/layout/ProjectTabStrip.vue'), 'utf8')
    expect(source).toMatch(/\.project-tab-strip button\s*\{[^}]*-webkit-app-region: no-drag/)
  })

  it('cancels capture on main revision and pending-operation changes', async () => {
    const f = mountGestures()
    pointer(f.tabs[0]!, 'pointerdown')
    f.current.value = { ...state, window: { ...state.window, revision: 2 } }; await flush()
    expect(f.bridge.cancelDrag).toHaveBeenCalledOnce()
    expect(f.tabs[0]!.releasePointerCapture).toHaveBeenCalledWith(7)
    pointer(f.tabs[0]!, 'pointerdown')
    f.roots.value = ['a']; await flush()
    expect(f.bridge.cancelDrag).toHaveBeenCalledTimes(2)
    expect(f.activate).not.toHaveBeenCalled()
  })

  it('provides an in-strip localized keyboard/context detach command with revisions', async () => {
    const f = mountGestures()
    const detach = f.host.querySelector<HTMLButtonElement>('[data-detach-tab-id="a"]')!
    f.tabs[0]!.focus()
    f.tabs[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(detach)
    expect(detach.getAttribute('aria-label')).toBe('desktop.tabs.detach: a')
    expect(f.host.querySelector('[role="menu"], [role="dialog"]')).toBeNull()
    pointer(detach, 'pointerdown'); detach.click(); await flush()
    expect(f.bridge.beginDrag).not.toHaveBeenCalled()
    expect(f.bridge.detachTab).toHaveBeenCalledExactlyOnceWith({ windowId: 'w', windowRevision: 1, tabId: 'a', tabRevision: 1 })
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.current.value).toBe(state)
    f.tabs[0]!.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(detach)
  })

  it('closes with accessible inline controls only after native gesture cancellation', async () => {
    const f = mountGestures()
    const close = f.host.querySelector<HTMLButtonElement>('[data-close-tab-id="a"]')!
    expect(close.getAttribute('aria-label')).toBe('desktop.tabs.close: a')
    expect(close.getAttribute('title')).toBe('desktop.tabs.close')
    expect(close.getAttribute('role')).not.toBe('tab')
    close.focus(); expect(document.activeElement).toBe(close)
    pointer(f.tabs[0]!, 'pointerdown')
    let disarm!: () => void
    f.bridge.cancelDrag.mockImplementationOnce(() => new Promise(resolve => { disarm = resolve }))
    close.click(); await flush()
    expect(f.tabs[0]!.releasePointerCapture).toHaveBeenCalledWith(7)
    expect(f.bridge.closeTab).not.toHaveBeenCalled()
    disarm(); await flush()
    expect(f.bridge.closeTab).toHaveBeenCalledExactlyOnceWith({ windowId: 'w', windowRevision: 1, tabId: 'a', tabRevision: 1 })
    expect(f.activate).not.toHaveBeenCalled()
    expect(f.bridge.detachTab).not.toHaveBeenCalled()
    expect(f.current.value).toBe(state)
  })

  it('preserves tabs and reports close cancellation or transport failure', async () => {
    const f = mountGestures()
    const close = f.host.querySelector<HTMLButtonElement>('[data-close-tab-id="a"]')!
    f.bridge.closeTab.mockResolvedValueOnce({ closed: false })
    close.click(); await flush()
    expect(f.host.querySelector('[role="alert"]')?.textContent).toBe('desktop.tabs.error')
    f.bridge.closeTab.mockRejectedValueOnce(new Error('lost transport'))
    close.click(); await flush()
    expect(f.bridge.closeTab).toHaveBeenCalledTimes(2)
    expect(f.current.value).toBe(state)
    f.roots.value = ['a']; await flush()
    expect(close.disabled).toBe(true)
  })

  it('reorders by keyboard and displays localized native errors without topology changes', async () => {
    const f = mountGestures()
    f.tabs[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true, bubbles: true, cancelable: true }))
    await flush()
    expect(f.bridge.reorderTab).toHaveBeenCalledExactlyOnceWith({ windowId: 'w', windowRevision: 1, tabId: 'a', tabRevision: 1, index: 1 })
    expect(f.activate).not.toHaveBeenCalled()
    f.bridge.detachTab.mockRejectedValueOnce(new Error('native attach failed'))
    f.host.querySelector<HTMLButtonElement>('[data-detach-tab-id="a"]')!.click(); await flush()
    expect(f.host.querySelector('[role="alert"]')?.textContent).toBe('desktop.tabs.error')
    expect(f.current.value).toBe(state)
  })
})


describe('production close preload bridge', () => {
  it('uses fixed close channels, hides IPC events, and removes the exact subscription', async () => {
    const exposed: Record<string, unknown> = {}
    const ipcRenderer = { invoke: vi.fn(async () => undefined), on: vi.fn((_channel: string, _listener: (...args: unknown[]) => void) => {}), removeListener: vi.fn() }
    runInNewContext(readFileSync(resolve(process.cwd(), '../desktop/preload.cjs'), 'utf8'), {
      require: () => ({ contextBridge: { exposeInMainWorld: (name: string, value: unknown) => { exposed[name] = value } }, ipcRenderer }),
    })
    const bridge = exposed.voxariumTabs as DesktopTabsBridge
    const request = { windowId: 'w', windowRevision: 1, tabId: 'a', tabRevision: 1 }
    await bridge.closeTab!(request)
    expect(ipcRenderer.invoke).toHaveBeenLastCalledWith('studio:tabs-close', request)
    const callback = vi.fn()
    const unsubscribe = bridge.onPrepareClose!(callback)
    const [channel, listener] = ipcRenderer.on.mock.calls.at(-1)!
    const closeRequest = { requestId: 'close-1', tabId: 'a', tabRevision: 1, project_id: 'a', epoch: 'e', phase: 'prepare' as const }
    listener({ sender: 'not exposed' }, closeRequest)
    expect(callback).toHaveBeenCalledExactlyOnceWith(closeRequest)
    expect(channel).toBe('studio:tabs-close-request')
    const ack = { ...closeRequest, prepared: false }
    await bridge.acknowledgeClose!(ack)
    expect(ipcRenderer.invoke).toHaveBeenLastCalledWith('studio:tabs-close-ack', ack)
    unsubscribe()
    expect(ipcRenderer.removeListener).toHaveBeenCalledExactlyOnceWith(channel, listener)
    expect(Object.isFrozen(bridge)).toBe(true)
  })
})
