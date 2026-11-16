// @vitest-environment jsdom
/** Mounted regressions: §5.3.2 inventory recovery must not own local Memory drafts. */
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createApp, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MemoryEntry, MemoryEntryDetail, MemoryOverview } from '@/api/types'
import { t } from '@/i18n'
import { ProjectSession } from '@/session/project-session'

const session = vi.hoisted(() => ({ current: null as ProjectSession | null }))
vi.mock('@/session/project-session', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/session/project-session')>(),
  get rendererSession() { return session.current },
}))

const api = {
  memory: vi.fn(), memoryEntry: vi.fn(), saveMemoryEntry: vi.fn(),
  deleteMemoryEntry: vi.fn(), updateSettings: vi.fn(),
}
vi.mock('@/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
}))

type Scope = { project_id?: string; epoch?: string }
type Listener = (payload: unknown, envelope?: Scope) => void | Promise<void>
const listeners = new Map<string, Listener>()
vi.mock('@/ws/connection', () => ({
  ws: { state: 'connected' },
  onEvent: vi.fn((type: string, handler: Listener) => {
    listeners.set(type, handler)
    return () => listeners.delete(type)
  }),
}))

const { default: MemoryView } = await import('./MemoryView.vue')
const { useMemoryStore } = await import('@/stores/memory')

function entry(name = 'fact', version = 0): MemoryEntry {
  return {
    name, description: `Description ${name} version ${version}`, author: null, author_member: null,
    updated_at: `2026-11-13T10:00:0${version}Z`, chars: 25, warning: null,
  }
}

function detail(name = 'fact', version = 0): MemoryEntryDetail {
  return { ...entry(name, version), content: `Content ${name} version ${version}\n` }
}

