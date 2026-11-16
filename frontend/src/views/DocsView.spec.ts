// @vitest-environment jsdom
/** Mounted Documents close/transfer invariants (dev spec §5.3, block-editor spec §8.2).
 * Real BlockEditor, ProseMirror, Yjs room and docs store; no native Electron transfer claim.
 */
import type { Editor } from '@tiptap/core'
import { Slice } from '@tiptap/pm/model'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createApp, nextTick, type App, type Ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import type { DocDetail, SyncStatus } from '@/api/types'
import { ApiError } from '@/api/client'
import { rendererCloseBarrier } from '@/desktop/close-barrier'
import { seedUpdate } from '@/lib/collab/seed'
import { CollabSession, type SocketLike } from '@/lib/collab/session'
import { useDocsStore } from '@/stores/docs'
import { useSyncStore } from '@/stores/sync'

const api = vi.hoisted(() => ({
  docs: vi.fn(), doc: vi.fn(), docFolders: vi.fn(), docsGraph: vi.fn(),
  saveDoc: vi.fn(), uploadDocAsset: vi.fn(),
}))
vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  api: new Proxy({}, { get: (_, name: string) => api[name as keyof typeof api] }),
  uploadDocAsset: api.uploadDocAsset,
}))
vi.mock('@/ws/connection', () => ({ onEvent: vi.fn(() => () => {}) }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ project: null }) }))
vi.mock('@/stores/vcs', () => ({
  useVcsStore: () => ({ subscribe: vi.fn(), refresh: vi.fn() }),
}))

// Observe, do not replace, Tiptap's real useEditor and lifecycle.
const captured = vi.hoisted(() => ({ editors: [] as Ref<Editor | undefined>[] }))
vi.mock('@tiptap/vue-3', async importOriginal => {
  const actual = await importOriginal<typeof import('@tiptap/vue-3')>()
  return { ...actual, useEditor: (...args: Parameters<typeof actual.useEditor>) => {
    const editor = actual.useEditor(...args)
    captured.editors.push(editor)
    return editor
  } }
})
const roomApi = vi.hoisted(() => ({ open: vi.fn() }))
vi.mock('@/lib/collab/session', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/collab/session')>(),
  openSession: roomApi.open,
}))

// Keep the actual document/editor stack; unrelated navigation/panel furniture is inert.
vi.mock('@/components/layout/SidePane.vue', async () => {
  const { h, defineComponent } = await import('vue')
  return { default: defineComponent({ setup: (_, { slots }) =>
    () => h('aside', [slots.actions?.(), slots.top?.(), slots.default?.()]) }) }
})
vi.mock('@/components/docs/DocTree.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/docs/DocSidePanel.vue', () => ({ default: { render: () => null } }))

const { default: DocsView } = await import('./DocsView.vue')
const PATH = 'one.md'
const SOURCE = '# Document\n\nA paragraph.\n'

function detail(path = PATH, content = SOURCE): DocDetail {
  return { path, title: 'Document', size_bytes: content.length,
    updated_at: '2026-11-13T10:00:00+00:00', content, links: [], backlinks: [] }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
async function flush() {
  for (let i = 0; i < 12; i++) { await Promise.resolve(); await nextTick() }
}

let app: App | undefined
let pinia: Pinia
let root: HTMLElement
let rooms: CollabSession[]
let extraGuards: (() => void)[]

beforeEach(() => {
  vi.useFakeTimers()
  for (const mock of Object.values(api)) mock.mockReset()
  captured.editors = []
  roomApi.open.mockReset()
  api.docs.mockResolvedValue([detail()])
  api.doc.mockImplementation(async (path: string) => detail(path))
  api.docFolders.mockResolvedValue([])
  api.docsGraph.mockResolvedValue({ nodes: [], edges: [] })
  api.saveDoc.mockImplementation(async (path: string, content: string) => detail(path, content))
  pinia = createPinia()
  setActivePinia(pinia)
  root = document.body.appendChild(document.createElement('div'))
  rooms = []
  extraGuards = []
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})

afterEach(async () => {
  extraGuards.forEach(off => off())
  app?.unmount()
  app = undefined
  await flush()
  await useDocsStore(pinia).settle()
  await flush()
  // close() is idempotent; leave() normally already called it after the editor unmounted.
  rooms.forEach(room => room.close())
  disposePinia(pinia)
  document.body.replaceChildren()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function liveRoom(scribe = true, aheadAtJoin = false) {
  const doc = new Y.Doc()
  Y.applyUpdate(doc, seedUpdate(SOURCE))
  const socket: SocketLike = {
    send: vi.fn(), close: vi.fn(), readyState: 1,
    onopen: null, onmessage: null, onclose: null,
  }
  const room = new CollabSession(PATH, 'mounted-room',
    { id: 'alice', name: 'Alice', color: '#123456' }, doc, socket, scribe, aheadAtJoin)
  rooms.push(room)
  const close = vi.spyOn(room, 'close')
  return { room, socket, close }
}

async function mount(room?: CollabSession) {
  if (room) {
    useSyncStore(pinia).status = { state: 'connected' } as SyncStatus
    roomApi.open.mockResolvedValue(room)
  }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/docs', name: 'docs', component: DocsView },
  ] })
  await router.push({ name: 'docs', query: { path: PATH } })
  app = createApp(DocsView).use(pinia).use(router)
  app.mount(root)
  await flush()
  await vi.advanceTimersByTimeAsync(20)
  await flush()
  expect(root.querySelector('.ProseMirror')?.textContent).toContain('A paragraph.')
  return useDocsStore(pinia)
}

