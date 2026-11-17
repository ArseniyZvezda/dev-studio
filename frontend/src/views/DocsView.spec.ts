// @vitest-environment jsdom
/** Mounted Documents close/transfer invariants (dev spec §5.3, block-editor spec §8.2).
 * Real BlockEditor, ProseMirror, Yjs room and docs store; no native Electron transfer claim.
 */
import type { Editor } from '@tiptap/core'
import { Slice } from '@tiptap/pm/model'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createApp, h, nextTick, shallowRef, type App, type Ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import type { DocDetail, DocumentFence, ProjectRemoteEditorLease, ProjectRemoteStatus, SyncStatus } from '@/api/types'
import { ProjectSession, rendererSession } from '@/session/project-session'
import type { RemoteBindingChange } from '@/ws/events'
import { ApiError } from '@/api/client'
import { rendererCloseBarrier } from '@/desktop/close-barrier'
import { seedUpdate } from '@/lib/collab/seed'
import { CollabSession, toBase64, type OpenOptions, type SocketLike } from '@/lib/collab/session'
import BlockEditor from '@/components/docs/BlockEditor.vue'
import { useDocsStore } from '@/stores/docs'
import { useSyncStore } from '@/stores/sync'

const api = vi.hoisted(() => ({
  docs: vi.fn(), doc: vi.fn(), docFolders: vi.fn(), docsGraph: vi.fn(),
  saveDoc: vi.fn(), uploadDocAsset: vi.fn(),
  registerRemoteEditor: vi.fn(), heartbeatRemoteEditor: vi.fn(), deleteRemoteEditor: vi.fn(),
  ackRemoteEditor: vi.fn(), projectRemote: vi.fn(),
  materializeRemoteEditor: vi.fn(), confirmRemoteEditorMaterialization: vi.fn(),
  moveDoc: vi.fn(),
}))
const apiTransport = vi.hoisted(() => ({ realProof: false }))
vi.mock('@/api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual,
    api: new Proxy({}, { get: (_, name: string) => api[name as keyof typeof api] }),
    createApiClient: (...args: Parameters<typeof actual.createApiClient>) => apiTransport.realProof
      ? actual.createApiClient(...args) : ({
      ...actual.createApiClient(...args), materializeRemoteEditor: api.materializeRemoteEditor,
      confirmRemoteEditorMaterialization: api.confirmRemoteEditorMaterialization,
    }),
    uploadDocAsset: api.uploadDocAsset,
  }
})
const pageTarget = vi.hoisted(() => ({
  apply: null as null | Parameters<typeof import('@/desktop/page-state')['useDesktopPageTarget']>[1],
}))
vi.mock('@/desktop/page-state', async importOriginal => {
  const actual = await importOriginal<typeof import('@/desktop/page-state')>()
  return { ...actual, useDesktopPageTarget: (...args: Parameters<typeof actual.useDesktopPageTarget>) => {
    pageTarget.apply = args[1]
    actual.useDesktopPageTarget(...args)
  } }
})
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
let blockApps: App[]
let extraPinia: Pinia[]

beforeEach(() => {
  vi.useFakeTimers()
  for (const mock of Object.values(api)) mock.mockReset()
  captured.editors = []
  roomApi.open.mockReset()
  apiTransport.realProof = false
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
  blockApps = []
  extraPinia = []
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})

