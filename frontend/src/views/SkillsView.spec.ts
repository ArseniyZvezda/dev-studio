// @vitest-environment jsdom
/** Mounted regressions: §5.3.2 resync replaces inventory, never the editor's local draft. */
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createApp, nextTick, type App, type Component } from 'vue'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SkillDetail, SkillEntry, SkillsOverview } from '@/api/types'
import { t } from '@/i18n'
import { ProjectSession } from '@/session/project-session'

const session = vi.hoisted(() => ({ current: null as ProjectSession | null }))
vi.mock('@/session/project-session', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/session/project-session')>(),
  get rendererSession() { return session.current },
}))

const api = { skills: vi.fn(), skill: vi.fn(), createSkill: vi.fn(), saveSkill: vi.fn(), deleteSkill: vi.fn() }
vi.mock('@/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
}))

type Scope = { project_id?: string; epoch?: string }
type Listener = (payload: unknown, envelope?: Scope) => void | Promise<void>
const listeners = new Map<string, Listener>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: Listener) => {
    listeners.set(type, handler)
    return () => listeners.delete(type)
  }),
}))

const { default: SkillsView } = await import('./SkillsView.vue')
const { useSkillsStore } = await import('@/stores/skills')

function entry(name = 'tester', version = 0): SkillEntry {
  return {
    name, path: `.voxarium_studio/skills/${name}`, description: '', warning: null,
    files: [], file_count: 0, file_count_capped: false,
    updated_at: `2026-11-13T10:00:0${version}+00:00`, used_by: [],
  }
}

function detail(name = 'tester', version = 0): SkillDetail {
  return { ...entry(name, version), content: `# ${name}\n\nInstructions version ${version}\n`, crlf: false }
}

function page(...skills: SkillEntry[]): SkillsOverview {
  return {
    path: '.voxarium_studio/skills', absolute_path: 'D:/game/.voxarium_studio/skills',
    skills, invalid: [], missing: [],
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

/** Flush API continuations and Vue's real watchers/render queue, without sleep/timing races. */
async function flush() {
  for (let step = 0; step < 5; step++) {
    await Promise.resolve()
    await nextTick()
  }
}

async function mount(...skills: SkillEntry[]) {
  api.skills.mockResolvedValue(page(...(skills.length ? skills : [entry()])))
  app = createApp(SkillsView).use(pinia)
  app.mount(root)
  await flush()
}

function button(label: string, within: ParentNode = root): HTMLButtonElement {
  const found = [...within.querySelectorAll('button')].find((node) => node.textContent?.trim() === label)
  expect(found, `button ${label}`).toBeDefined()
  return found!
}

function card(name = 'tester'): HTMLLIElement {
  const found = [...root.querySelectorAll('li')].find((node) =>
    node.querySelector(`[title=".voxarium_studio/skills/${name}"]`))
  expect(found, `skill card ${name}`).toBeDefined()
  return found!
}

function textarea(): HTMLTextAreaElement {
  const field = root.querySelector<HTMLTextAreaElement>('textarea.skill-mono')
  expect(field, 'selected skill editor').not.toBeNull()
  return field!
}

async function click(target: HTMLElement) {
  target.click()
  await flush()
}

async function open(name = 'tester') {
  await click(button(t('common.edit'), card(name)))
  expect(textarea().value).toBe(detail(name).content)
}

async function type(text: string) {
  textarea().value = text
  textarea().dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

async function searchFor(text: string) {
  const field = root.querySelector<HTMLInputElement>('input[type="search"]')
  expect(field).not.toBeNull()
  field!.value = text
  field!.dispatchEvent(new Event('input', { bubbles: true }))
  await flush()
}

function expectRetainedEditor(field: HTMLTextAreaElement, text: string) {
  expect(textarea()).toBe(field)
  expect(document.activeElement).toBe(field)
  expect(field.selectionStart).toBe(3)
  expect(field.selectionEnd).toBe(12)
  expect(field.selectionDirection).toBe('backward')
  expectDraft(text)
  expect(root.querySelectorAll('textarea.skill-mono')).toHaveLength(1)
  expect(root.querySelectorAll('[title=".voxarium_studio/skills/tester"]')).toHaveLength(1)
}

function gap(scope: Scope = { project_id: 'project-a', epoch: 'epoch-a' }) {
  expect(listeners.has('system.dropped')).toBe(true)
  return listeners.get('system.dropped')!({ count: 1, resync: true }, scope)
}

async function recover(inventory: SkillsOverview) {
  api.skills.mockResolvedValue(inventory)
  await gap()
  await flush()
}

function expectDraft(text: string, name = 'tester') {
  expect(textarea().value).toBe(text)
  expect(textarea().closest('li')).toBe(card(name))
  expect(button(t('common.edit'), card(name)).disabled).toBe(true)
  expect(card(name).textContent).toContain(t('skills.editor.unsaved'))
}

function expectMissing() {
  expect(card().textContent).toContain(t('skills.editor.deletedOnDisk', {
    name: 'tester', folder: '.voxarium_studio/skills',
  }))
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  })
  pinia = createPinia()
  setActivePinia(pinia)
  session.current = new ProjectSession('project-a')
  session.current.bind({ project_id: 'project-a', epoch: 'epoch-a' })
  listeners.clear()
  for (const mock of Object.values(api)) mock.mockReset()
  api.skill.mockImplementation(async (name: string) => detail(name))
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
  vi.unstubAllGlobals()
})

