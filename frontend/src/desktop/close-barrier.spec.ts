// @vitest-environment jsdom
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DesktopTabsState } from '@/desktop'
import {
  connectRendererCloseBarrier, createCloseBarrier, rendererCloseBarrier, useCloseGuard,
  type RendererCloseAck, type RendererCloseBridge, type RendererCloseRequest,
} from './close-barrier'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
async function flush() {
  for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick() }
}

const request: RendererCloseRequest = {
  requestId: 'close-1', tabId: 'tab-1', tabRevision: 4, project_id: 'project-A', epoch: 'epoch-1', phase: 'prepare',
}
function projectState(): DesktopTabsState {
  const binding = { root: 'D:/A', project_id: 'project-A', epoch: 'epoch-1' }
  return { role: 'project', binding, windowId: '1', windowRevision: 2,
    tab: { tabId: 'tab-1', project: binding, ownerWindowId: '1', state: 'ready', revision: 4, transferGeneration: 0 } }
}
function transport() {
  let state = projectState()
  let listener: ((request: RendererCloseRequest) => void) | undefined
  const acknowledgeClose = vi.fn(async (_ack: RendererCloseAck) => {})
  const unsubscribe = vi.fn(() => { listener = undefined })
  const bridge: RendererCloseBridge = {
    getState: vi.fn(async () => state), acknowledgeClose,
    onPrepareClose: callback => { listener = callback; return unsubscribe },
  }
  return { bridge, acknowledgeClose, unsubscribe,
    send: (value = request) => listener?.(value),
    setState: (value: DesktopTabsState) => { state = value },
  }
}

let app: App | undefined
afterEach(() => { app?.unmount(); app = undefined; document.body.replaceChildren(); vi.restoreAllMocks() })