afterEach(async () => {
  extraGuards.forEach(off => off())
  blockApps.forEach(block => block.unmount())
  app?.unmount()
  app = undefined
  await flush()
  await useDocsStore(pinia).settle()
  for (const peer of extraPinia) await useDocsStore(peer).settle()
  await flush()
  // close() is idempotent; leave() normally already called it after the editor unmounted.
  rooms.forEach(room => room.close())
  disposePinia(pinia)
  extraPinia.forEach(disposePinia)
  document.body.replaceChildren()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function liveRoom(scribe = true, aheadAtJoin = false, initial?: Uint8Array) {
  const doc = new Y.Doc()
  Y.applyUpdate(doc, initial ?? seedUpdate(SOURCE))
  const socket: SocketLike = {
    send: vi.fn(), close: vi.fn(), readyState: 1,
    onopen: null, onmessage: null, onclose: null,
  }
  const room = new CollabSession(PATH, `mounted-room-${rooms.length}`,
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
  app = createApp({ render: () => h(RouterView) }).use(pinia).use(router)
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

/** Mount the real component directly to inspect its narrow I8 snapshot interface.
 * The relay below is local/synthetic; it says nothing about deployed Remote interop.
 */
async function mountedBlock(room?: CollabSession) {
  const exposed = shallowRef<InstanceType<typeof BlockEditor> | null>(null)
  const host = document.body.appendChild(document.createElement('div'))
  const dirty = vi.fn()
  const change = vi.fn()
  const block = createApp({ render: () => h(BlockEditor, {
    ref: exposed, source: SOURCE, path: PATH, documents: [], collab: room,
    onDirty: dirty, onChange: change,
  }) })
  blockApps.push(block)
  block.mount(host)
  await flush()
  await vi.advanceTimersByTimeAsync(20)
  const instance = captured.editors.at(-1)!.value!
  return { exposed: exposed.value!, instance, dirty, change, host }
}

describe('I8 frozen materialization from real mounted BlockEditors', () => {
  it('materializes BOTH mounted peers after the final scribe save and ACKs only their own receipts', async () => {
    const aliceRoom = liveRoom(true)
    const bobRoom = liveRoom(false, false, Y.encodeStateAsUpdate(aliceRoom.room.doc))
    const alice = await mountedBlock(aliceRoom.room)
    const bob = await mountedBlock(bobRoom.room)
    for (const [from, to] of [[aliceRoom, bobRoom], [bobRoom, aliceRoom]]) {
      vi.mocked(from.socket.send).mockImplementation(data => {
        const frame = JSON.parse(data)
        if (frame.t === 'update') to.room.receive(frame)
      })
    }
    bob.instance.commands.insertContentAt(1, 'Shared final ')
    alice.exposed.freeze(true); bob.exposed.freeze(true)
    expect(alice.instance.getText()).toContain('Shared final ')
    alice.exposed.flush(); bob.exposed.flush()
    expect(alice.change).toHaveBeenCalledTimes(1)
    expect(bob.change).not.toHaveBeenCalled()
    const final = alice.change.mock.calls[0][0] as string
    // A narrow synthetic local API, with the causal peer-save anchor modeled
    // explicitly. No server, production inventory or new Remote wire is claimed.
    const diskSave = deferred<DocDetail>()
    api.saveDoc.mockReturnValueOnce(diskSave.promise)
    const saving = api.saveDoc(PATH, final)
    const snapshots = [alice.exposed.captureMaterialization(), bob.exposed.captureMaterialization()]
    snapshots.forEach(snapshot => expect(snapshot.content).toBe(final))
    expect(bob.exposed.hasPendingWork()).toBe(true)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    diskSave.resolve(detail(PATH, final)); await saving
    let receipt = 50
    const receipts = new Map<string, number>()
    const pending = new Map<string, string>()
    api.materializeRemoteEditor.mockImplementation(async (id, body) => {
      const proof_id = `proof-${id}`; pending.set(id, proof_id)
      return { ...body, editor_id: id, binding_generation: 7, proof_id }
    })
    api.confirmRemoteEditorMaterialization.mockImplementation(async (id, body) => {
      expect(body.content).toBe(final)
      expect(body.proof_id).toBe(pending.get(id)); pending.delete(id)
      receipts.set(id, ++receipt)
      return { ...body, editor_id: id, binding_generation: 7, dirty: false, confirmed: true,
        last_saved_version: receipt }
    })
    api.ackRemoteEditor.mockImplementation(async (_change, ack) => {
      if (ack.last_saved_version !== receipts.get(ack.editor_id)) throw new ApiError(409, 'not own receipt', 'invalid_editor_ack')
    })
    for (const [index, name] of ['alice', 'bob'].entries()) {
      const member = index === 0 ? alice : bob
      const session = index === 0 ? aliceRoom.room : bobRoom.room
      const snapshot = snapshots[index]
      const document = member.instance.state.doc
      const dom = member.instance.view.dom
      const tuple = { backend_epoch: 'backend-a', expected_binding_generation: 7,
        document_path: PATH, client_instance_id: name, sid: session.sid, version: 1, revision: 4 }
      const ticket = await api.materializeRemoteEditor(`editor-${name}`, tuple)
      snapshot.assertCurrent()
      const confirmed = await api.confirmRemoteEditorMaterialization(`editor-${name}`,
        { ...tuple, proof_id: ticket.proof_id, content: snapshot.content })
      snapshot.accept()
      if (name === 'bob') await expect(api.ackRemoteEditor('change-a', {
        editor_id: 'editor-bob', old_generation: 7, last_saved_version: 51,
      })).rejects.toMatchObject({ code: 'invalid_editor_ack' })
      await api.ackRemoteEditor('change-a', { editor_id: `editor-${name}`, old_generation: 7,
        last_saved_version: confirmed.last_saved_version })
      expect(member.instance.state.doc).toBe(document); expect(member.instance.view.dom).toBe(dom)
      expect(session.doc.isDestroyed).toBe(false)
    }
    expect([...receipts.values()]).toEqual([51, 52])
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(aliceRoom.close).not.toHaveBeenCalled(); expect(bobRoom.close).not.toHaveBeenCalled()
  })

  it('reads two clean collab trees without writes, replacements or Yjs updates', async () => {
    const aliceRoom = liveRoom(true).room
    const bobRoom = liveRoom(false).room
    const alice = await mountedBlock(aliceRoom)
    const bob = await mountedBlock(bobRoom)
    const updates = [vi.fn(), vi.fn()]
    aliceRoom.doc.on('update', updates[0]); bobRoom.doc.on('update', updates[1])
    for (const member of [alice, bob]) {
      const document = member.instance.state.doc
      const dom = member.instance.view.dom
      const replace = vi.spyOn(member.instance.commands, 'setContent')
      member.exposed.freeze(true)
      const snapshot = member.exposed.captureMaterialization()
      expect(snapshot.content).toBe(SOURCE)
      snapshot.assertCurrent(); snapshot.accept()
      expect(member.instance.state.doc).toBe(document)
      expect(member.instance.view.dom).toBe(dom)
      expect(member.exposed.hasPendingWork()).toBe(false)
      expect(member.change).not.toHaveBeenCalled()
      expect(replace).not.toHaveBeenCalled()
    }
    await vi.advanceTimersByTimeAsync(800)
    updates.forEach(update => expect(update).not.toHaveBeenCalled())
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it('does not clear non-scribe roomAhead by reading; acceptance changes bookkeeping only', async () => {
    const room = liveRoom(false).room
    const bob = await mountedBlock(room)
    bob.instance.commands.insertContentAt(1, 'Non-scribe ')
    bob.exposed.freeze(true)
    const snapshot = bob.exposed.captureMaterialization()
    expect(snapshot.content).toContain('Non-scribe ')
    expect(bob.exposed.hasPendingWork()).toBe(true)
    const document = bob.instance.state.doc
    const json = bob.instance.getJSON()
    const updates = vi.fn(); room.doc.on('update', updates)
    snapshot.assertCurrent()
    expect(bob.exposed.hasPendingWork()).toBe(true)
    snapshot.accept()
    expect(bob.exposed.hasPendingWork()).toBe(false)
    expect(bob.instance.state.doc).toBe(document)
    expect(bob.instance.getJSON()).toEqual(json)
    expect(updates).not.toHaveBeenCalled()
    expect(bob.change).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it('rejects changed remote state even if the same Markdown is restored before confirm', async () => {
    const room = liveRoom(false).room
    const bob = await mountedBlock(room)
    bob.exposed.freeze(true)
    const snapshot = bob.exposed.captureMaterialization()
    const peer = new Y.Doc()
    Y.applyUpdate(peer, Y.encodeStateAsUpdate(room.doc))
    // A remote Yjs mutation outside the PM tree is invisible in Markdown but still
    // invalidates the room generation. Byte equality cannot resurrect the snapshot.
    peer.getMap('proof-race').set('revision', 1)
    const update = Y.encodeStateAsUpdate(peer, Y.encodeStateVector(room.doc))
    room.receive({ t: 'update', update: toBase64(update) })
    peer.destroy()
    expect(bob.exposed.captureMaterialization().content).toBe(snapshot.content)
    expect(() => snapshot.assertCurrent()).toThrow()
    expect(() => snapshot.accept()).toThrow()
    expect(bob.change).not.toHaveBeenCalled()
  })

  it('invalidates a snapshot for an exact duplicate update even when Yjs and PM emit no change', async () => {
    const room = liveRoom(false).room
    const member = await mountedBlock(room)
    member.exposed.freeze(true)
    const snapshot = member.exposed.captureMaterialization()
    const document = member.instance.state.doc
    const update = vi.fn(); room.doc.on('update', update)
    const revision = room.materializationRevision
    room.receive({ t: 'update', update: toBase64(Y.encodeStateAsUpdate(room.doc)) })
    expect(update).not.toHaveBeenCalled()
    expect(member.instance.state.doc).toBe(document)
    expect(room.materializationRevision).toBeGreaterThan(revision)
    expect(member.exposed.captureMaterialization().content).toBe(snapshot.content)
    expect(() => snapshot.assertCurrent()).toThrow()
    expect(() => snapshot.accept()).toThrow()
    expect(member.change).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it('rejects mutable, unflushed and destroyed editors without consuming their pending save', async () => {
    const member = await mountedBlock()
    expect(() => member.exposed.captureMaterialization()).toThrow()
    member.instance.commands.insertContentAt(1, 'Pending ')
    member.exposed.freeze(true)
    expect(() => member.exposed.captureMaterialization()).toThrow()
    expect(member.exposed.hasPendingWork()).toBe(true)
    expect(member.change).not.toHaveBeenCalled()
    member.exposed.flush()
    expect(member.change).toHaveBeenCalledTimes(1)
    const snapshot = member.exposed.captureMaterialization()
    member.instance.commands.insertContentAt(1, 'New ')
    expect(() => snapshot.assertCurrent()).toThrow()
    member.exposed.flush()
    const latest = member.exposed.captureMaterialization()
    blockApps.pop()!.unmount()
    expect(() => latest.accept()).toThrow()
  })

  it('rejects snapshots while an image upload or block drag is pending', async () => {
    const member = await mountedBlock()
    const uploaded = deferred<{ src: string; alt: string }>()
    api.uploadDocAsset.mockReturnValue(uploaded.promise)
    vi.spyOn(member.instance.view, 'posAtCoords').mockReturnValue({ pos: 1, inside: -1 })
    const event = new MouseEvent('drop', { bubbles: true, cancelable: true })
    Object.defineProperty(event, 'dataTransfer', { value: {
      files: [new File(['image'], 'pic.png', { type: 'image/png' })], types: ['Files'],
    } })
    member.instance.view.someProp('handleDrop', handle => handle(member.instance.view, event as DragEvent, Slice.empty, false))
    member.exposed.freeze(true)
    expect(() => member.exposed.captureMaterialization()).toThrow()
    uploaded.reject(new Error('upload refused'))
    await flush(); await vi.advanceTimersByTimeAsync(0)
    member.exposed.flush()
    expect(() => member.exposed.captureMaterialization()).not.toThrow()
    const snapshot = member.exposed.captureMaterialization()
    const dragging = member.instance.view.dragging
    member.instance.view.dragging = { slice: Slice.empty, move: true }
    // Native PM dragging is separate from the gutter composable's pointer drag.
    // The component must reject either one before proving a frozen tree.
    expect(() => member.exposed.captureMaterialization()).toThrow()
    expect(() => snapshot.assertCurrent()).toThrow()
    member.instance.view.dragging = dragging
  })
})

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

/** I5 lease/receipt fixture semantics, exercised through real mounted editors (I8).
 * No production inventory capability or cross-editor receipt is invented here.
 */
function scopedFixture() {
  vi.spyOn(rendererSession, 'isLegacy', 'get').mockReturnValue(false)
  vi.spyOn(rendererSession, 'scope', 'get').mockReturnValue({ project_id: 'ctx-a', epoch: 'runtime-a' })
  vi.spyOn(rendererSession, 'matches').mockImplementation(frame => frame.project_id === 'ctx-a' && frame.epoch === 'runtime-a')
  const remote = { project_id: 'ctx-a', epoch: 'runtime-a', backend_epoch: 'backend-a',
    binding_generation: 7, state: 'disabled', identity_revision: 1, persistent_id: 'id-a',
    config_fingerprint: 'f'.repeat(64), url: '', enabled: false, has_token: false,
    active_rooms: 0, pending_writes: 0 } satisfies ProjectRemoteStatus
  useSyncStore(pinia).remote = remote
  let row: ProjectRemoteEditorLease
  let sequence = 1
  let disk = SOURCE
  api.registerRemoteEditor.mockImplementation(async body => {
    row = { editor_id: 'editor-a', backend_epoch: body.backend_epoch, revision: 1,
      binding_generation: body.expected_binding_generation, version: 0, last_saved_version: sequence,
      document_path: body.document_path, client_instance_id: body.client_instance_id,
      lease_expires_at: Date.now() / 1000 + 30, dirty: false, confirmed: true,
      loaded_document: detail(body.document_path, api.saveDoc.mock.calls.at(-1)?.[1] ?? SOURCE) }
    return structuredClone(row)
  })
  api.heartbeatRemoteEditor.mockImplementation(async (_id, body) => {
    if (body.dirty === true) { row.dirty = true; row.confirmed = false; row.revision += 1 }
    if (body.version !== undefined) row.version = body.version
    return structuredClone(row)
  })
  api.saveDoc.mockImplementation(async (path, content, _author, _updated, _sid, fence: DocumentFence) => {
    if (fence.expected_binding_generation !== useSyncStore(pinia).remote?.binding_generation)
      throw new ApiError(409, 'old generation', 'stale_binding_generation')
    row.version = fence.version!
    disk = content
    row.revision += 1
    row.last_saved_version = ++sequence
    row.dirty = false; row.confirmed = true
    return { ...detail(path, content), editor_id: row.editor_id, backend_epoch: row.backend_epoch,
      binding_generation: row.binding_generation, version: row.version, last_saved_version: sequence }
  })
  api.deleteRemoteEditor.mockResolvedValue({ closed: true })
  let ticket: { proof_id: string; body: Record<string, unknown> } | null = null
  let proofSequence = 0
  const assertTuple = (id: string, body: Record<string, unknown>) => {
    if (id !== row.editor_id || body.backend_epoch !== row.backend_epoch
      || body.expected_binding_generation !== row.binding_generation
      || body.document_path !== row.document_path || body.client_instance_id !== row.client_instance_id
      || body.version !== row.version || body.revision !== row.revision
      || !rooms.some(room => room.sid === body.sid)) throw new ApiError(409, 'stale proof', 'editor_mismatch')
  }
  api.materializeRemoteEditor.mockImplementation(async (id, body) => {
    assertTuple(id, body)
    // Joining revoked the ordinary read receipt. A ticket is not evidence yet.
    row.confirmed = false; row.last_saved_version = null
    ticket = { proof_id: `proof-${++proofSequence}`, body: structuredClone(body) }
    return { ...structuredClone(row), proof_id: ticket.proof_id }
  })
  api.confirmRemoteEditorMaterialization.mockImplementation(async (id, body) => {
    assertTuple(id, body)
    const issued = ticket; ticket = null
    const { proof_id, content, ...tuple } = body
    if (!issued || proof_id !== issued.proof_id || JSON.stringify(tuple) !== JSON.stringify(issued.body)
      || content !== disk) throw new ApiError(409, 'divergent snapshot', 'pending_edits')
    row.dirty = false; row.confirmed = true; row.last_saved_version = ++sequence
    return structuredClone(row)
  })
  api.ackRemoteEditor.mockImplementation(async (_change, body) => {
    if (body.editor_id !== row.editor_id || row.dirty || !row.confirmed
      || body.last_saved_version !== row.last_saved_version) throw new ApiError(409, 'invalid evidence', 'invalid_editor_ack')
  })
  return { remote, row: () => row,
    // Synthetic local backend causal peer-final-save+ACK anchor, NOT a Remote wire.
    peerFinalSave: (content: string) => { disk = content },
  }
}
function change(state: RemoteBindingChange['state'] = 'draining', extra: Partial<RemoteBindingChange> = {}) {
  useSyncStore(pinia).bindingChange = { change_id: 'change-a', expires_at: Date.now() / 1000 + 300,
    state, binding_generation: 7, expected_generation: 7, fenced: false,
    required_editor_acks: ['editor-a'], missing_acks: ['editor-a'], reason: null, ...extra }
}

/** Two separate renderer owners over one synthetic local backend/room.
 * Real DocsView, stores, routers, BlockEditors, Yjs and typed HTTP clients execute
 * the handshake. This model supplies only local CAS/causal-save evidence; it is
 * neither a backend implementation nor evidence of deployed Remote interop.
 */
async function pairedDocsViews() {
  const { remote } = scopedFixture()
  apiTransport.realProof = true
  const scope = { project_id: 'ctx-a', epoch: 'runtime-a' }
  const binding = new ProjectSession(scope.project_id); binding.bind(scope)
  const actualApi = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  const client = actualApi.createApiClient(binding)
  api.registerRemoteEditor.mockImplementation(client.registerRemoteEditor)
  api.heartbeatRemoteEditor.mockImplementation(client.heartbeatRemoteEditor)
  api.ackRemoteEditor.mockImplementation(client.ackRemoteEditor)
  api.saveDoc.mockImplementation(client.saveDoc)
  api.deleteRemoteEditor.mockImplementation(client.deleteRemoteEditor)

  type Body = Record<string, unknown>
  type Lease = { dto: ProjectRemoteEditorLease; sid: string | null;
    proof?: { id: string; tuple: Body } }
  const leases = new Map<string, Lease>()
  const sessions = new Map<string, CollabSession>()
  const requests: Array<{ path: string; method: string; body: Body }> = []
  const acks: Array<{ editor_id: string; old_generation: number; last_saved_version: number }> = []
  const confirmed: string[] = []
  const saveGate = deferred<void>()
  // A failed assertion before the explicit completion must not strand teardown
  // on an unresolved writer chain or leak either renderer's Pinia lifecycle.
  extraGuards.push(() => saveGate.resolve())
  let sequence = 100
  let proofSequence = 0
  let disk = SOURCE
  let saving = false
  let anchor = true
  const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })
  const refused = (code: string) => new Response(JSON.stringify({ code, details: {} }), { status: 409 })
  const tupleMatches = (row: Lease, body: Body) => body.backend_epoch === row.dto.backend_epoch
    && body.expected_binding_generation === row.dto.binding_generation
    && body.document_path === row.dto.document_path && body.client_instance_id === row.dto.client_instance_id
    && body.sid === row.sid && body.version === row.dto.version && body.revision === row.dto.revision

  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.searchParams.get('project_id')).toBe(scope.project_id)
    expect(parsed.searchParams.get('epoch')).toBe(scope.epoch)
    const path = parsed.pathname
    const body = JSON.parse(String(init.body)) as Body
    requests.push({ path, method: init.method!, body })
    if (path.endsWith('/remote/editors') && init.method === 'POST') {
      const id = `editor-${leases.size === 0 ? 'alice' : 'bob'}`
      const dto: ProjectRemoteEditorLease = { editor_id: id, backend_epoch: String(body.backend_epoch),
        binding_generation: Number(body.expected_binding_generation), document_path: String(body.document_path),
        client_instance_id: String(body.client_instance_id), version: 0, revision: 1,
        lease_expires_at: Date.now() / 1000 + 30, dirty: false, confirmed: true,
        last_saved_version: ++sequence, loaded_document: detail(PATH, disk) }
      leases.set(id, { dto, sid: null })
      return response(dto)
    }
    if (path.endsWith('/editor-ack')) {
      const row = leases.get(String(body.editor_id))!
      if (!row || saving || !row.dto.confirmed || row.dto.dirty
        || body.old_generation !== row.dto.binding_generation
        || body.last_saved_version !== row.dto.last_saved_version) return refused('invalid_editor_ack')
      acks.push({ editor_id: row.dto.editor_id, old_generation: Number(body.old_generation),
        last_saved_version: Number(body.last_saved_version) })
      return new Response(null, { status: 204 })
    }
    if (path.endsWith(`/docs/${PATH}`) && init.method === 'PUT') {
      const row = leases.get(String(body.editor_id))!
      expect(row.dto.editor_id).toBe('editor-alice')
      expect(body.collab_session).toBe(row.sid)
      expect(body.client_instance_id).toBe(row.dto.client_instance_id)
      expect(body.expected_binding_generation).toBe(7)
      expect(body.version).toBe(row.dto.version)
      saving = true
      await saveGate.promise
      disk = String(body.content)
      saving = false; anchor = true
      row.dto.last_saved_version = ++sequence
      row.dto.dirty = false; row.dto.confirmed = true
      return response({ ...detail(PATH, disk), editor_id: row.dto.editor_id,
        backend_epoch: row.dto.backend_epoch, binding_generation: row.dto.binding_generation,
        version: row.dto.version, last_saved_version: row.dto.last_saved_version })
    }
    const match = path.match(/\/remote\/editors\/([^/]+)(.*)$/)
    if (!match) throw new Error(`Unexpected paired API request: ${path}`)
    const row = leases.get(match[1])!
    expect(row).toBeDefined()
    if (match[2] === '/heartbeat') {
      expect(body.sid).toBe(row.sid)
      expect(body.client_instance_id).toBe(row.dto.client_instance_id)
      if (body.dirty === true || body.version !== undefined && body.version !== row.dto.version) {
        row.dto.revision += 1; row.dto.dirty = true; row.dto.confirmed = false
        row.proof = undefined
      }
      if (body.version !== undefined) row.dto.version = Number(body.version)
      return response(row.dto)
    }
    if (match[2] === '/materialization' || match[2] === '/materialization/confirm') {
      if (!tupleMatches(row, body)) return refused('stale_editor_version')
      if (saving || !anchor) return refused('pending_edits')
      if (match[2] === '/materialization') {
        row.proof = { id: `paired-proof-${++proofSequence}`, tuple: structuredClone(body) }
        return response({ ...row.dto, proof_id: row.proof.id })
      }
      const ticket = row.proof; row.proof = undefined
      const { proof_id, content, ...tuple } = body
      if (!ticket || proof_id !== ticket.id || JSON.stringify(tuple) !== JSON.stringify(ticket.tuple)
        || content !== disk) return refused('pending_edits')
      row.dto.confirmed = true; row.dto.dirty = false; row.dto.last_saved_version = ++sequence
      confirmed.push(row.dto.editor_id)
      return response(row.dto)
    }
    if (init.method === 'DELETE') return response({ closed: true })
    throw new Error(`Unexpected paired API method: ${path}`)
  }))

  const seed = seedUpdate(SOURCE)
  roomApi.open.mockImplementation(async (options: OpenOptions) => {
    const id = options.fence!.editor_id!
    const row = leases.get(id)!
    const doc = new Y.Doc(); Y.applyUpdate(doc, seed)
    const socket: SocketLike = { onopen: null, onmessage: null, onclose: null,
      readyState: 1, close: vi.fn(), send: data => {
        const frame = JSON.parse(data)
        if (frame.t !== 'update') return
        anchor = false
        for (const lease of leases.values()) {
          lease.dto.revision += 1; lease.dto.dirty = true; lease.dto.confirmed = false
          lease.dto.last_saved_version = null; lease.proof = undefined
        }
        for (const [peerId, peer] of sessions) if (peerId !== id) peer.receive(frame)
      } }
    row.sid = `sid-${id}`
    row.dto.revision += 1; row.dto.dirty = true; row.dto.confirmed = false
    row.dto.last_saved_version = null
    delete row.dto.loaded_document
    const session = new CollabSession(PATH, row.sid,
      { id, name: id, color: '#123456' }, doc, socket, id === 'editor-alice', false,
      binding, binding.scope, options.ticket!.assertCurrent, options.fence)
    sessions.set(id, session); rooms.push(session)
    return session
  })

  async function mountPeer(id: 'editor-alice' | 'editor-bob') {
    const peerPinia = createPinia(); extraPinia.push(peerPinia); setActivePinia(peerPinia)
    const sync = useSyncStore(peerPinia)
    sync.remote = { ...remote, state: 'connected' }
    const host = document.body.appendChild(document.createElement('div'))
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/docs', name: 'docs', component: DocsView },
    ] })
    await router.push({ name: 'docs', query: { path: PATH } })
    const peerApp = createApp({ render: () => h(RouterView) }).use(peerPinia).use(router)
    blockApps.push(peerApp); peerApp.mount(host)
    await flush(); await vi.advanceTimersByTimeAsync(20); await flush()
    const instance = captured.editors.at(-1)!.value!
    expect(host.querySelector('.ProseMirror')).toBe(instance.view.dom)
    expect(instance.getText()).toContain('A paragraph.')
    expect(useDocsStore(peerPinia).live).toBe(sessions.get(id)!.sid)
    return { pinia: peerPinia, sync, docs: useDocsStore(peerPinia), instance, host,
      session: sessions.get(id)!, row: leases.get(id)!, id }
  }
  const alice = await mountPeer('editor-alice')
  const bob = await mountPeer('editor-bob')
  const quiesce = () => {
    for (const peer of [alice, bob]) peer.sync.bindingChange = {
      change_id: 'paired-change', expires_at: Date.now() / 1000 + 300,
      state: 'draining', binding_generation: 7, expected_generation: 7, fenced: false,
      required_editor_acks: [...leases.keys()], missing_acks: [...leases.keys()], reason: null,
    }
  }
  return { alice, bob, requests, acks, confirmed, quiesce, saveGate,
    saving: () => saving, disk: () => disk }
}

describe('I8 paired mounted DocsView owners (synthetic local seam only)', () => {
  it.each(['clean', 'non-scribe-edit'] as const)('drains %s through both real view owners and their separate own receipts', async mode => {
    const fixture = await pairedDocsViews()
    const { alice, bob } = fixture
    expect(alice.docs).not.toBe(bob.docs)
    expect(alice.sync).not.toBe(bob.sync)
    expect(alice.row.dto.client_instance_id).not.toBe(bob.row.dto.client_instance_id)
    expect(alice.session.sid).not.toBe(bob.session.sid)
    expect(alice.session.scribe).toBe(true); expect(bob.session.scribe).toBe(false)
    const doms = [alice.instance.view.dom, bob.instance.view.dom]
    const destroyed = [vi.spyOn(alice.session.doc, 'destroy'), vi.spyOn(bob.session.doc, 'destroy')]
    const closes = [vi.spyOn(alice.session, 'close'), vi.spyOn(bob.session, 'close')]
    if (mode === 'non-scribe-edit') {
      bob.instance.commands.insertContentAt(1, 'Paired non-scribe ')
      await flush()
      expect(alice.instance.getText()).toContain('Paired non-scribe ')
      expect(alice.docs.dirty).toBe(true); expect(bob.docs.dirty).toBe(true)
    }
    fixture.quiesce(); await flush(); await flush(); await flush()
    expect(alice.instance.isEditable).toBe(false); expect(bob.instance.isEditable).toBe(false)
    if (mode === 'non-scribe-edit') {
      expect(alice.docs.fenceError).toBeNull()
      expect(fixture.saving()).toBe(true)
      expect(api.saveDoc).toHaveBeenCalledTimes(1)
      expect(fixture.acks).toEqual([])
      expect(alice.docs.lastSaveReceipt).toBeNull(); expect(bob.docs.lastSaveReceipt).toBeNull()
      expect(fixture.confirmed).toEqual([])
      await vi.advanceTimersByTimeAsync(251); await flush()
      expect(fixture.acks).toEqual([])
      fixture.saveGate.resolve()
      await flush(); await vi.advanceTimersByTimeAsync(501); await flush()
    }
    expect(fixture.acks.map(ack => ack.editor_id).sort()).toEqual(['editor-alice', 'editor-bob'])
    expect(new Set(fixture.acks.map(ack => ack.last_saved_version)).size).toBe(2)
    for (const [index, peer] of [alice, bob].entries()) {
      const ack = fixture.acks.find(ack => ack.editor_id === peer.id)!
      expect(ack.old_generation).toBe(7)
      expect(ack.last_saved_version).toBe(peer.docs.lastSaveReceipt!.last_saved_version)
      expect(peer.docs.lastSaveReceipt!.editor_id).toBe(peer.id)
      expect(peer.docs.dirty).toBe(false)
      expect(peer.docs.fenceError).toBeNull(); expect(peer.docs.saveError).toBeNull()
      expect(peer.host.querySelector('.ProseMirror')).toBe(doms[index])
      expect(peer.instance.isDestroyed).toBe(false)
      expect(peer.session.doc.isDestroyed).toBe(false)
      expect(peer.docs.live).toBe(peer.session.sid)
      expect(peer.instance.getText()).toContain(mode === 'clean' ? 'A paragraph.' : 'Paired non-scribe ')
    }
    expect(api.saveDoc).toHaveBeenCalledTimes(mode === 'clean' ? 0 : 1)
    expect(fixture.requests.filter(request => request.method === 'PUT')).toHaveLength(mode === 'clean' ? 0 : 1)
    expect(fixture.confirmed.sort()).toEqual(mode === 'clean' ? ['editor-alice', 'editor-bob'] : ['editor-bob'])
    expect(roomApi.open).toHaveBeenCalledTimes(2)
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(2)
    destroyed.forEach(spy => expect(spy).not.toHaveBeenCalled())
    closes.forEach(spy => expect(spy).not.toHaveBeenCalled())
    expect(fixture.disk()).toContain(mode === 'clean' ? 'A paragraph.' : 'Paired non-scribe ')
  })
})

describe('I8 local mounted lease/flush handshake (production R3 remains closed)', () => {
  it('keeps the same live editor editable across an ordinary same-G metadata observation', async () => {
    const fixture = scopedFixture()
    const { room } = liveRoom()
    const sync = useSyncStore(pinia)
    sync.remote = { ...fixture.remote, state: 'connected' }
    await mount(room)
    const instance = editor()
    sync.remotePending = true; await flush()
    sync.remotePending = false; await flush()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(true)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
  })

  it('keeps the ordinary lease usable after a same-path server refresh without writing on that refresh', async () => {
    scopedFixture()
    const store = await mount()
    const instance = editor()
    await store.select(PATH)
    await flush()
    expect(api.saveDoc).not.toHaveBeenCalled()
    edit('After refresh ')
    await vi.advanceTimersByTimeAsync(701)
    await flush()
    expect(store.fenceError).toBeNull()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(true)
    expect(api.saveDoc.mock.calls[0][1]).toContain('After refresh ')
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
  })

  it('orders newer dirty heartbeats behind a pending save so its I5 receipt cannot be relabelled', async () => {
    const fixture = scopedFixture()
    const store = await mount()
    const saved = deferred<DocDetail>()
    api.saveDoc.mockReturnValueOnce(saved.promise)
    edit('First ')
    await vi.advanceTimersByTimeAsync(701)
    await flush()
    edit('Second ')
    await flush()
    expect(api.heartbeatRemoteEditor).toHaveBeenCalledTimes(1)
    const row = fixture.row()
    expect(row.version).toBe(1)
    saved.resolve({ ...detail(PATH, api.saveDoc.mock.calls[0][1]), ...{
      editor_id: row.editor_id, backend_epoch: row.backend_epoch, binding_generation: row.binding_generation,
      version: row.version, last_saved_version: 2 } })
    await flush()
    expect(api.heartbeatRemoteEditor).toHaveBeenCalledTimes(2)
    expect(store.fenceError).toBeNull()
    expect(editor().isEditable).toBe(true)
    await vi.advanceTimersByTimeAsync(701)
    await flush()
    expect(api.saveDoc).toHaveBeenCalledTimes(2)
    expect(api.saveDoc.mock.calls[1][5].version).toBe(2)
    expect(store.dirty).toBe(false)
  })

  it('freezes rename until the outgoing heartbeat and Markdown save settle, then moves the saved bytes', async () => {
    const fixture = scopedFixture()
    await mount()
    const pending = deferred<ProjectRemoteEditorLease>()
    api.heartbeatRemoteEditor.mockReturnValueOnce(pending.promise)
    api.moveDoc.mockResolvedValue({ path: 'moved.md', referrers: [] })
    api.doc.mockImplementation(async path => detail(path, api.saveDoc.mock.calls.at(-1)?.[1] ?? SOURCE))
    edit('Must survive rename ')
    await flush()
    root.querySelector<HTMLButtonElement>('header button')!.click()
    await flush()
    const input = root.querySelector<HTMLInputElement>('header input')!
    input.value = 'moved.md'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    root.querySelector('header form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(api.moveDoc).not.toHaveBeenCalled()
    expect(editor().getText()).toContain('Must survive rename ')
    expect(editor().isEditable).toBe(false)
    pending.resolve(fixture.row())
    await flush()
    await flush()
    expect(useDocsStore(pinia).saveError).toBeNull()
    expect(api.deleteRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(api.saveDoc.mock.calls[0][1]).toContain('Must survive rename ')
    expect(api.moveDoc).toHaveBeenCalledWith(PATH, 'moved.md', {
      backend_epoch: 'backend-a', expected_binding_generation: 7,
    })
    expect(api.deleteRemoteEditor).toHaveBeenCalledTimes(1)
  })

  it('registers an ordinary editor before first save, renews without dirty laundering, and clean transfer writes zero', async () => {
    scopedFixture()
    await mount()
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    const instance = editor()
    const dom = instance.view.dom
    const host = document.body.appendChild(document.createElement('section'))
    host.appendChild(root)
    vi.mocked(document.hasFocus).mockReturnValue(false)
    blur()
    document.body.prepend(root)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.heartbeatRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.heartbeatRemoteEditor.mock.calls[0][1]).toMatchObject({ backend_epoch: 'backend-a', expected_binding_generation: 7 })
    expect(api.heartbeatRemoteEditor.mock.calls[0][1].dirty).toBeUndefined()
    expect(root.querySelector('.ProseMirror')).toBe(dom)
    expect(api.saveDoc).not.toHaveBeenCalled()
    change()
    await flush()
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 1,
    })
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(instance.isEditable).toBe(false)
    change('cancelled')
    await flush()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(true)
  })

  it('freezes in place, flushes ordinary debounce and waits for the exact completed-save receipt before ACK', async () => {
    scopedFixture()
    const store = await mount()
    const instance = editor()
    const saved = deferred<DocDetail>()
    api.saveDoc.mockReturnValueOnce(saved.promise)
    edit('Only on screen ')
    change()
    await flush()
    expect(instance.isEditable).toBe(false)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    const fence = api.saveDoc.mock.calls[0][5] as DocumentFence
    expect(fence).toMatchObject({ backend_epoch: 'backend-a', expected_binding_generation: 7,
      editor_id: 'editor-a', sid: null, version: 1 })
    // I5 receipt is a backend sequence, explicitly different from client version=1.
    saved.resolve({ ...detail(PATH, api.saveDoc.mock.calls[0][1]), ...{
      editor_id: 'editor-a', backend_epoch: 'backend-a', binding_generation: 7,
      version: 1, last_saved_version: 91 } })
    api.ackRemoteEditor.mockResolvedValueOnce(undefined)
    await flush()
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 91,
    })
    expect(store.dirty).toBe(false)
    expect(editor()).toBe(instance)
    expect(instance.getText()).toContain('Only on screen ')
  })

  it.each(['editor_lease_expired', 'stale_binding_generation'])('keeps dirty text and rejects ACK/Keep mine on %s', async code => {
    scopedFixture()
    const store = await mount()
    edit('Never discard ')
    api.saveDoc.mockRejectedValueOnce(new ApiError(409, 'fenced', code))
    const instance = editor()
    change()
    await flush()
    expect(store.dirty).toBe(true)
    expect(store.staleOnDisk).toBe(false)
    expect(store.fenceError).not.toBeNull()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(instance.getText()).toContain('Never discard ')
    expect(editor()).toBe(instance)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
  })

  it('does not rejoin/adopt/destroy Yjs or erase text on offline or a fenced/new-target event', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom()
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    edit('Room RAM ')
    await flush()
    const instance = editor()
    room.end('offline')
    await flush()
    expect(editor()).toBe(instance)
    expect(store.live).toBe(room.sid)
    expect(instance.getText()).toContain('Room RAM ')
    expect(close).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    change('ready', { fenced: true, binding_generation: 8 })
    useSyncStore(pinia).remote = { ...fixture.remote, binding_generation: 8, state: 'connected' }
    await vi.advanceTimersByTimeAsync(10_000)
    await flush()
    expect(roomApi.open).toHaveBeenCalledTimes(1)
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    expect(instance.getText()).toContain('Room RAM ')
  })

  it('rejects an old debounce after a generation change without retrying it with latest G', async () => {
    const fixture = scopedFixture()
    const store = await mount()
    edit('Old callback ')
    const instance = editor()
    useSyncStore(pinia).remote = { ...fixture.remote, binding_generation: 8 }
    await vi.advanceTimersByTimeAsync(800)
    await flush()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(store.dirty).toBe(true)
    expect(instance.getText()).toContain('Old callback ')
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
  })

  it('proves a clean collab non-scribe with its own receipt and ACK, with zero Markdown writes', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    await mount(room)
    const instance = editor()
    change()
    await flush()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.materializeRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1)
    expect(api.confirmRemoteEditorMaterialization.mock.calls[0][1]).toMatchObject({
      content: SOURCE, sid: room.sid, document_path: PATH, version: 0, revision: 1,
      backend_epoch: 'backend-a', expected_binding_generation: 7,
    })
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 2,
    })
    expect(close).not.toHaveBeenCalled()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(false)
  })

  it.each(['pending_edits', 'unknown_sid', 'editor_lease_expired'])('keeps the original clean collab tree on proof refusal %s', async code => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    const dom = instance.view.dom
    const document = instance.state.doc
    api.confirmRemoteEditorMaterialization.mockRejectedValue(new ApiError(409, 'proof refused', code))
    change(); await flush()
    expect(api.materializeRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(store.staleOnDisk).toBe(false)
    expect(store.saveError !== null || root.querySelector('[role="status"]') !== null).toBe(true)
    expect(editor()).toBe(instance)
    expect(instance.view.dom).toBe(dom); expect(instance.state.doc).toBe(document)
    expect(close).not.toHaveBeenCalled()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
  })

  it('proves dirty non-scribe RAM only against a completed peer-final-save, without a local save', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    edit('Peer final '); await flush()
    expect(store.dirty).toBe(true)
    expect(fixture.row().confirmed).toBe(false)
    fixture.peerFinalSave(SOURCE.replace('# Document', '# Peer final Document'))
    change(); await flush()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.confirmRemoteEditorMaterialization.mock.calls[0][1].content).toContain('Peer final ')
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 2,
    })
    expect(store.dirty).toBe(false)
    expect(editor()).toBe(instance)
    expect(instance.getText()).toContain('Peer final ')
    expect(close).not.toHaveBeenCalled()
  })

  it('waits fail-closed for a peer-final-save anchor, then retries only its own old-G proof', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    edit('Waiting peer '); await flush()
    change(); await flush()
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(store.dirty).toBe(true)
    expect(store.fenceError).toBeNull()
    expect(store.staleOnDisk).toBe(false)
    await store.saveOverwriting('Forbidden overwrite')
    expect(api.saveDoc).not.toHaveBeenCalled()
    fixture.peerFinalSave(SOURCE.replace('# Document', '# Waiting peer Document'))
    await vi.advanceTimersByTimeAsync(251); await flush()
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(2)
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 2,
    })
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(roomApi.open).toHaveBeenCalledTimes(1)
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    expect(store.dirty).toBe(false)
    expect(editor()).toBe(instance)
    expect(close).not.toHaveBeenCalled()
  })

  it('invalidates desktop approval on a same-Markdown room change after its own proof', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    await mount(room)
    const instance = editor()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    expect(instance.isEditable).toBe(true)
    const peer = new Y.Doc()
    Y.applyUpdate(peer, Y.encodeStateAsUpdate(room.doc))
    peer.getMap('post-proof').set('same-markdown', true)
    room.receive({ t: 'update', project_id: 'ctx-a', epoch: 'runtime-a',
      update: toBase64(Y.encodeStateAsUpdate(peer, Y.encodeStateVector(room.doc))) })
    peer.destroy()
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(instance.getText()).toContain('A paragraph.')
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
  })

  it.each([false, true])('thaws a clean collab after desktop prepare (unrelated guard veto=%s) and rejects approval after the next edit', async veto => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    const dom = instance.view.dom
    const document = instance.state.doc
    const otherGuard = rendererCloseBarrier.register(() => !veto)
    extraGuards.push(otherGuard)
    expect(await rendererCloseBarrier.prepare()).toBe(!veto)
    expect(rendererCloseBarrier.validate()).toBe(!veto)
    expect(instance.isEditable).toBe(true)
    expect(editor()).toBe(instance)
    expect(instance.view.dom).toBe(dom); expect(instance.state.doc).toBe(document)
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1)
    expect(api.saveDoc).not.toHaveBeenCalled()
    if (veto) {
      otherGuard()
      expect(await rendererCloseBarrier.prepare()).toBe(true)
      expect(rendererCloseBarrier.validate()).toBe(true)
      expect(instance.isEditable).toBe(true)
    }
    edit('After prepare ')
    expect(store.dirty).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(instance.getText()).toContain('After prepare ')
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.deleteRemoteEditor).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
  })

  it('does not request a ticket when cancellation wins the neutral proof heartbeat', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    const heartbeat = deferred<ProjectRemoteEditorLease>()
    api.heartbeatRemoteEditor.mockReturnValueOnce(heartbeat.promise)
    change(); await flush()
    expect(api.heartbeatRemoteEditor).toHaveBeenCalledTimes(1)
    const body = api.heartbeatRemoteEditor.mock.calls[0][1]
    expect(body.sid).toBe(room.sid)
    expect(body.dirty).toBeUndefined(); expect(body.version).toBeUndefined()
    change('cancelled'); await flush()
    heartbeat.resolve(structuredClone(fixture.row())); await flush()
    expect(api.materializeRemoteEditor).not.toHaveBeenCalled()
    expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(store.lastSaveReceipt).toBeNull()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(true)
    expect(close).not.toHaveBeenCalled()
  })

  it('does not send confirm when cancellation wins the delayed ticket request', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    const pending = deferred<ProjectRemoteEditorLease & { proof_id: string }>()
    const issue = api.materializeRemoteEditor.getMockImplementation()!
    let ticket: ProjectRemoteEditorLease & { proof_id: string }
    api.materializeRemoteEditor.mockImplementationOnce(async (id, body) => {
      ticket = await issue(id, body)
      return pending.promise
    })
    change(); await flush()
    expect(api.materializeRemoteEditor).toHaveBeenCalledTimes(1)
    change('cancelled'); await flush()
    pending.resolve(ticket!); await flush()
    expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(store.lastSaveReceipt).toBeNull()
    expect(editor()).toBe(instance)
    expect(instance.isEditable).toBe(true)
    expect(close).not.toHaveBeenCalled()
  })

  it('rejects a late confirm receipt and ACK after forced unmount, retaining the unconfirmed room lease', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    // Model the backend join revoking ordinary read evidence before its neutral
    // heartbeat. A pending server confirm is not frontend acceptance of a receipt.
    Object.assign(fixture.row(), { confirmed: false, last_saved_version: null, dirty: true })
    const pending = deferred<ProjectRemoteEditorLease>()
    const confirm = api.confirmRemoteEditorMaterialization.getMockImplementation()!
    let evidence: ProjectRemoteEditorLease
    api.confirmRemoteEditorMaterialization.mockImplementationOnce(async (id, body) => {
      evidence = await confirm(id, body)
      return pending.promise
    })
    change(); await flush()
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1)
    app!.unmount(); app = undefined
    await flush()
    pending.resolve(evidence!); await flush()
    expect(store.lastSaveReceipt).toBeNull()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.deleteRemoteEditor).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    expect(room.doc.isDestroyed).toBe(false)
    expect(roomApi.open).toHaveBeenCalledTimes(1)
  })

  it.each(['quiesce', 'new-generation'] as const)('refuses an unseen delayed join after %s without admitting a room or starting a second join', async race => {
    const fixture = scopedFixture()
    const store = await mount()
    const unseen = liveRoom(false)
    const pending = deferred<CollabSession | null>()
    roomApi.open.mockReturnValueOnce(pending.promise)
    const sync = useSyncStore(pinia)
    sync.remote = { ...fixture.remote, state: 'connected' }
    await flush()
    expect(roomApi.open).toHaveBeenCalledTimes(1)
    expect(root.querySelector('.ProseMirror')).toBeNull()
    if (race === 'quiesce') change()
    else sync.remote = { ...fixture.remote, state: 'connected', binding_generation: 8 }
    await flush()
    pending.resolve(unseen.room)
    await flush(); await vi.advanceTimersByTimeAsync(1000); await flush()
    expect(unseen.close).toHaveBeenCalledTimes(1)
    expect(unseen.room.doc.isDestroyed).toBe(true) // It was never mounted/admitted.
    expect(store.live).toBeNull()
    expect(root.querySelector('.ProseMirror[contenteditable="true"]')).toBeNull()
    expect(roomApi.open).toHaveBeenCalledTimes(1)
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    expect(api.materializeRemoteEditor).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.deleteRemoteEditor).not.toHaveBeenCalled()
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
  })

  it.each(['cancelled', 'offline', 'new-generation', 'same-markdown-update'] as const)(
    'does not accept or ACK a delayed proof after %s', async race => {
      const fixture = scopedFixture()
      const { room, close } = liveRoom(false)
      useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
      const store = await mount(room)
      const instance = editor()
      const pending = deferred<ProjectRemoteEditorLease>()
      const confirm = api.confirmRemoteEditorMaterialization.getMockImplementation()!
      let evidence: ProjectRemoteEditorLease | undefined
      api.confirmRemoteEditorMaterialization.mockImplementationOnce(async (id, body) => {
        evidence = await confirm(id, body)
        return pending.promise
      })
      change(); await flush()
      expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1)
      if (race === 'cancelled') change('cancelled')
      else if (race === 'offline') room.end('offline')
      else if (race === 'new-generation') {
        change('ready', { fenced: true, binding_generation: 8 })
        useSyncStore(pinia).remote = { ...fixture.remote, binding_generation: 8 }
      } else {
        const peer = new Y.Doc()
        Y.applyUpdate(peer, Y.encodeStateAsUpdate(room.doc))
        peer.getMap('proof-race').set('same-markdown', true)
        room.receive({ t: 'update', project_id: 'ctx-a', epoch: 'runtime-a',
          update: toBase64(Y.encodeStateAsUpdate(peer, Y.encodeStateVector(room.doc))) })
        peer.destroy()
      }
      await flush()
      pending.resolve(evidence!)
      await flush()
      expect(api.ackRemoteEditor).not.toHaveBeenCalled()
      expect(api.saveDoc).not.toHaveBeenCalled()
      expect(store.lastSaveReceipt).toBeNull()
      expect(editor()).toBe(instance)
      expect(instance.getText()).toContain('A paragraph.')
      expect(roomApi.open).toHaveBeenCalledTimes(1)
      expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
      expect(close).not.toHaveBeenCalled()
      if (race === 'cancelled') expect(instance.isEditable).toBe(true)
    },
  )

  it('flushes the live scribe to its old sid during a same-G pending observation before ACK, without destroying the editor', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom()
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    const instance = editor()
    edit('Final scribe ')
    await flush()
    useSyncStore(pinia).remotePending = true // I7 closes new admission before the same-G GET resolves.
    change()
    await flush()
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(api.saveDoc.mock.calls[0][4]).toBe(room.sid)
    expect(api.saveDoc.mock.calls[0][5]).toMatchObject({ editor_id: 'editor-a', sid: room.sid,
      expected_binding_generation: 7, version: 1 })
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 2,
    })
    expect(store.dirty).toBe(false)
    expect(editor()).toBe(instance)
    expect(close).not.toHaveBeenCalled()
  })

  it('uses the registered old-G ticket when a non-scribe inherits the final save during quiesce', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    await mount(room)
    edit('Inherited scribe ')
    await flush()
    useSyncStore(pinia).remotePending = true
    change()
    await flush()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    room.receive({ t: 'scribe', project_id: 'ctx-a', epoch: 'runtime-a' })
    await vi.advanceTimersByTimeAsync(701)
    await flush()
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
    expect(api.saveDoc.mock.calls[0][1]).toContain('Inherited scribe ')
    expect(api.saveDoc.mock.calls[0][5]).toMatchObject({ expected_binding_generation: 7, sid: room.sid })
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 2,
    })
    expect(close).not.toHaveBeenCalled()
  })

  it('retains a dirty Y.Doc on forced unmount and never treats disappearing DOM as saved evidence', async () => {
    const fixture = scopedFixture()
    const { room, close } = liveRoom(false)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    const store = await mount(room)
    edit('Unmounted RAM ')
    await flush()
    app!.unmount(); app = undefined
    await flush()
    expect(store.dirty).toBe(true)
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.deleteRemoteEditor).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    expect(room.doc.isDestroyed).toBe(false)
  })

  it('retains ordinary screen Markdown in the open model on a forced unmount with a refused save', async () => {
    scopedFixture()
    const store = await mount()
    edit('Ordinary RAM ')
    api.saveDoc.mockRejectedValue(new ApiError(500, 'offline'))
    app!.unmount(); app = undefined
    await flush()
    await store.settle()
    expect(store.dirty).toBe(true)
    expect(store.open?.content).toContain('Ordinary RAM ')
    expect(api.deleteRemoteEditor).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    // A fresh initial-read response would contain only disk bytes. Remount must
    // not request it as recovery permission, replace retained text, or auto-retry.
    api.registerRemoteEditor.mockResolvedValue({ loaded_document: detail() })
    await mount()
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
    expect(store.open?.content).toContain('Ordinary RAM ')
    expect(editor().getText()).toContain('Ordinary RAM ')
    expect(editor().isEditable).toBe(false)
    expect(api.saveDoc).toHaveBeenCalledTimes(1)
  })

  it.each(['clean', 'refused'] as const)('ignores an old ordinary %s renewal after join and requires independent collab proof', async outcome => {
    const fixture = scopedFixture()
    await mount()
    const oldCleanProof = structuredClone(fixture.row())
    const pending = deferred<ProjectRemoteEditorLease>()
    api.heartbeatRemoteEditor.mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(10_000)
    await flush()
    const { room, close } = liveRoom()
    roomApi.open.mockResolvedValue(room)
    useSyncStore(pinia).remote = { ...fixture.remote, state: 'connected' }
    await flush()
    expect(useDocsStore(pinia).live).toBe(room.sid)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    if (outcome === 'clean') pending.resolve(oldCleanProof)
    else pending.reject(new ApiError(409, 'old ordinary heartbeat', 'editor_mismatch'))
    await flush()
    expect(editor().isEditable).toBe(true)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    change()
    await flush()
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(3)
    expect(api.ackRemoteEditor).toHaveBeenCalledWith('change-a', {
      editor_id: 'editor-a', old_generation: 7, last_saved_version: 4,
    })
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
  })

  it('blocks a dirty expired heartbeat rather than reviving the lease or falling back to an ordinary write', async () => {
    scopedFixture()
    await mount()
    api.heartbeatRemoteEditor.mockRejectedValueOnce(new ApiError(409, 'expired', 'editor_lease_expired'))
    const instance = editor()
    edit('Expired RAM ')
    await flush()
    change()
    await vi.advanceTimersByTimeAsync(800)
    await flush()
    expect(instance.isEditable).toBe(false)
    expect(instance.getText()).toContain('Expired RAM ')
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
  })

  it('never draws an editable ordinary document before its delayed registration is complete', async () => {
    scopedFixture()
    const registered = deferred<ProjectRemoteEditorLease>()
    const registerImpl = api.registerRemoteEditor.getMockImplementation()!
    api.registerRemoteEditor.mockImplementationOnce(async body => {
      const result = await registerImpl(body)
      return registered.promise.then(() => result)
    })
    const mounting = mount()
    await flush()
    expect(root.querySelector('.ProseMirror')).toBeNull()
    expect(api.saveDoc).not.toHaveBeenCalled()
    registered.resolve({} as ProjectRemoteEditorLease)
    await mounting
    expect(editor().isEditable).toBe(true)
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it('freezes legacy browser editors but never claims a scoped capable ACK or registers a guessed G', async () => {
    await mount()
    const instance = editor()
    edit('Legacy RAM ')
    change()
    await flush()
    expect(instance.isEditable).toBe(false)
    expect(api.registerRemoteEditor).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(instance.getText()).toContain('Legacy RAM ')
  })
})