describe('SkillsView scoped inventory recovery', () => {
  it.each(['absent', 'invalid'] as const)('retains a dirty selected skill when resync finds its source %s', async (missing) => {
    await mount()
    await open()
    await type('My unsaved instructions')
    // A missed external update first offers Keep mine; deletion must revoke that write path.
    await recover(page(entry('tester', 1)))
    expect(button(t('skills.editor.keepMine')).disabled).toBe(false)

    const inventory = page()
    if (missing === 'invalid') inventory.invalid.push({ name: 'tester', error: 'SKILL.md missing', used_by: [] })
    await recover(inventory) // No explorer.file_changed event: deletion was missed in the gap.

    expect(useSkillsStore(pinia).byName.has('tester')).toBe(false)
    expectDraft('My unsaved instructions')
    expectMissing()
    expect(window.confirm).not.toHaveBeenCalled()
    const save = button(t('common.save'))
    expect(save.disabled).toBe(true)
    await click(save)
    // Also dispatch directly: the handler itself must reject a missing source, not just CSS/UI.
    save.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const shortcut = new KeyboardEvent('keydown', { key: 's', ctrlKey: true, bubbles: true, cancelable: true })
    textarea().dispatchEvent(shortcut)
    expect(shortcut.defaultPrevented).toBe(true)
    for (const label of [t('skills.editor.keepMine'), t('common.reload')]) {
      const action = [...root.querySelectorAll('button')].find((node) => node.textContent?.trim() === label)
      if (action) expect(action.disabled).toBe(true)
    }
    await flush()
    expect(api.saveSkill).not.toHaveBeenCalled()
    expect(api.skill).toHaveBeenCalledTimes(1)
    expectDraft('My unsaved instructions')
  })

  it('preserves continued typing and selection through repeated gaps and overtaken inventories', async () => {
    await mount()
    await open()
    await type('Draft before the gap')
    const first = deferred<SkillsOverview>()
    const last = deferred<SkillsOverview>()
    api.skills.mockReturnValueOnce(first.promise).mockReturnValueOnce(last.promise)
    const recovery = gap()
    await flush()
    const repeated = gap()
    gap()
    await type('Draft typed during recovery')
    first.resolve(page(entry('tester', 1)))
    await flush()
    expectDraft('Draft typed during recovery')
    expect(api.skills).toHaveBeenCalledTimes(3) // Initial inventory + two recovery passes.
    last.resolve(page())
    await Promise.all([recovery, repeated])
    await flush()
    expectDraft('Draft typed during recovery')
    expectMissing()
    await type('Still typing after deletion')
    await recover(page())
    expectDraft('Still typing after deletion')
    expectMissing()
    expect(api.skill).toHaveBeenCalledTimes(1)
  })

  it.each([
    { project_id: 'project-b', epoch: 'epoch-a' },
    { project_id: 'project-a', epoch: 'epoch-b' },
  ])('ignores a foreign gap %j without disturbing the draft', async (scope) => {
    await mount()
    await open()
    await type('Local project draft')
    api.skills.mockResolvedValue(page())
    await gap(scope)
    await flush()
    expect(api.skills).toHaveBeenCalledTimes(1)
    expectDraft('Local project draft')
  })

  it('updates a clean editor normally after recovery finds a newer file', async () => {
    await mount()
    await open()
    api.skill.mockResolvedValueOnce(detail('tester', 1))
    await recover(page(entry('tester', 1)))
    expect(textarea().value).toBe(detail('tester', 1).content)
    expect(textarea().closest('li')).toBe(card())
    expect(button(t('common.save')).disabled).toBe(true)
    expect(api.skill).toHaveBeenCalledTimes(2)
    expect(window.confirm).not.toHaveBeenCalled()
  })
})