function editor(): Editor {
  const instance = captured.editors.at(-1)?.value
  expect(instance).toBeDefined()
  expect(instance!.isDestroyed).toBe(false)
  return instance!
}
function edit(text = 'Edited ') {
  editor().commands.insertContentAt(1, text)
}
function blur(relatedTarget: EventTarget | null = null) {
  editor().view.dom.dispatchEvent(new FocusEvent('blur', { relatedTarget }))
}
function drop(file: File, location: 'content' | 'margin') {
  const instance = editor()
  vi.spyOn(instance.view, 'posAtCoords').mockReturnValue({ pos: 1, inside: -1 })
  const event = new MouseEvent('drop', { bubbles: true, cancelable: true, clientX: 10, clientY: 10 })
  Object.defineProperty(event, 'dataTransfer', { value: { files: [file], types: ['Files'] } })
  if (location === 'margin') {
    root.querySelector('.block-editor-scroller')!.dispatchEvent(event)
  } else {
    instance.view.someProp('handleDrop', handle => handle(instance.view, event as DragEvent, Slice.empty, false))
  }
}

describe('mounted Documents close and live-view invariants (§5.3)', () => {
  it('opens a clean real collab editor and reparents/focuses it without Markdown writes or serialization', async () => {
    const { room, socket, close } = liveRoom()
    const store = await mount(room)
    const instance = editor()
    const dom = instance.view.dom
    const json = vi.spyOn(instance, 'getJSON')
    const update = vi.fn()
    room.doc.on('update', update)
    const otherHost = document.body.appendChild(document.createElement('section'))
    // Mounted invariant only: reparent the same live subtree; no native Electron claim.
    otherHost.appendChild(root)
    vi.mocked(document.hasFocus).mockReturnValue(false)
    window.dispatchEvent(new FocusEvent('blur'))
    blur()
    document.body.prepend(root)
    window.dispatchEvent(new FocusEvent('focus'))
    vi.mocked(document.hasFocus).mockReturnValue(true)
    await flush()

    expect(editor()).toBe(instance)
    expect(root.querySelector('.ProseMirror')).toBe(dom)
    expect(store.live).toBe(room.sid)
    expect(close).not.toHaveBeenCalled()
    expect(update).not.toHaveBeenCalled()
    expect(json).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(vi.mocked(socket.send).mock.calls.filter(([frame]) => JSON.parse(frame).t === 'update')).toEqual([])
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
  })

  it('waits for the real solo editor save and retains its DOM until the delayed write finishes', async () => {
    const store = await mount()
    const instance = editor()
    const saved = deferred<DocDetail>()
    api.saveDoc.mockReturnValueOnce(saved.promise)
    edit()
    expect(store.dirty).toBe(true)
    let completed = false
    const closing = rendererCloseBarrier.prepare().then(value => { completed = true; return value })
    await flush()
    expect(completed).toBe(false)
    expect(store.saving).toBe(true)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(editor()).toBe(instance)
    expect(root.contains(instance.view.dom)).toBe(true)
    saved.resolve(detail(PATH, api.saveDoc.mock.calls[0][1]))
    expect(await closing).toBe(true)
    expect(store.dirty).toBe(false)
    expect(editor()).toBe(instance)
    expect(rendererCloseBarrier.validate()).toBe(true)
  })

  it.each([409, 500])('denies close after a deferred %s save failure without discarding the real editor', async status => {
    const store = await mount()
    const saved = deferred<DocDetail>()
    api.saveDoc.mockReturnValueOnce(saved.promise)
    edit('Only on screen ')
    const instance = editor()
    const closing = rendererCloseBarrier.prepare()
    await flush()
    saved.reject(new ApiError(status, 'write refused'))
    expect(await closing).toBe(false)
    expect(editor()).toBe(instance)
    expect(instance.getText()).toContain('Only on screen ')
    expect(store.dirty).toBe(true)
    expect(store.saveState).toBe('failed')
    expect(store.saveError).not.toBeNull()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
  })

  it('revalidates a late editor edit while another mounted form is still saving', async () => {
    const store = await mount()
    const other = deferred<boolean>()
    const waiting = vi.fn(() => other.promise)
    extraGuards.push(rendererCloseBarrier.register(waiting))
    const closing = rendererCloseBarrier.prepare()
    await flush()
    expect(waiting).toHaveBeenCalledTimes(1)
    edit('Late edit ')
    other.resolve(true)
    expect(await closing).toBe(false)
    expect(store.dirty).toBe(true)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(editor().getText()).toContain('Late edit ')
  })

  it('invalidates an approved snapshot when the open document/editor changes', async () => {
    const store = await mount()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    const original = editor()
    await store.select('other.md')
    await flush()
    await vi.advanceTimersByTimeAsync(0)
    expect(editor()).not.toBe(original)
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it.each(['saveError', 'upload'] as const)('revalidates late %s state while another form is saving', async state => {
    const store = await mount()
    const other = deferred<boolean>()
    const waiting = vi.fn(() => other.promise)
    extraGuards.push(rendererCloseBarrier.register(waiting))
    const closing = rendererCloseBarrier.prepare()
    await flush()
    expect(waiting).toHaveBeenCalledTimes(1)
    const uploaded = deferred<{ src: string; alt: string }>()
    if (state === 'saveError') store.saveError = 'A late failure'
    else {
      api.uploadDocAsset.mockReturnValueOnce(uploaded.promise)
      drop(new File(['image'], 'late.png', { type: 'image/png' }), 'content')
    }
    other.resolve(true)
    expect(await closing).toBe(false)
    expect(api.saveDoc).not.toHaveBeenCalled()
    if (state === 'upload') {
      uploaded.reject(new Error('upload refused'))
      await flush()
      await vi.advanceTimersByTimeAsync(0)
      expect(store.error).toContain('upload refused')
      expect(api.saveDoc).not.toHaveBeenCalled()
      expect(await rendererCloseBarrier.prepare()).toBe(false)
      expect(api.saveDoc).not.toHaveBeenCalled()
    }
  })

  it.each(['content', 'margin'] as const)('denies close during a pending %s upload, then saves the inserted image', async location => {
    await mount()
    const uploaded = deferred<{ src: string; alt: string }>()
    api.uploadDocAsset.mockReturnValueOnce(uploaded.promise)
    const instance = editor()
    const file = new File(['image'], 'picture.png', { type: 'image/png' })
    drop(file, location)
    expect(api.uploadDocAsset).toHaveBeenCalledWith(PATH, file)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(editor()).toBe(instance)
    uploaded.resolve({ src: 'picture.png', alt: 'Picture' })
    await flush()
    await vi.advanceTimersByTimeAsync(0)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(api.saveDoc.mock.calls[0][1]).toContain('![Picture](picture.png)')
    expect(editor()).toBe(instance)
  })

  it('does not flush a dirty editor on native-window blur/reparent, but keeps intra-document blur autosave', async () => {
    const store = await mount()
    edit('Pending ')
    const instance = editor()
    const json = vi.spyOn(instance, 'getJSON')
    vi.mocked(document.hasFocus).mockReturnValue(false)
    blur()
    const otherHost = document.body.appendChild(document.createElement('section'))
    otherHost.appendChild(root)
    window.dispatchEvent(new FocusEvent('blur'))
    window.dispatchEvent(new FocusEvent('focus'))
    await flush()
    expect(store.dirty).toBe(true)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(json).not.toHaveBeenCalled()
    expect(editor()).toBe(instance)

    // A target in a different document must not count as an intra-renderer focus change,
    // even if hasFocus() has not reflected the native transition yet.
    vi.mocked(document.hasFocus).mockReturnValue(true)
    blur() // Native transition before hasFocus() has changed: no known local target.
    blur(document.implementation.createHTMLDocument().body)
    await flush()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(json).not.toHaveBeenCalled()
    const within = root.querySelector('button')!
    blur(within)
    await flush()
    await store.settle()
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(json).toHaveBeenCalled()
    expect(store.dirty).toBe(false)
  })
})
