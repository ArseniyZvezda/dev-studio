// @vitest-environment jsdom
/** Mounted §5.3 close regressions: real composer, draft storage and upload guards. */
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createApp, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TeamMember } from '@/api/types'
import { rendererCloseBarrier } from '@/desktop/close-barrier'
import { t } from '@/i18n'

const transport = vi.hoisted(() => ({ chats: vi.fn(), upload: vi.fn() }))
vi.mock('@/api/client', () => ({
  api: { chats: transport.chats }, ApiError: class extends Error {},
  uploadAttachment: transport.upload, attachmentUrl: (reference: string) => `/attachment/${reference}`,
}))
vi.mock('@/ws/connection', () => ({ onEvent: vi.fn(() => () => {}) }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ project: { root: 'D:/close-chat' } }) }))
vi.mock('@/stores/docs', () => ({ useDocsStore: () => ({ open: null, draftTitle: '' }) }))
vi.mock('@/stores/teams', () => ({ useTeamsStore: () => ({
  members: [{ id: 'agent', kind: 'agent', name: 'Developer' } as TeamMember],
  teams: [], memberById: new Map(), busy: false,
}) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ name: 'home', path: '/', fullPath: '/' }) }))
// Display children only. AttachmentPicker and the entire actual composer stay mounted.
vi.mock('@/components/teams/ConversationPanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/teams/MemberMark.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/teams/MemberSelect.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/teams/SessionStatus.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./ChatList.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./ChatHeaderChips.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./ProducerControls.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./ProducerTaskStatus.vue', () => ({ default: { template: '<span />' } }))

const { default: ChatWidget } = await import('./ChatWidget.vue')
const { useChatStore } = await import('@/stores/chat')

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
async function flush() {
  for (let step = 0; step < 5; step++) { await Promise.resolve(); await nextTick() }
}

let app: App | undefined
let pinia: Pinia
let root: HTMLDivElement
let chat: ReturnType<typeof useChatStore>

async function mount() {
  chat = useChatStore()
  chat.view = 'new'
  chat.open = true
  chat.newMemberId = 'agent'
  app = createApp(ChatWidget).use(pinia)
  app.component('RouterLink', { template: '<span><slot /></span>' })
  app.mount(root)
  await flush()
}
async function type(text: string) {
  const field = document.querySelector<HTMLTextAreaElement>('[data-chat-widget] textarea')!
  expect(field).not.toBeNull()
  field.value = text
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flush()
  return field
}
async function pick() {
  const input = document.querySelector<HTMLInputElement>('[data-chat-widget] input[type=file]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['image'], 'screen.png')] })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flush()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  localStorage.clear()
  transport.chats.mockResolvedValue([])
  pinia = createPinia()
  setActivePinia(pinia)
  root = document.createElement('div')
  document.body.append(root)
})
afterEach(async () => {
  vi.restoreAllMocks()
  app?.unmount()
  app = undefined
  root.remove()
  disposePinia(pinia)
  vi.useRealTimers()
  expect(await rendererCloseBarrier.prepare()).toBe(true)
})

describe('mounted ChatWidget close guard', () => {
  it('flushes the real 250ms draft debounce without sending or prompting', async () => {
    await mount()
    const confirm = vi.spyOn(window, 'confirm')
    const send = vi.spyOn(chat, 'send')
    await type('Unsent question')
    const key = chat.currentDraftKey!
    expect(localStorage.getItem(key)).toBeNull()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(localStorage.getItem(key)).toBe('Unsent question')
    expect(document.querySelector('textarea')?.value).toBe('Unsent question')
    expect(send).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('fails closed when draft storage throws, retaining the composer for retry', async () => {
    await mount()
    const field = await type('Keep this text')
    const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(field.value).toBe('Keep this text')
    storage.mockRestore()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(localStorage.getItem(chat.currentDraftKey!)).toBe('Keep this text')
  })

  it('invalidates an approval for a late composer edit without writing in validation', async () => {
    await mount()
    await type('Prepared draft')
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    await type('New edit while another form saves')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(localStorage.getItem(chat.currentDraftKey!)).toBe('Prepared draft')
    expect(storage).not.toHaveBeenCalled()
  })

  it('does not flush the composer merely on focus or popup hide/show', async () => {
    await mount()
    const field = await type('Mounted draft')
    field.focus()
    chat.hide()
    await flush()
    chat.show()
    await flush()
    expect(localStorage.getItem(chat.currentDraftKey!)).toBeNull()
    expect(document.querySelector('textarea')?.value).toBe('Mounted draft')
    expect(await rendererCloseBarrier.prepare()).toBe(true)
  })

  it('keeps pending/failed uploads closed, and follows the existing retry/remove UX', async () => {
    await mount()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    const upload = deferred<never>()
    transport.upload.mockReturnValueOnce(upload.promise)
    await pick()
    expect(rendererCloseBarrier.validate()).toBe(false)
    upload.reject(new Error('Upload refused'))
    await flush()
    expect(document.querySelector('[role=alert]')?.textContent).toBe('Upload refused')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)

    transport.upload.mockResolvedValueOnce({ reference: 'stored', name: 'screen.png', image: false, size: 5 })
    await pick()
    expect(document.querySelector('[role=alert]')).toBeNull()
    // Uploaded chat metadata is intentionally memory-only; no new crash recovery.
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    document.querySelector<HTMLButtonElement>(`button[aria-label="${t('attachments.remove', { name: 'screen.png' })}"]`)!.click()
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
  })
})