describe('SkillsView search during scoped inventory recovery', () => {
  it('keeps the same focused dirty editor when recovered metadata no longer matches search', async () => {
    const original = { ...entry(), description: 'Original searchable description' }
    const writer = { ...entry('writer'), description: 'Another searchable skill' }
    await mount(original, writer)
    await searchFor('searchable')
    await open()
    const text = 'My unsaved instructions survive metadata changes'
    await type(text)
    const field = textarea()
    field.focus()
    field.setSelectionRange(3, 12, 'backward')

    for (const version of [1, 2]) {
      const current = {
        ...entry('tester', version), description: `Current unrelated description ${version}`,
        warning: `Current warning ${version}`, files: [`reference-${version}.md`], file_count: 1,
      }
      await recover(page(current, writer))
      expectRetainedEditor(field, text)
      expect(card().textContent).toContain(current.description)
      expect(card().textContent).toContain(current.warning)
      expect(card().textContent).not.toContain(original.description)
      expect(card().querySelector('[title="reference-' + version + '.md"]')).not.toBeNull()
      expect(card().textContent).not.toContain(t('skills.editor.deletedOnDisk', {
        name: 'tester', folder: '.voxarium_studio/skills',
      }))
      expect(button(t('common.save')).disabled).toBe(false)
    }

    // Matching again must reuse the pinned row, rather than adding a second selected card.
    await recover(page({ ...entry('tester', 3), description: 'Restored searchable description' }, writer))
    expectRetainedEditor(field, text)
    expect(card().textContent).toContain('Restored searchable description')
    expect(api.skill).toHaveBeenCalledTimes(1)
    expect(api.saveSkill).not.toHaveBeenCalled()
    expect(window.confirm).not.toHaveBeenCalled()

    await recover(page({ ...entry('tester', 4), description: 'Unrelated again' }, writer))
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    await click(button(t('common.close')))
    expectRetainedEditor(field, text)
    expect(window.confirm).toHaveBeenLastCalledWith(t('skills.editor.discardConfirm', { name: 'tester' }))
    await click(button(t('common.close')))
    expect(root.querySelector('textarea.skill-mono')).toBeNull()
    expect(root.querySelector('[title=".voxarium_studio/skills/tester"]')).toBeNull()
    expect(window.confirm).toHaveBeenCalledTimes(2)
    await searchFor('writer')
    await open('writer')
    expect(textarea().closest('li')).toBe(card('writer'))
  })

  it('still searches and explicitly switches skills while the selected draft is retained', async () => {
    await mount({ ...entry(), description: 'Searchable old description' }, entry('writer'))
    await searchFor('searchable')
    await open()
    await type('Draft before explicitly selecting another skill')
    await recover(page({ ...entry('tester', 1), description: 'New unrelated description' }, entry('writer')))
    expectDraft('Draft before explicitly selecting another skill')
    await searchFor('writer')
    expect(card('writer')).toBeDefined()
    expectDraft('Draft before explicitly selecting another skill')
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    await click(button(t('common.edit'), card('writer')))
    expect(api.skill).toHaveBeenCalledTimes(1)
    expectDraft('Draft before explicitly selecting another skill')
    await open('writer')
    expect(window.confirm).toHaveBeenCalledTimes(2)
    expect(api.skill).toHaveBeenLastCalledWith('writer')
    expect(root.querySelectorAll('textarea.skill-mono')).toHaveLength(1)
    expect(root.querySelector('[title=".voxarium_studio/skills/tester"]')).toBeNull()
    expect(api.saveSkill).not.toHaveBeenCalled()
  })

  it('retains a focused missing source under search and reuses its row when it returns nonmatching', async () => {
    await mount({ ...entry(), description: 'Searchable old description' }, entry('writer'))
    await searchFor('searchable')
    await open()
    const text = 'Local draft while source is missing'
    await type(text)
    const field = textarea()
    field.focus()
    field.setSelectionRange(3, 12, 'backward')
    for (let pass = 0; pass < 2; pass++) {
      await recover(page(entry('writer')))
      expectRetainedEditor(field, text)
      expectMissing()
      expect(button(t('common.save')).disabled).toBe(true)
    }
    await recover(page({ ...entry('tester', 1), description: 'Returned unrelated description' }, entry('writer')))
    expectRetainedEditor(field, text)
    expect(card().textContent).toContain('Returned unrelated description')
    expect(button(t('common.save')).disabled).toBe(false)
    expect(api.skill).toHaveBeenCalledTimes(1)
    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.saveSkill).not.toHaveBeenCalled()
  })
})