function page(...entries: MemoryEntry[]): MemoryOverview {
  const text = entries.map((item) => `${item.name} — ${item.description}`).join('\n')
  return {
    path: '.voxarium_studio/memory', enabled: true, index_chars: 10000,
    index: { text, chars: text.length, total: entries.length, shown: entries.length },
    entries, invalid: [],
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

let app: App | undefined
let pinia: Pinia
let root: HTMLDivElement

/** Drain promises and real Vue watchers/rendering without sleeps or fake DOM stubs. */
async function flush() {
  for (let step = 0; step < 5; step++) {
    await Promise.resolve()
    await nextTick()
  }
}

async function mount(...entries: MemoryEntry[]) {
  api.memory.mockResolvedValue(page(...(entries.length ? entries : [entry()])))
  app = createApp(MemoryView).use(pinia)
  app.mount(root)
  await flush()
}

function button(label: string, within: ParentNode = root): HTMLButtonElement {
  const found = [...within.querySelectorAll('button')].find((node) => node.textContent?.trim() === label)
  expect(found, `button ${label}`).toBeDefined()
  return found!
}

function card(name = 'fact'): HTMLLIElement {
  const found = [...root.querySelectorAll('li')].find((node) =>
    node.querySelector(`[title=".voxarium_studio/memory/${name}.md"]`))
  expect(found, `memory card ${name}`).toBeDefined()
  return found!
}

function content(): HTMLTextAreaElement {
  const field = root.querySelector<HTMLTextAreaElement>('textarea.memory-mono')
  expect(field, 'selected memory editor').not.toBeNull()
  return field!
}

function description(): HTMLTextAreaElement {
  return content().closest('li')!.querySelector<HTMLTextAreaElement>('textarea:not(.memory-mono)')!
}

function cap(): HTMLInputElement {
  const field = root.querySelector<HTMLInputElement>('input[type="number"]')
  expect(field, 'memory index cap').not.toBeNull()
  return field!
}

async function click(target: HTMLElement) {
  target.click()
  await flush()
}

async function input(field: HTMLInputElement | HTMLTextAreaElement, value: string) {
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

async function open(name = 'fact') {
  await click(button(t('common.edit'), card(name)))
  expect(content().value).toBe(detail(name).content)
  expect(description().value).toBe(detail(name).description)
}

async function type(text = 'Unsaved content', summary = 'Unsaved description') {
  await input(content(), text)
  await input(description(), summary)
}

function gap(scope: Scope = { project_id: 'project-a', epoch: 'epoch-a' }, resync = true) {
  expect(listeners.has('system.dropped')).toBe(true)
  return listeners.get('system.dropped')!({ count: 1, resync }, scope)
}

async function recover(inventory: MemoryOverview) {
  api.memory.mockResolvedValue(inventory)
  await gap()
  await flush()
}

function expectDraft(text = 'Unsaved content', summary = 'Unsaved description', name = 'fact') {
  expect(content().value).toBe(text)
  expect(description().value).toBe(summary)
  expect(content().closest('li')).toBe(card(name))
  expect(button(t('common.edit'), card(name)).disabled).toBe(true)
  expect(card(name).textContent).toContain(t('memory.editor.unsaved'))
}

function expectMissing() {
  expect(root.textContent).toContain(t('memory.editor.deletedOnDisk', { name: 'fact' }))
}

function expectNoWrites() {
  expect(api.saveMemoryEntry).not.toHaveBeenCalled()
  expect(api.deleteMemoryEntry).not.toHaveBeenCalled()
  expect(api.updateSettings).not.toHaveBeenCalled()
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  session.current = new ProjectSession('project-a')
  session.current.bind({ project_id: 'project-a', epoch: 'epoch-a' })
  listeners.clear()
  for (const mock of Object.values(api)) mock.mockReset()
  api.memoryEntry.mockImplementation(async (name: string) => detail(name))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  root = document.createElement('div')
  document.body.append(root)
})

afterEach(() => {
  app?.unmount()
  app = undefined
  disposePinia(pinia)
  document.body.replaceChildren()
  vi.restoreAllMocks()
})

describe('MemoryView scoped inventory recovery', () => {
  it.each(['content', 'description'] as const)('keeps the live %s field when search metadata changes across repeated gaps', async (focused) => {
    await mount(entry(), entry('other'), entry('hidden', 1))
    const search = root.querySelector<HTMLInputElement>('input[type="search"]')!
    await input(search, 'version 0')
    await open()
    await type()
    const row = card()
    const text = content()
    const summary = description()
    const field = focused === 'content' ? text : summary
    let draftText = 'Unsaved content'
    let draftSummary = 'Unsaved description'
    field.focus()
    field.setSelectionRange(2, 7, 'backward')

    function expectLive() {
      expect(card()).toBe(row)
      expect(content()).toBe(text)
      expect(description()).toBe(summary)
      expect(field.isConnected).toBe(true)
      expect(document.activeElement).toBe(field)
      expect([field.selectionStart, field.selectionEnd, field.selectionDirection]).toEqual([2, 7, 'backward'])
      expectDraft(draftText, draftSummary)
      expect(root.querySelectorAll('li')).toHaveLength(2)
      expect(root.querySelectorAll('[title=".voxarium_studio/memory/fact.md"]')).toHaveLength(1)
      expect(root.querySelector('[title=".voxarium_studio/memory/hidden.md"]')).toBeNull()
      expect(card('other')).toBeDefined()
      expect(root.textContent).not.toContain(t('memory.editor.deletedOnDisk', { name: 'fact' }))
      expectNoWrites()
    }

    for (const version of [1, 2]) {
      await recover(page(entry('fact', version), entry('other'), entry('hidden', 1)))
      expectLive()
      expect(row.textContent).toContain(entry('fact', version).description)
      if (version === 1) {
        draftText = 'Continued content after recovery'
        draftSummary = 'Continued description after recovery'
        await type(draftText, draftSummary)
        field.setSelectionRange(2, 7, 'backward')
      }
    }
    // Matching again must reuse the same keyed row, not add a second copy.
    await recover(page({ ...entry('fact', 3), description: entry().description }, entry('other'), entry('hidden', 1)))
    expectLive()
    await recover(page(entry('fact', 4), entry('other'), entry('hidden', 1)))
    expectLive()
    expect(api.memoryEntry).toHaveBeenCalledTimes(1)
    expect(window.confirm).not.toHaveBeenCalled()

    // Search continues to filter other entries while the selected editor remains live.
    await input(search, 'hidden')
    expect(content()).toBe(text)
    expect(description()).toBe(summary)
    expectDraft(draftText, draftSummary)
    expect(root.querySelectorAll('li')).toHaveLength(2)
    expect(card('hidden')).toBeDefined()
    expect(root.querySelector('[title=".voxarium_studio/memory/other.md"]')).toBeNull()
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    await click(button(t('common.close')))
    expect(content()).toBe(text)
    expect(description()).toBe(summary)
    expectDraft(draftText, draftSummary)
    await click(button(t('common.close')))
    expect(window.confirm).toHaveBeenCalledTimes(2)
    expect(window.confirm).toHaveBeenLastCalledWith(t('memory.editor.discardConfirm', { name: 'fact' }))
    expect(root.querySelector('textarea.memory-mono')).toBeNull()
    expect(root.querySelector('[title=".voxarium_studio/memory/fact.md"]')).toBeNull()
    expect(root.querySelectorAll('li')).toHaveLength(1)
    await input(search, 'other')
    expect(card('other')).toBeDefined()
    expect(root.querySelector('[title=".voxarium_studio/memory/hidden.md"]')).toBeNull()
    expectNoWrites()
  })

  it('keeps a clean editor mounted while refreshing detail outside the active search', async () => {
    await mount(entry(), entry('other'))
    await input(root.querySelector<HTMLInputElement>('input[type="search"]')!, 'version 0')
    await open()
    const row = card()
    const text = content()
    const summary = description()
    for (const version of [1, 2]) {
      api.memoryEntry.mockResolvedValueOnce(detail('fact', version))
      await recover(page(entry('fact', version), entry('other')))
      expect(card()).toBe(row)
      expect(content()).toBe(text)
      expect(description()).toBe(summary)
      expect(text.value).toBe(detail('fact', version).content)
      expect(summary.value).toBe(detail('fact', version).description)
      expect(button(t('common.save')).disabled).toBe(true)
      expect(root.querySelectorAll('li')).toHaveLength(2)
      expectNoWrites()
    }
    await click(button(t('common.close')))
    expect(root.querySelector('textarea.memory-mono')).toBeNull()
    expect(root.querySelector('[title=".voxarium_studio/memory/fact.md"]')).toBeNull()
    expect(card('other')).toBeDefined()
    expect(window.confirm).not.toHaveBeenCalled()
    expectNoWrites()
  })

  it.each(['absent', 'invalid'] as const)('retains text, description, selection and cap after a missed forget (%s)', async (source) => {
    await mount()
    await open()
    await type()
    await input(cap(), '12500') // Input only: the user has not committed the setting.
    const inventory = { ...page(), index_chars: 20000 }
    if (source === 'invalid') inventory.invalid.push({ file: 'fact.md', error: 'Unreadable frontmatter' })
    await recover(inventory) // No memory.changed/file_changed event: forget/settings were missed.
    expect(useMemoryStore(pinia).overview?.entries).toEqual([])
    expectDraft()
    expectMissing()
    expect(cap().value).toBe('12500')
    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.memoryEntry).toHaveBeenCalledTimes(1)
    expectNoWrites()
  })

  it('preserves continued edits through repeated gaps and overtaken delayed inventories', async () => {
    await mount()
    await open()
    await type()
    await input(cap(), '12500')
    const first = deferred<MemoryOverview>()
    const last = deferred<MemoryOverview>()
    api.memory.mockReturnValueOnce(first.promise).mockReturnValueOnce(last.promise)
    const recovery = gap()
    await flush()
    const repeated = gap()
    gap()
    await type('Typed during recovery', 'Description during recovery')
    first.resolve({ ...page(entry('fact', 1)), index_chars: 15000 })
    await flush()
    expectDraft('Typed during recovery', 'Description during recovery')
    expect(api.memory).toHaveBeenCalledTimes(3)
    last.resolve({ ...page(), index_chars: 20000 })
    await Promise.all([recovery, repeated])
    await flush()
    expectDraft('Typed during recovery', 'Description during recovery')
    expectMissing()
    expect(cap().value).toBe('12500')
    await type('Still typing after forget', 'Still editing the description')
    await recover({ ...page(), index_chars: 25000 })
    expectDraft('Still typing after forget', 'Still editing the description')
    expectMissing()
    expect(cap().value).toBe('12500')
    expect(api.memoryEntry).toHaveBeenCalledTimes(1)
    expectNoWrites()
  })

  it.each([
    { project_id: 'project-b', epoch: 'epoch-a' },
    { project_id: 'project-a', epoch: 'epoch-b' },
  ])('ignores a foreign gap %j', async (scope) => {
    await mount()
    await open()
    await type()
    await input(cap(), '12500')
    api.memory.mockResolvedValue(page())
    await gap(scope)
    await gap(undefined, false)
    await flush()
    expect(api.memory).toHaveBeenCalledTimes(1)
    expectDraft()
    expect(cap().value).toBe('12500')
    expectNoWrites()
  })

  it('refreshes a clean editor and clean cap normally after missed changes', async () => {
    await mount()
    await open()
    api.memoryEntry.mockResolvedValueOnce(detail('fact', 1))
    await recover({ ...page(entry('fact', 1)), index_chars: 20000 })
    expect(content().value).toBe(detail('fact', 1).content)
    expect(description().value).toBe(detail('fact', 1).description)
    expect(content().closest('li')).toBe(card())
    expect(cap().value).toBe('20000')
    expect(button(t('common.save')).disabled).toBe(true)
    expect(api.memoryEntry).toHaveBeenCalledTimes(2)
    expect(window.confirm).not.toHaveBeenCalled()
    expectNoWrites()
  })

  it('retains a clean selected entry too when the inventory no longer contains its source', async () => {
    await mount()
    await open()
    await recover(page())
    expect(content().value).toBe(detail().content)
    expect(description().value).toBe(detail().description)
    expect(content().closest('li')).toBe(card())
    expectMissing()
    expect(button(t('common.save')).disabled).toBe(true)
    expectNoWrites()
  })

  it('retains an invalid unfinished cap until the user returns to the current server value', async () => {
    await mount()
    await input(cap(), '')
    await recover({ ...page(entry()), index_chars: 20000 })
    expect(cap().value).toBe('')
    await input(cap(), '20000')
    await recover({ ...page(entry()), index_chars: 25000 })
    expect(cap().value).toBe('25000')
    expectNoWrites()
  })
})

describe('MemoryView late detail responses', () => {
  it.each([false, true])('keeps a pending explicit selection live when the old entry reloads (reload first: %s)', async (reloadFirst) => {
    await mount(entry(), entry('other'))
    await open()
    const selected = deferred<MemoryEntryDetail>()
    const background = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(selected.promise).mockReturnValueOnce(background.promise)
    await click(button(t('common.edit'), card('other')))
    expect(api.memoryEntry).toHaveBeenNthCalledWith(2, 'other')
    await recover(page(entry('fact', 1), entry('other')))
    expect(api.memoryEntry).toHaveBeenNthCalledWith(3, 'fact')
    await recover(page(entry('other')))
    expectMissing()
    if (reloadFirst) {
      background.resolve(detail('fact', 1))
      await flush()
    }
    selected.resolve(detail('other'))
    await flush()
    await type()
    if (!reloadFirst) {
      background.resolve(detail('fact', 1))
      await flush()
    }
    expectDraft('Unsaved content', 'Unsaved description', 'other')
    expect(root.querySelectorAll('textarea.memory-mono')).toHaveLength(1)
    expectNoWrites()
  })

  it('does not overwrite content or description typed while a clean editor reloads', async () => {
    await mount()
    await open()
    const late = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(late.promise)
    await recover(page(entry('fact', 1)))
    await type()
    late.resolve(detail('fact', 1))
    await flush()
    expectDraft()
    expect(button(t('common.save')).disabled).toBe(false)
    expectNoWrites()
  })

  it.each(['success', 'error'] as const)('ignores a late reload %s after the source disappears', async (outcome) => {
    await mount()
    await open()
    const late = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(late.promise)
    await recover(page(entry('fact', 1)))
    await type()
    await recover(page())
    if (outcome === 'success') late.resolve(detail('fact', 1))
    else late.reject(new Error('Obsolete detail failure'))
    await flush()
    expectDraft()
    expectMissing()
    expect(root.textContent).not.toContain('Obsolete detail failure')
    expectNoWrites()
  })

  it('keeps a newly selected entry when the previous entry reload finishes late', async () => {
    await mount(entry(), entry('other'))
    await open()
    const late = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(late.promise)
    await recover(page(entry('fact', 1), entry('other')))
    await open('other')
    await type()
    late.resolve(detail('fact', 1))
    await flush()
    expectDraft('Unsaved content', 'Unsaved description', 'other')
    expect(root.querySelectorAll('textarea.memory-mono')).toHaveLength(1)
    expectNoWrites()
  })

  it('does not replace a reopened editor for the same name with its previous reload', async () => {
    await mount()
    await open()
    const late = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(late.promise)
    await recover(page(entry('fact', 1)))
    await click(button(t('common.close')))
    await open()
    await type()
    late.resolve(detail('fact', 1))
    await flush()
    expectDraft()
    expectNoWrites()
  })

  it('keeps the newest clean reload when older detail requests finish last', async () => {
    await mount()
    await open()
    const older = deferred<MemoryEntryDetail>()
    const newer = deferred<MemoryEntryDetail>()
    api.memoryEntry.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    await recover(page(entry('fact', 1)))
    await recover(page(entry('fact', 2)))
    newer.resolve(detail('fact', 2))
    await flush()
    expect(content().value).toBe(detail('fact', 2).content)
    expect(description().value).toBe(detail('fact', 2).description)
    older.resolve(detail('fact', 1))
    await flush()
    expect(content().value).toBe(detail('fact', 2).content)
    expect(description().value).toBe(detail('fact', 2).description)
    expect(button(t('common.save')).disabled).toBe(true)
    expectNoWrites()
  })
})

describe('MemoryView explicit user actions', () => {
  it('asks before closing a missing dirty entry and preserves it when closing is cancelled', async () => {
    await mount()
    await open()
    await type()
    await recover(page())
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    await click(button(t('common.close')))
    expect(window.confirm).toHaveBeenLastCalledWith(t('memory.editor.discardConfirm', { name: 'fact' }))
    expectDraft()
    expectMissing()
    await click(button(t('common.close')))
    expect(root.querySelector('textarea.memory-mono')).toBeNull()
    expect(root.querySelector('[title=".voxarium_studio/memory/fact.md"]')).toBeNull()
    expect(window.confirm).toHaveBeenCalledTimes(2)
    expectNoWrites()
  })

  it('still closes a dirty entry after an explicitly confirmed delete', async () => {
    await mount()
    await open()
    await type()
    api.deleteMemoryEntry.mockResolvedValue(page())
    const remove = [...card().querySelectorAll('button')].find((node) => node.title === t('memory.list.deleteTitle'))
    expect(remove).toBeDefined()
    await click(remove!)
    const dialog = document.body.querySelector('[role="alertdialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog!.textContent).toContain('.voxarium_studio/memory/fact.md')
    expect(api.deleteMemoryEntry).not.toHaveBeenCalled()
    await click(button(t('common.delete'), dialog!))
    expect(api.deleteMemoryEntry).toHaveBeenCalledExactlyOnceWith('fact')
    expect(root.querySelector('textarea.memory-mono')).toBeNull()
    expect(root.textContent).not.toContain(t('memory.editor.deletedOnDisk', { name: 'fact' }))
    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.saveMemoryEntry).not.toHaveBeenCalled()
  })

  it.each([false, true])('saves only on explicit save with the original version expectation (missing: %s)', async (missing) => {
    await mount()
    await open()
    await type()
    if (missing) await recover(page())
    expectNoWrites()
    api.saveMemoryEntry.mockResolvedValue({ ...detail('fact', 1), description: 'Unsaved description', content: 'Unsaved content' })
    api.memory.mockResolvedValue(page(entry('fact', 1)))
    await click(button(t('common.save')))
    expect(api.saveMemoryEntry).toHaveBeenCalledExactlyOnceWith('fact', {
      description: 'Unsaved description', content: 'Unsaved content',
      expected_updated_at: detail().updated_at,
    })
    expect(content().value).toBe('Unsaved content')
    expect(description().value).toBe('Unsaved description')
    expect(button(t('common.save')).disabled).toBe(true)
  })

  it('commits the preserved cap only after an explicit change', async () => {
    await mount()
    await input(cap(), '12500')
    await recover({ ...page(entry()), index_chars: 20000 })
    expect(cap().value).toBe('12500')
    expectNoWrites()
    api.updateSettings.mockResolvedValue({})
    api.memory.mockResolvedValue({ ...page(entry()), index_chars: 12500 })
    cap().dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    expect(api.updateSettings).toHaveBeenCalledExactlyOnceWith({ teams: { memory_index_chars: 12500 } })
    expect(cap().value).toBe('12500')
    await recover({ ...page(entry()), index_chars: 30000 })
    expect(cap().value).toBe('30000') // A committed setting is clean again.
    expect(api.updateSettings).toHaveBeenCalledTimes(1)
  })
})

const { rendererCloseBarrier } = await import('@/desktop/close-barrier')

describe('Memory mounted close barrier', () => {
  it('clean close does not write, cancel preserves the editor, accept does not unmount', async () => {
    await mount()
    await open()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expectNoWrites()
    await type()
    const field = content()
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(content()).toBe(field)
    expectDraft()
    expectNoWrites()
  })
  it('unfinished and failed save cannot close even with discard accepted', async () => {
    await mount()
    await open()
    await type()
    const saving = deferred<MemoryEntryDetail>()
    api.saveMemoryEntry.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    saving.reject(new Error('Disk full'))
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expectDraft()
  })
  it('continued typing is dirty after delayed success and still asks before close', async () => {
    await mount()
    await open()
    await type('Sent content', 'Sent description')
    const saving = deferred<MemoryEntryDetail>()
    api.saveMemoryEntry.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    await type('Newer content', 'Newer description')
    saving.resolve({ ...detail('fact', 1), content: 'Sent content', description: 'Sent description' })
    await flush()
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expectDraft('Newer content', 'Newer description')
    expect(window.confirm).toHaveBeenCalledTimes(1)
  })
  it('uncommitted and invalid cap fail closed without settings writes', async () => {
    await mount()
    await input(cap(), '12500')
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    await input(cap(), '12')
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expectNoWrites()
  })
})

describe('Memory close approval validity', () => {
  it('revokes confirmed discard when typing continues while another guard awaits', async () => {
    await mount()
    await open()
    await type()
    const later = deferred<boolean>()
    const unregister = rendererCloseBarrier.register(() => later.promise)
    try {
      const closing = rendererCloseBarrier.prepare()
      await flush()
      expect(window.confirm).toHaveBeenCalledTimes(1)
      await type('New draft after confirmation', 'New description')
      later.resolve(true)
      expect(await closing).toBe(false)
      expectDraft('New draft after confirmation', 'New description')
    } finally {
      unregister()
    }
  })

  it('rechecks approved primitives after prepare, including a newly started save', async () => {
    await mount()
    await open()
    await type()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    const saving = deferred<MemoryEntryDetail>()
    api.saveMemoryEntry.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    expect(rendererCloseBarrier.validate()).toBe(false)
    saving.resolve({ ...detail('fact', 1), content: 'Unsaved content', description: 'Unsaved description' })
    await flush()
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    await input(cap(), '12500')
    expect(rendererCloseBarrier.validate()).toBe(false)
  })

  it('confirms a create draft without unmounting it and revokes approval for new typing', async () => {
    await mount()
    await click(button(t('memory.create.button')))
    const form = root.querySelector('form')!
    const field = form.querySelector('input')!
    await input(field, 'new-fact')
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    expect(root.querySelector('form')).toBe(form)
    await input(form.querySelector('textarea')!, 'Later description')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expectNoWrites()
  })
})