describe('renderer close barrier (§5.3.5)', () => {
  it('coalesces repeated prepares and fails closed for errors/non-true results', async () => {
    const barrier = createCloseBarrier()
    const save = deferred<boolean>()
    const guard = vi.fn(() => save.promise)
    const off = barrier.register(guard)
    const first = barrier.prepare()
    expect(barrier.prepare()).toBe(first)
    await flush()
    expect(guard).toHaveBeenCalledTimes(1)
    save.reject(new Error('save failed'))
    expect(await first).toBe(false)
    off()
    barrier.register(() => false)
    expect(await barrier.prepare()).toBe(false)
  })

  it('does not approve a changed set of mounted editors while awaiting a save', async () => {
    const barrier = createCloseBarrier()
    const save = deferred<boolean>()
    const off = barrier.register(() => save.promise)
    const pending = barrier.prepare()
    await flush()
    off()
    barrier.register(() => true)
    save.resolve(true)
    expect(await pending).toBe(false)
  })

  it('rechecks an earlier editor after another guard delays the transaction', async () => {
    const barrier = createCloseBarrier()
    let dirty = false
    barrier.register(() => true, () => !dirty)
    const save = deferred<boolean>()
    barrier.register(() => save.promise)
    const pending = barrier.prepare()
    await flush()
    dirty = true
    save.resolve(true)
    expect(await pending).toBe(false)
  })

  it('rechecks new edits after the final asynchronous native identity lookup', async () => {
    const barrier = createCloseBarrier()
    let dirty = false
    barrier.register(() => true, () => !dirty)
    const ipc = transport()
    const lookup = deferred<DesktopTabsState>()
    vi.mocked(ipc.bridge.getState).mockResolvedValueOnce(projectState())
      .mockResolvedValueOnce(projectState()).mockReturnValueOnce(lookup.promise)
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send(); await flush()
    dirty = true
    lookup.resolve(projectState()); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenCalledExactlyOnceWith({ ...request, prepared: false })
    connection.dispose()
  })

  it('registers mounted guards, preserves cancel state, and unregisters on unmount', async () => {
    const dirty = ref(false)
    const confirm = vi.fn(() => false)
    app = createApp(defineComponent({ setup() {
      useCloseGuard(() => !dirty.value || confirm())
      return () => h('textarea', { value: dirty.value ? 'draft' : '', onInput: () => { dirty.value = true } })
    } }))
    const root = document.body.appendChild(document.createElement('div'))
    app.mount(root)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    root.querySelector('textarea')!.dispatchEvent(new Event('input'))
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(dirty.value).toBe(true)
    expect(confirm).toHaveBeenCalledTimes(1)
    confirm.mockReturnValue(true)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    app.unmount(); app = undefined
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('ACKs only after all saves finish, with the original request identity', async () => {
    const barrier = createCloseBarrier()
    const save = deferred<boolean>()
    const guard = vi.fn(() => save.promise)
    barrier.register(guard)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send(); ipc.send()
    await flush()
    expect(ipc.acknowledgeClose).not.toHaveBeenCalled()
    expect(guard).toHaveBeenCalledTimes(1)
    save.resolve(true)
    await flush()
    expect(ipc.acknowledgeClose).toHaveBeenCalledExactlyOnceWith({ ...request, prepared: true })
    ipc.send(); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenCalledTimes(1)
    connection.dispose()
  })

  it('keeps cancelled/failed saves negative and allows a distinct retry', async () => {
    const barrier = createCloseBarrier()
    const guard = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(true)
    barrier.register(guard)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send(); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenLastCalledWith({ ...request, prepared: false })
    ipc.send({ ...request, requestId: 'retry' }); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenLastCalledWith({ ...request, requestId: 'retry', prepared: true })
    connection.dispose()
  })

  it.each(['tab', 'revision', 'epoch'] as const)('cannot turn a late ACK into permission for a new %s', async changed => {
    const barrier = createCloseBarrier()
    const save = deferred<boolean>()
    barrier.register(() => save.promise)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send(); await flush()
    const state = projectState()
    if (state.role !== 'project') throw new Error('fixture')
    ipc.setState({ ...state, binding: { ...state.binding, epoch: changed === 'epoch' ? 'epoch-2' : 'epoch-1' },
      tab: { ...state.tab, tabId: changed === 'tab' ? 'tab-2' : 'tab-1', revision: changed === 'revision' ? 5 : 4 } })
    save.resolve(true); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenCalledExactlyOnceWith({ ...request, prepared: false })
    // Even a fresh request for the replacement cannot resurrect this old renderer's rights.
    ipc.send({ ...request, requestId: 'new', tabId: 'tab-2', tabRevision: 5, epoch: 'epoch-2' }); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenLastCalledWith(expect.objectContaining({ requestId: 'new', prepared: false }))
    connection.dispose()
  })

  it('ignores foreign/malformed requests without running guards', async () => {
    const barrier = createCloseBarrier()
    const guard = vi.fn(() => true)
    barrier.register(guard)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send({ ...request, tabId: 'foreign' }); await flush()
    expect(guard).not.toHaveBeenCalled()
    expect(ipc.acknowledgeClose).toHaveBeenLastCalledWith(expect.objectContaining({ prepared: false }))
    ipc.send({ ...request, tabRevision: NaN }); await flush()
    expect(ipc.acknowledgeClose).toHaveBeenCalledTimes(1)
    connection.dispose()
  })

  it('does not flush/prepare on focus, owner transfer or reorder state changes', async () => {
    const barrier = createCloseBarrier()
    const guard = vi.fn(() => true)
    barrier.register(guard)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    const state = projectState()
    if (state.role !== 'project') throw new Error('fixture')
    ipc.setState({ ...state, windowId: '2', windowRevision: 9,
      tab: { ...state.tab, ownerWindowId: '2', transferGeneration: 1, revision: 8 } })
    window.dispatchEvent(new Event('focus'))
    await flush()
    expect(guard).not.toHaveBeenCalled()
    expect(ipc.acknowledgeClose).not.toHaveBeenCalled()
    connection.dispose()
  })

  it('drops late results and unsubscribes on App disposal', async () => {
    const barrier = createCloseBarrier()
    const save = deferred<boolean>()
    barrier.register(() => save.promise)
    const ipc = transport()
    const connection = connectRendererCloseBarrier(ipc.bridge, barrier)
    await connection.ready
    ipc.send(); await flush()
    connection.dispose()
    save.resolve(true); await flush()
    expect(ipc.acknowledgeClose).not.toHaveBeenCalled()
    expect(ipc.unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('does not fabricate ACK transport in a browser or the old main seam', async () => {
    await connectRendererCloseBarrier(undefined).ready
    const getState = vi.fn(async () => projectState())
    await connectRendererCloseBarrier({ getState }).ready
    expect(getState).not.toHaveBeenCalled()
  })
})