describe('SkillsView late detail/reload responses', () => {
  it.each([
    { missing: false, reloadFirst: false },
    { missing: false, reloadFirst: true },
    { missing: true, reloadFirst: false },
    { missing: true, reloadFirst: true },
  ])('does not cancel an explicit pending selection when the previous skill resyncs (%j)', async ({ missing, reloadFirst }) => {
    await mount(entry(), entry('writer'))
    await open()
    const selected = deferred<SkillDetail>()
    const background = deferred<SkillDetail>()
    api.skill.mockReturnValueOnce(selected.promise).mockReturnValueOnce(background.promise)
    await click(button(t('common.edit'), card('writer')))
    expect(api.skill).toHaveBeenNthCalledWith(2, 'writer')
    expect(textarea().closest('li')).toBe(card()) // A remains visible while selecting B.

    await recover(page(entry('tester', 1), entry('writer')))
    expect(api.skill).toHaveBeenNthCalledWith(3, 'tester')
    if (missing) {
      await recover(page(entry('writer')))
      expectMissing()
    }
    // Both background auto-reload and missing-source invalidation must leave B's request live.
    if (reloadFirst) {
      background.resolve(detail('tester', 1))
      await flush()
    }
    selected.resolve(detail('writer'))
    await flush()
    expect(textarea().closest('li')).toBe(card('writer'))
    expect(textarea().value).toBe(detail('writer').content)
    await type('Draft in the explicitly selected writer')
    if (!reloadFirst) {
      background.resolve(detail('tester', 1))
      await flush()
    }
    expectDraft('Draft in the explicitly selected writer', 'writer')
    expect(root.querySelectorAll('textarea.skill-mono')).toHaveLength(1)
    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.saveSkill).not.toHaveBeenCalled()
  })

  it('does not overwrite typing started while a clean editor reloads', async () => {
    await mount()
    await open()
    const late = deferred<SkillDetail>()
    api.skill.mockReturnValueOnce(late.promise)
    await recover(page(entry('tester', 1)))
    await type('Typed while reading the new file')
    late.resolve(detail('tester', 1))
    await flush()
    expectDraft('Typed while reading the new file')
    expect(button(t('common.save')).disabled).toBe(false)
  })

  it.each(['success', 'error'] as const)('ignores a late reload %s after the source disappears', async (outcome) => {
    await mount()
    await open()
    const late = deferred<SkillDetail>()
    api.skill.mockReturnValueOnce(late.promise)
    await recover(page(entry('tester', 1)))
    await type('Draft before source disappeared')
    await recover(page())
    if (outcome === 'success') late.resolve(detail('tester', 1))
    else late.reject(new Error('Obsolete detail failure'))
    await flush()
    expectDraft('Draft before source disappeared')
    expectMissing()
    expect(root.textContent).not.toContain('Obsolete detail failure')
    expect(button(t('common.save')).disabled).toBe(true)
  })

  it('keeps the newest reload when older detail requests finish last', async () => {
    await mount()
    await open()
    const older = deferred<SkillDetail>()
    const newer = deferred<SkillDetail>()
    api.skill.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    await recover(page(entry('tester', 1)))
    await recover(page(entry('tester', 2)))
    newer.resolve(detail('tester', 2))
    await flush()
    expect(textarea().value).toBe(detail('tester', 2).content)
    await type('Draft based on the latest file')
    older.resolve(detail('tester', 1))
    await flush()
    expectDraft('Draft based on the latest file')
  })

  it('does not replace a newly reopened editor for the same skill with an old reload', async () => {
    await mount()
    await open()
    const late = deferred<SkillDetail>()
    api.skill.mockReturnValueOnce(late.promise)
    await recover(page(entry('tester', 1)))
    await click(button(t('common.close')))
    api.skill.mockResolvedValueOnce(detail('tester', 1))
    await click(button(t('common.edit'), card()))
    await type('New editor draft')
    late.resolve(detail('tester', 1))
    await flush()
    expectDraft('New editor draft')
  })
})

