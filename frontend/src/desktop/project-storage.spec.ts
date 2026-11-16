// @vitest-environment jsdom
/** Persisted UI ownership, exercised through the real component setup/watchers (§5.3.2). */
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createApp, defineComponent, nextTick, reactive, ref, type App, type Component, type SetupContext } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { projectStorageKey, readProjectValue, writeProjectValue } from './project-storage'

let project = reactive({ project: { root: 'd:/A', project_id: 'runtime_a', epoch: 'epoch_1' } })
vi.mock('@/stores/session', () => ({ useSessionStore: () => project }))
vi.mock('@/stores/docs', () => ({ useDocsStore: () => ({ open: null, draftTitle: '' }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ name: 'tasks', fullPath: '/tasks' }) }))
vi.mock('@/ws/connection', () => ({ onEvent: () => () => {}, ws: { state: 'connected' } }))

const act = vi.fn(async () => true)
const messageTask = vi.fn(async () => true)
vi.mock('@/stores/teams', () => ({ useTeamsStore: () => ({
  members: [], teams: [], tasks: [], team: { id: 'copied_team' }, memberById: new Map(),
  load: async () => {}, loadOptions: async () => {}, managerOf: () => null,
  isHuman: () => false, act, messageTask,
}) }))
const upload = vi.fn()
vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  api: { chats: async () => [], removeTaskAttachment: async () => {} },
  uploadTaskAttachment: (...args: unknown[]) => upload(...args),
}))

const { default: TaskDetail } = await import('@/components/teams/TaskDetail.vue')
const { default: ChatWidget } = await import('@/components/chat/ChatWidget.vue')
const { useChatStore } = await import('@/stores/chat')

