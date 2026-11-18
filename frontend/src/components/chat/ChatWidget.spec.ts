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
// The route the widget reads, reactive so a test can navigate a mounted widget.
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({ name: 'home', path: '/', fullPath: '/' })
  return { useRoute: () => route, route }
})
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
const { route } = await import('vue-router') as unknown as {
  route: { name: string; path: string; fullPath: string }
}

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

async function mount(options: { open?: boolean } = {}) {
  chat = useChatStore()
  chat.view = 'new'
  chat.open = options.open ?? true
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
  Object.assign(route, { name: 'home', path: '/', fullPath: '/' })
  document.getElementById('producer-chat')?.remove()
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

describe('the corner chat button', () => {
  /** VS-185: the button is on every page — including Producer Mode, whose own slot the
   * widget leaves as a popup and returns to when that popup closes. */
  it('lifts the Producer page conversation into a popup, and puts it back', async () => {
    Object.assign(route, { name: 'producer', path: '/producer', fullPath: '/producer' })
    const slot = document.createElement('div')
    slot.id = 'producer-chat'
    document.body.append(slot)
    await mount({ open: false })

    // Docked: the widget sits in the workspace's slot, the button in the shell's corner.
    expect(chat.pageVisible).toBe(true)
    expect(slot.querySelector('[data-chat-widget]')).not.toBeNull()
    expect(document.querySelectorAll('.chat-launcher')).toHaveLength(1)
    expect(root.querySelector('.chat-launcher')).not.toBeNull()

    root.querySelector<HTMLButtonElement>('.chat-launcher')!.click()
    await flush()
    expect(chat.open).toBe(true)
    expect(chat.pageVisible).toBe(false)
    expect(slot.querySelector('[data-chat-widget]')).toBeNull()
    // Out of the page panel the same instance is an ordinary chat again: it has the header
    // the workspace replaces, so the member and the list are reachable from here.
    expect(root.querySelector('[data-chat-widget]')).not.toBeNull()
    expect(root.querySelector(`button[title="${t('chat.minimise')}"]`)).not.toBeNull()
    expect(document.querySelectorAll('.chat-launcher')).toHaveLength(1)

    root.querySelector<HTMLButtonElement>('.chat-launcher')!.click()
    await flush()
    expect(chat.open).toBe(false)
    expect(chat.pageVisible).toBe(true)
    expect(slot.querySelector('[data-chat-widget]')).not.toBeNull()
  })
})