describe('SkillsView explicit destructive actions', () => {
  it('asks before closing a missing dirty skill, preserving it on cancel and closing on accept', async () => {
    await mount()
    await open()
    await type('Draft worth keeping')
    await recover(page())
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    await click(button(t('common.close')))
    expect(window.confirm).toHaveBeenLastCalledWith(t('skills.editor.discardConfirm', { name: 'tester' }))
    expectDraft('Draft worth keeping')
    expectMissing()
    await click(button(t('common.close')))
    expect(root.querySelector('textarea.skill-mono')).toBeNull()
    expect(window.confirm).toHaveBeenCalledTimes(2)
    expect(api.saveSkill).not.toHaveBeenCalled()
  })

  it('still closes a dirty editor after an explicitly confirmed delete', async () => {
    await mount()
    await open()
    await type('Draft intentionally deleted')
    api.deleteSkill.mockResolvedValue(page())
    const remove = [...card().querySelectorAll('button')].find((node) => node.title === t('skills.list.deleteTitle'))
    expect(remove).toBeDefined()
    await click(remove!)
    const dialog = document.body.querySelector('[role="alertdialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog!.textContent).toContain('.voxarium_studio/skills/tester/SKILL.md')
    await click(button(t('common.delete'), dialog!))
    expect(api.deleteSkill).toHaveBeenCalledExactlyOnceWith('tester')
    expect(root.querySelector('textarea.skill-mono')).toBeNull()
    expect(root.textContent).not.toContain(t('skills.editor.deletedOnDisk', { name: 'tester', folder: '.voxarium_studio/skills' }))
    expect(window.confirm).not.toHaveBeenCalled()
  })
})

const { rendererCloseBarrier } = await import('@/desktop/close-barrier')

describe('Skills mounted close barrier', () => {
  it('clean close does not write, cancel preserves the editor, accept does not unmount', async () => {
    await mount()
    await open()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(api.saveSkill).not.toHaveBeenCalled()
    await type('Unsaved draft')
    const field = textarea()
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(textarea()).toBe(field)
    expect(textarea().value).toBe('Unsaved draft')
    expect(api.saveSkill).not.toHaveBeenCalled()
  })
  it('unfinished and failed save cannot close even with discard accepted', async () => {
    await mount()
    await open()
    await type('Save draft')
    const saving = deferred<SkillDetail>()
    api.saveSkill.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    saving.reject(new Error('Disk full'))
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(textarea().value).toBe('Save draft')
  })
  it('continued typing is dirty after delayed success and still asks before close', async () => {
    await mount()
    await open()
    await type('Sent draft')
    const saving = deferred<SkillDetail>()
    api.saveSkill.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    await type('Newer draft')
    saving.resolve({ ...detail('tester', 1), content: 'Sent draft' })
    await flush()
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(textarea().value).toBe('Newer draft')
    expect(window.confirm).toHaveBeenCalledTimes(1)
  })
})

describe('Skills close approval validity', () => {
  it('revokes confirmed discard when typing continues while another guard awaits', async () => {
    await mount()
    await open()
    await type('Accepted draft')
    const later = deferred<boolean>()
    const unregister = rendererCloseBarrier.register(() => later.promise)
    try {
      const closing = rendererCloseBarrier.prepare()
      await flush()
      expect(window.confirm).toHaveBeenCalledTimes(1)
      await type('New draft after confirmation')
      later.resolve(true)
      expect(await closing).toBe(false)
      expectDraft('New draft after confirmation')
    } finally {
      unregister()
    }
  })

  it('rechecks approved primitives after prepare, including a newly started save', async () => {
    await mount()
    await open()
    await type('Accepted draft')
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    const saving = deferred<SkillDetail>()
    api.saveSkill.mockReturnValueOnce(saving.promise)
    await click(button(t('common.save')))
    expect(rendererCloseBarrier.validate()).toBe(false)
    saving.resolve({ ...detail('tester', 1), content: 'Accepted draft' })
    await flush()
    // Even a completed write needs a new prepare, not stale approval from before it started.
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    await type('Later transport-time edit')
    expect(rendererCloseBarrier.validate()).toBe(false)
  })

  it('confirms a create draft without unmounting it and revokes approval for new typing', async () => {
    await mount()
    await click(button(t('skills.create.button')))
    const form = root.querySelector('form')!
    const field = form.querySelector('input')!
    field.value = 'new-skill'
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    vi.mocked(window.confirm).mockReturnValueOnce(false).mockReturnValueOnce(true)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    expect(root.querySelector('form')).toBe(form)
    field.value = 'later-skill'
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(api.createSkill).not.toHaveBeenCalled()
  })
})


/** Pipeline fixtures live beside the form guards so their mounted close regressions stay permanent. */
describe('PipelineEditorView mounted close barrier', () => {
  let PipelineEditor: Component
  const pipelineMocks = {
    pipelines: { load: vi.fn(), save: vi.fn(), subscribe: vi.fn(), colorFor: vi.fn(() => 'blue'), nodeTypes: [], nodeTypeById: new Map(), issues: [] },
    runs: { subscribe: vi.fn(), watchPipeline: vi.fn(), loadHistory: vi.fn(), openRun: vi.fn(), start: vi.fn(), history: [], nodeStatus: {}, nodeError: {}, fromCache: {}, nodeOutputs: {}, nodeProgress: {}, currentRun: null },
  }

  beforeAll(async () => {
    vi.doMock('@/stores/pipelines', () => ({ usePipelinesStore: () => pipelineMocks.pipelines }))
    vi.doMock('@/stores/runs', () => ({ useRunsStore: () => pipelineMocks.runs }))
    vi.doMock('@/stores/theme', () => ({ useThemeStore: () => ({ canvasGrid: 'none' }) }))
    vi.doMock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
    vi.doMock('@/plugin-runtime/loader', () => ({ pluginComponent: vi.fn() }))
    vi.doMock('@/plugin-runtime/node-params', () => ({ provideNodeParamWriter: () => () => {} }))
    vi.doMock('@vue-flow/core', () => ({
      VueFlow: { render: () => null },
      useVueFlow: () => ({
        onConnect: () => {}, onNodeClick: () => {}, onPaneClick: () => {},
        screenToFlowCoordinate: () => ({ x: 0, y: 0 }), fitView: () => {}, getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
      }),
    }))
    vi.doMock('@/components/pipeline/LogPanel.vue', () => ({ default: { render: () => null } }))
    vi.doMock('@/components/pipeline/RunHistory.vue', () => ({ default: { render: () => null } }))
    vi.doMock('@/components/pipeline/NodePalette.vue', async () => {
      const { h } = await import('vue')
      return {
        default: {
          emits: ['add'],
          setup(_: unknown, { emit }: { emit: (event: string, value: unknown) => void }) {
            return () => h('button', {
              id: 'add-node', onClick: () => emit('add', { id: 'test', name: 'Test' }),
            }, 'Add node')
          },
        },
      }
    })
    vi.doMock('@/components/ui/ResizeHandle.vue', () => ({ default: { render: () => null } }))
    vi.doMock('@/components/ui/StatusBadge.vue', () => ({ default: { render: () => null } }))
    vi.doMock('@/node-renderer/NodeConfigPanel.vue', () => ({ default: { render: () => null } }))
    vi.doMock('@/node-renderer/GenericNode.vue', () => ({ default: { render: () => null } }))
    PipelineEditor = (await import('./PipelineEditorView.vue')).default
  })

  afterAll(() => {
    vi.doUnmock('@/stores/pipelines')
    vi.doUnmock('@/stores/runs')
    vi.doUnmock('@/stores/theme')
    vi.doUnmock('vue-router')
    vi.doUnmock('@/plugin-runtime/loader')
    vi.doUnmock('@/plugin-runtime/node-params')
    vi.doUnmock('@vue-flow/core')
    vi.doUnmock('@/components/pipeline/LogPanel.vue')
    vi.doUnmock('@/components/pipeline/RunHistory.vue')
    vi.doUnmock('@/components/pipeline/NodePalette.vue')
    vi.doUnmock('@/components/ui/ResizeHandle.vue')
    vi.doUnmock('@/components/ui/StatusBadge.vue')
    vi.doUnmock('@/node-renderer/NodeConfigPanel.vue')
    vi.doUnmock('@/node-renderer/GenericNode.vue')
  })
  async function addPipelineNode() {
    await click(root.querySelector<HTMLButtonElement>('#add-node')!)
  }
  beforeEach(async () => {
    vi.clearAllMocks()
    pipelineMocks.pipelines.load.mockResolvedValue({ id: 'test', name: 'Test', description: '', version: '1', nodes: [], edges: [], viewport: {} })
    pipelineMocks.pipelines.save.mockResolvedValue(undefined)
    app = createApp(PipelineEditor, { id: 'test' }).use(pinia)
    app.mount(root)
    await flush()
  })

  it('does not write clean pipelines on close', async () => {
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(pipelineMocks.pipelines.save).not.toHaveBeenCalled()
  })
  it('revokes pipeline approval for edits while another guard is awaiting', async () => {
    await addPipelineNode()
    const later = deferred<boolean>()
    const unregister = rendererCloseBarrier.register(() => later.promise)
    try {
      const closing = rendererCloseBarrier.prepare()
      await flush()
      expect(pipelineMocks.pipelines.save).toHaveBeenCalledTimes(1)
      await addPipelineNode()
      later.resolve(true)
      expect(await closing).toBe(false)
      expect(rendererCloseBarrier.validate()).toBe(false)
    } finally {
      unregister()
    }
  })
  it('awaits a close-triggered save, coalesces repeated close and permits only success', async () => {
    await addPipelineNode()
    const saving = deferred<void>()
    pipelineMocks.pipelines.save.mockReturnValueOnce(saving.promise)
    const first = rendererCloseBarrier.prepare()
    const second = rendererCloseBarrier.prepare()
    expect(second).toBe(first)
    let settled = false
    void first.then(() => { settled = true })
    await flush()
    expect(settled).toBe(false)
    expect(pipelineMocks.pipelines.save).toHaveBeenCalledTimes(1)
    saving.resolve()
    expect(await first).toBe(true)
    expect(root.querySelector('#add-node')).not.toBeNull()
  })
  it('rejects close on failed save and keeps dirty state', async () => {
    await addPipelineNode()
    pipelineMocks.pipelines.save.mockRejectedValueOnce(new Error('Disk full'))
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    await flush()
    expect(root.textContent).toContain('Disk full')
    expect(root.textContent).toContain(t('pipelines.editor.unsaved'))
  })
  it('retains edits made during save and refuses false success', async () => {
    await addPipelineNode()
    const saving = deferred<void>()
    pipelineMocks.pipelines.save.mockReturnValueOnce(saving.promise)
    const closing = rendererCloseBarrier.prepare()
    await flush()
    await addPipelineNode()
    saving.resolve()
    expect(await closing).toBe(false)
    await flush()
    expect(root.textContent).toContain(t('pipelines.editor.unsaved'))
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(pipelineMocks.pipelines.save).toHaveBeenCalledTimes(2)
  })
  it('fails closed during manual save and does not start run after failed save', async () => {
    await addPipelineNode()
    const saving = deferred<void>()
    pipelineMocks.pipelines.save.mockReturnValueOnce(saving.promise)
    button(t('common.save')).click()
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    saving.reject(new Error('Disk full'))
    await flush()
    pipelineMocks.pipelines.save.mockRejectedValueOnce(new Error('Disk full'))
    button(t('pipelines.editor.run')).click()
    await flush()
    expect(pipelineMocks.runs.start).not.toHaveBeenCalled()
  })
})