// Keep the actual SFC setup, watchers and lifecycle, but omit unrelated presentation.
// This makes draft ownership observable without replacing its implementation with a mock.
type State = Record<string, any>
const apps: App[] = []
const piniaStores: ReturnType<typeof createPinia>[] = []
function mountSetup(component: Component, taskId?: string) {
  const task = ref({
    id: taskId, title: 'Task', brief: '', assignee_id: 'member', assignee: null,
    depends_on: [], write_paths: [], messages: [], status: 'needs_review',
    model: null, effort: null, auto_start: false,
  })
  let state!: State
  const setup = (component as unknown as { setup: (props: object, ctx: SetupContext) => State }).setup
  const app = createApp(defineComponent({ setup(_, ctx) {
    state = setup(reactive({ get task() { return task.value } }), ctx)
    return () => null
  } }))
  const pinia = createPinia()
  piniaStores.push(pinia)
  setActivePinia(pinia)
  const root = document.createElement('div')
  document.body.append(root)
  app.use(pinia).mount(root)
  apps.push(app)
  return { state, task, app }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function selectRoot(root: string, runtime = 'runtime_b') {
  project = reactive({ project: { root, project_id: runtime, epoch: 'epoch_2' } })
}
beforeEach(() => {
  window.localStorage.clear()
  selectRoot('d:/A', 'runtime_a')
  vi.clearAllMocks()
})
afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  for (const pinia of piniaStores.splice(0)) disposePinia(pinia)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('project storage keys', () => {
  it('requires canonical metadata and encodes tuples without delimiter collisions', () => {
    expect(projectStorageKey(null, 'draft')).toBeNull()
    expect(projectStorageKey('', 'draft')).toBeNull()
    expect(projectStorageKey('d:/A', 'x:y')).not.toBe(projectStorageKey('d:/A:x', 'y'))
    writeProjectValue(null, 'unowned')
    expect(readProjectValue(null)).toBeNull()
    expect(window.localStorage.length).toBe(0)
  })
  it('tolerates disabled or full storage and removes empty records', () => {
    const key = projectStorageKey('d:/A', 'draft')
    writeProjectValue(key, 'saved')
    expect(readProjectValue(key)).toBe('saved')
    writeProjectValue(key, null)
    expect(readProjectValue(key)).toBeNull()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('disabled') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full') })
    expect(readProjectValue(key)).toBeNull()
    expect(() => writeProjectValue(key, 'edit')).not.toThrow()
  })
})

describe('task draft and uploaded-attachment ownership', () => {
  it('isolates copied task IDs, ignores unowned keys, and restores by root with new runtime IDs', async () => {
    const legacyComment = 'voxarium_studio.task.comment.copied'
    const legacyAttachments = 'voxarium_studio.task.attachments.copied'
    window.localStorage.setItem(legacyComment, 'Unknown owner')
    window.localStorage.setItem(legacyAttachments, '[{"name":"unknown.png","path":"unknown"}]')
    const a = mountSetup(TaskDetail, 'copied')
    expect(a.state.comment.value).toBe('')
    expect(a.state.attachments.value).toEqual([])
    a.state.comment.value = 'A review'
    a.state.attachments.value = [{ name: 'a.png', path: 'a/path' }]
    selectRoot('d:/B')
    const b = mountSetup(TaskDetail, 'copied')
    expect(b.state.comment.value).toBe('')
    expect(b.state.attachments.value).toEqual([])
    b.state.comment.value = 'B review'
    b.state.attachments.value = [{ name: 'b.png', path: 'b/path' }]
    selectRoot('d:/A', 'new_runtime_a')
    const restored = mountSetup(TaskDetail, 'copied')
    expect(restored.state.comment.value).toBe('A review')
    expect(restored.state.attachments.value).toEqual([{ name: 'a.png', path: 'a/path' }])
    expect(window.localStorage.getItem(legacyComment)).toBe('Unknown owner')
    expect(window.localStorage.getItem(legacyAttachments)).toContain('unknown.png')
    expect(b.state.comment.value).toBe('B review')
    await nextTick()
  })
  it('snapshots a pending upload and comment write before task/project navigation', async () => {
    const a = mountSetup(TaskDetail, 'copied')
    a.state.comment.value = 'A unsent'
    const pending = deferred<{ name: string; path: string }>()
    upload.mockReturnValueOnce(pending.promise)
    const uploading = a.state.attachFiles([new File(['a'], 'a.png')])
    a.task.value = { ...a.task.value, id: 'other' }
    a.state.comment.value = 'Other task'
    selectRoot('d:/B')
    const b = mountSetup(TaskDetail, 'copied')
    b.state.comment.value = 'B unsent'
    pending.resolve({ name: 'a.png', path: 'A/upload' })
    await uploading
    expect(upload.mock.calls[0]?.slice(0, 2)).toEqual(['copied_team', 'copied'])
    expect(a.state.attachments.value).toEqual([])
    expect(b.state.attachments.value).toEqual([])
    selectRoot('d:/A', 'after_restart')
    const restored = mountSetup(TaskDetail, 'copied')
    expect(restored.state.comment.value).toBe('A unsent')
    expect(restored.state.attachments.value).toEqual([{ name: 'a.png', path: 'A/upload' }])
  })
  it('does not clear another task or project when a late review succeeds', async () => {
    const a = mountSetup(TaskDetail, 'copied')
    a.state.comment.value = 'A request changes'
    a.state.attachments.value = [{ name: 'a.png', path: 'A/upload' }]
    const pending = deferred<boolean>()
    act.mockReturnValueOnce(pending.promise)
    const reviewing = a.state.review('request-changes')
    a.task.value = { ...a.task.value, id: 'other' }
    a.state.comment.value = 'Other task draft'
    a.state.attachments.value = [{ name: 'other.png', path: 'other/upload' }]
    selectRoot('d:/B')
    const b = mountSetup(TaskDetail, 'copied')
    b.state.comment.value = 'B draft'
    pending.resolve(true)
    await reviewing
    expect(a.state.comment.value).toBe('Other task draft')
    expect(a.state.attachments.value[0].name).toBe('other.png')
    expect(b.state.comment.value).toBe('B draft')
    expect(b.state.attachments.value).toEqual([])
  })
})

describe('chat composer ownership', () => {
  it('keeps new-chat drafts, in-memory attachments and debounced writes in their renderer root', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('voxarium_studio.chat.draft.new', 'Unknown owner')
    const a = mountSetup(ChatWidget)
    const chatA = useChatStore()
    chatA.startNew('member')
    await nextTick()
    expect(a.state.draft.value).toBe('')
    a.state.draft.value = 'A draft'
    a.state.attached.value = [{ name: 'a.png', reference: 'a/ref' }]
    await nextTick()
    selectRoot('d:/B')
    const b = mountSetup(ChatWidget)
    const chatB = useChatStore()
    chatB.startNew('member')
    await nextTick()
    expect(b.state.draft.value).toBe('')
    expect(b.state.attached.value).toEqual([])
    b.state.draft.value = 'B draft'
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    expect(readProjectValue(chatA.currentDraftKey)).toBe('A draft')
    expect(readProjectValue(chatB.currentDraftKey)).toBe('B draft')
    expect(a.state.attached.value).toHaveLength(1)
    selectRoot('d:/A', 'after_restart')
    const restored = mountSetup(ChatWidget)
    useChatStore().startNew('member')
    await nextTick()
    expect(restored.state.draft.value).toBe('A draft')
    expect(restored.state.attached.value).toEqual([]) // Files/unsent uploads are not crash drafts.
    expect(window.localStorage.getItem('voxarium_studio.chat.draft.new')).toBe('Unknown owner')
  })
  it('isolates copied chat IDs and consumes only the originating attachments on a delayed send', async () => {
    vi.useFakeTimers()
    const a = mountSetup(ChatWidget)
    const chatA = useChatStore()
    await nextTick() // Settle the startup list before selecting the fixture conversation.
    chatA.activeChatId = 'copied'
    chatA.view = 'chat'
    await nextTick()
    a.state.draft.value = 'A message'
    a.state.attached.value = [{ name: 'a.png', reference: 'a/ref' }]
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    const pending = deferred<boolean>()
    const sending = vi.spyOn(chatA, 'send').mockReturnValueOnce(pending.promise)
    const submitted = a.state.submit()
    expect(sending.mock.calls[0]?.[2]).toEqual(['a/ref'])
    selectRoot('d:/B')
    const b = mountSetup(ChatWidget)
    const chatB = useChatStore()
    await nextTick()
    chatB.activeChatId = 'copied'
    chatB.view = 'chat'
    await nextTick()
    expect(b.state.draft.value).toBe('')
    expect(b.state.attached.value).toEqual([])
    b.state.draft.value = 'B message'
    b.state.attached.value = [{ name: 'b.png', reference: 'b/ref' }]
    await nextTick()
    pending.resolve(true)
    await submitted
    await vi.advanceTimersByTimeAsync(250)
    expect(a.state.attached.value).toEqual([])
    expect(b.state.attached.value[0].name).toBe('b.png')
    expect(readProjectValue(chatA.currentDraftKey)).toBeNull()
    expect(readProjectValue(chatB.currentDraftKey)).toBe('B message')
  })
})
