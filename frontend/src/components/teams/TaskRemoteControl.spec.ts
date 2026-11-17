// @vitest-environment jsdom
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { t } from '@/i18n'
import TaskRemoteControl from './TaskRemoteControl.vue'

let app: App | undefined
let host: HTMLDivElement
let props: {
  taskId: string
  remoteLink: string | null | undefined
  canConnect: boolean
  hint: string
  busy: boolean
}
const toggle = vi.fn()
const writeText = vi.fn()
const trigger = () => host.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]')!
const menu = () => host.querySelector('[role="menu"]')
const action = () => host.querySelector<HTMLButtonElement>('[role="menuitemcheckbox"]')!
const copy = () => host.querySelector<HTMLButtonElement>(`button[title="${t('taskDetail.remote.copyLink')}"]`)!
async function flush() {
  await Promise.resolve()
  await nextTick()
}
async function mount() {
  app = createApp({ render: () => h(TaskRemoteControl, { ...props, onToggle: toggle }) })
  app.mount(host)
  await flush()
}
async function open() {
  trigger().click()
  await flush()
}

beforeEach(() => {
  vi.clearAllMocks()
  writeText.mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { userAgent: navigator.userAgent, clipboard: { writeText } })
  props = reactive({ taskId: 'task-a', remoteLink: null, canConnect: true, hint: 'Ready hint', busy: false })
  host = document.createElement('div')
  document.body.append(host)
})
afterEach(() => {
  app?.unmount()
  app = undefined
  host.remove()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Task Remote Control header dropdown', () => {
  it('starts closed with an accessible icon-only header button', async () => {
    await mount()
    expect(menu()).toBeNull()
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(trigger().getAttribute('aria-label')).toBe(t('taskDetail.remote.title'))
    expect(trigger().title).toBe(t('taskDetail.remote.title'))
    expect(trigger().classList.contains('size-6')).toBe(true)
    expect(trigger().textContent?.trim()).toBe('')
    await open()
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    expect(menu()?.textContent).toContain(props.hint)
    expect(host.querySelector('input')).toBeNull()
  })

  it('emits connect/disconnect without mutating the supplied state', async () => {
    await mount()
    await open()
    expect(action().getAttribute('aria-checked')).toBe('false')
    expect(action().textContent).toContain(t('taskDetail.remote.connect'))
    action().click()
    expect(toggle).toHaveBeenLastCalledWith(true)
    expect(props.remoteLink).toBeNull()
    props.remoteLink = 'https://claude.ai/code/session-a'
    props.canConnect = false
    await flush()
    expect(action().getAttribute('aria-checked')).toBe('true')
    expect(action().disabled).toBe(false)
    expect(action().textContent).toContain(t('taskDetail.remote.disconnect'))
    action().click()
    expect(toggle).toHaveBeenLastCalledWith(false)
  })

  it.each([
    { busy: true, remoteLink: null, canConnect: true },
    { busy: true, remoteLink: 'https://claude.ai/code/a', canConnect: true },
    { busy: false, remoteLink: undefined, canConnect: false },
  ])('disables the action but leaves its hint discoverable: %o', async (state) => {
    Object.assign(props, state)
    await mount()
    await open()
    expect(action().disabled).toBe(true)
    expect(menu()?.textContent).toContain(props.hint)
    action().click()
    expect(toggle).not.toHaveBeenCalled()
  })

  it('shows a readonly URL, safe open link and temporary copy confirmation', async () => {
    vi.useFakeTimers()
    props.remoteLink = 'https://claude.ai/code/a'
    await mount()
    await open()
    const input = host.querySelector('input')!
    expect(input.readOnly).toBe(true)
    expect(input.value).toBe(props.remoteLink)
    const link = host.querySelector('a')!
    expect(link.href).toBe(props.remoteLink)
    expect(link.target).toBe('_blank')
    expect(link.rel).toBe('noopener noreferrer')
    copy().click()
    await flush()
    expect(writeText).toHaveBeenCalledWith(props.remoteLink)
    expect(host.querySelector(`button[title="${t('common.copied')}"]`)).not.toBeNull()
    await vi.advanceTimersByTimeAsync(1500)
    expect(copy()).not.toBeNull()
    copy().click()
    await flush()
    app!.unmount()
    app = undefined
    expect(vi.getTimerCount()).toBe(0)
  })

  it('selects the URL when clipboard access fails', async () => {
    writeText.mockRejectedValue(new Error('Clipboard unavailable'))
    props.remoteLink = 'https://claude.ai/code/a'
    await mount()
    await open()
    copy().click()
    await flush()
    const input = host.querySelector('input')!
    expect(document.activeElement).toBe(input)
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe(input.value.length)
    expect(copy()).not.toBeNull()
  })

  it('dismisses on outside click or trigger, not inside click', async () => {
    await mount()
    await open()
    menu()!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(menu()).not.toBeNull()
    document.body.click()
    await flush()
    expect(menu()).toBeNull()
    await open()
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await flush()
    expect(menu()).toBeNull()
    await open()
    await open()
    expect(menu()).toBeNull()
  })

  it('claims Escape before the task dialog and restores trigger focus', async () => {
    const parentKey = vi.fn()
    host.addEventListener('keydown', parentKey)
    await mount()
    await open()
    action().focus()
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    action().dispatchEvent(event)
    await flush()
    expect(event.defaultPrevented).toBe(true)
    expect(parentKey).not.toHaveBeenCalled()
    expect(menu()).toBeNull()
    expect(document.activeElement).toBe(trigger())
    trigger().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(parentKey).toHaveBeenCalledOnce()
  })

  it('reacts to URL changes and resets/closes for another task', async () => {
    vi.useFakeTimers()
    props.remoteLink = 'https://claude.ai/code/a'
    await mount()
    await open()
    copy().click()
    await flush()
    props.remoteLink = 'https://claude.ai/code/b'
    await flush()
    expect(host.querySelector('input')!.value).toBe(props.remoteLink)
    expect(host.querySelector('a')!.href).toBe(props.remoteLink)
    expect(copy()).not.toBeNull()
    expect(vi.getTimerCount()).toBe(0)
    props.taskId = 'task-b'
    await flush()
    expect(menu()).toBeNull()
    await open()
    props.remoteLink = null
    await flush()
    expect(host.querySelector('input')).toBeNull()
    expect(host.querySelector('a')).toBeNull()
    expect(action().getAttribute('aria-checked')).toBe('false')
  })

  it('ignores a clipboard result arriving after switching tasks', async () => {
    let resolve!: () => void
    writeText.mockReturnValue(new Promise<void>((done) => { resolve = done }))
    props.remoteLink = 'https://claude.ai/code/a'
    await mount()
    await open()
    copy().click()
    props.taskId = 'task-b'
    await flush()
    resolve()
    await flush()
    await open()
    expect(copy()).not.toBeNull()
    expect(host.querySelector(`button[title="${t('common.copied')}"]`)).toBeNull()
  })
})