describe('I8 pathless page-target release race (independent review regression)', () => {
  it('keeps input frozen through held lease DELETE and clears only the proved clean document', async () => {
    scopedFixture()
    const store = await mount()
    const before = editor()
    const released = deferred<{ closed: true }>()
    api.deleteRemoteEditor.mockReturnValueOnce(released.promise)
    const restoring = pageTarget.apply!({ path: '/documents' }, () => undefined)
    await flush()
    expect(api.deleteRemoteEditor).toHaveBeenCalledTimes(1)
    expect(before.isEditable).toBe(false)
    expect(store.open?.path).toBe(PATH)
    expect(before.isDestroyed).toBe(false)
    released.resolve({ closed: true })
    expect(await restoring).toBe(true)
    await flush()
    expect(store.open).toBeNull()
    expect(api.saveDoc).not.toHaveBeenCalled()
  })

  it.each(['late-edit', 'cancelled-target'] as const)('retains the same editor after %s during held lease DELETE', async race => {
    scopedFixture()
    const store = await mount()
    const before = editor()
    const dom = before.view.dom
    const released = deferred<{ closed: true }>()
    api.deleteRemoteEditor.mockReturnValueOnce(released.promise)
    let current = true
    const restoring = pageTarget.apply!({ path: '/documents' }, () => {
      if (!current) throw new Error('Stale page target')
    })
    await flush()
    expect(api.deleteRemoteEditor).toHaveBeenCalledTimes(1)
    expect(before.isEditable).toBe(false)
    if (race === 'late-edit') {
      // Stronger than a user gesture: a plugin/late command can still mutate a
      // read-only PM editor. DELETE's old receipt cannot bless this new tree.
      before.commands.insertContentAt(1, 'Late close RAM ')
      expect(before.getText()).toContain('Late close RAM ')
    } else current = false
    await flush()
    released.resolve({ closed: true })
    expect(await restoring).toBe(false)
    await flush()
    expect(store.open?.path).toBe(PATH)
    expect(editor()).toBe(before)
    expect(root.querySelector('.ProseMirror')).toBe(dom)
    expect(before.isDestroyed).toBe(false)
    expect(before.isEditable).toBe(false)
    expect(store.saveError).not.toBeNull()
    if (race === 'late-edit') {
      expect(store.dirty).toBe(true)
      expect(before.getText()).toContain('Late close RAM ')
    }
    expect(api.saveDoc).not.toHaveBeenCalled()
    expect(api.ackRemoteEditor).not.toHaveBeenCalled()
    expect(api.registerRemoteEditor).toHaveBeenCalledTimes(1)
  })
})
