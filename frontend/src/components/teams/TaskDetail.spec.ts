// @vitest-environment jsdom
/** Mounted §5.3 regressions over the actual task form, autosave and upload guards. */
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskDetail as Task, TaskUpdate } from '@/api/types'
import { rendererCloseBarrier } from '@/desktop/close-barrier'
import { projectStorageKey } from '@/desktop/project-storage'
import { t } from '@/i18n'

const transport = vi.hoisted(() => ({ upload: vi.fn(), reviewUpload: vi.fn() }))
vi.mock('@/api/client', () => ({
  api: { removeTaskAttachment: vi.fn() }, uploadAttachment: transport.upload,
  uploadTaskAttachment: transport.reviewUpload,
  attachmentUrl: (reference: string) => `/attachment/${reference}`,
  taskAttachmentUrl: () => '/task-attachment',
}))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ project: { root: 'D:/close-task' } }) }))
vi.mock('@/stores/teams', () => ({ useTeamsStore: () => teams }))
// Keep before/controls slots: the actual brief AttachmentPicker is in #before.
vi.mock('./ConversationPanel.vue', () => ({ default: { template: '<div><slot name="before" /><slot name="controls" /></div>' } }))
vi.mock('./MemberMark.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./MemberSelect.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./DependencyPicker.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./RunOverrides.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./SessionStatus.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./SubagentSummary.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./TaskChanges.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./TaskDiff.vue', () => ({ default: { template: '<span />' } }))
vi.mock('./TaskWritePaths.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/ui/TokenUsageChip.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/ui/ContextUsageBar.vue', () => ({ default: { template: '<span />' } }))
vi.mock('@/components/ui/AgentBackendIcon.vue', () => ({ default: { template: '<span />' } }))

const { default: TaskDetail } = await import('./TaskDetail.vue')

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
async function flush() {
  for (let step = 0; step < 6; step++) { await Promise.resolve(); await nextTick() }
}
function taskFixture(): Task {
  return {
    id: 'task', number: 50, display_id: 'VS-50', team_id: 'team', assignee_id: 'agent',
    created_by: null, title: 'Task title', brief: 'Original brief', status: 'todo',
    write_paths: [], depends_on: [], auto_start: false, resume_on_limit: false,
    limit_reset_at: null, limit_resume_at: null, model: null, effort: null,
    agent_session_id: null, remote_control_url: null, worktree_path: null,
    review_comment: null, result_summary: null, result_type: null, error: null,
    total_cost_usd: 0, total_tokens: 0, num_turns: 0, archived: false,
    created_at: '2026-11-13T00:00:00Z', started_at: null, finished_at: null,
    messages: [], assignee: null, subagents: [],
  }
}
let app: App | undefined
let root: HTMLDivElement
let task: Task
let teams: ReturnType<typeof teamsFixture>
function teamsFixture() {
  return reactive({
    team: { id: 'team' }, members: [], memberById: new Map(), tasks: [], busy: false,
    error: null as string | null, compressingTaskId: null, cancellingCompressionTaskId: null,
    loadOptions: vi.fn(), managerOf: () => null, isHuman: () => false,
    updateTask: vi.fn(async (_id: string, changes: TaskUpdate) => { Object.assign(task, changes); return true }),
    act: vi.fn(), messageTask: vi.fn(), remoteControl: vi.fn(),
  })
}
async function mount() {
  app = createApp({ render: () => h(TaskDetail, { task, embedded: true }) })
  app.mount(root)
  await flush()
}
function button(label: string, within: ParentNode = root) {
  const found = [...within.querySelectorAll<HTMLButtonElement>('button')].find(element => element.textContent?.trim() === label)
  expect(found, label).toBeDefined()
  return found!
}
async function edit() {
  root.querySelector<HTMLButtonElement>(`button[title="${t('taskDetail.header.edit')}"]`)!.click()
  await flush()
}
async function type(selector: string, value: string) {
  const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!
  expect(field, selector).not.toBeNull()
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flush()
  return field
}
async function pick(input: HTMLInputElement) {
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['image'], 'screen.png')] })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flush()
}

describe('task brief Markdown', () => {
  it('renders formatting and keeps the original Markdown in the editor', async () => {
    task.brief = '# Goal\n\n**Important** and *useful* with `code`.\n\n- First\n- Second\n\n[Guide](https://example.com)\n\n```ts\nconst ready = true\n```'
    await mount()
    const brief = root.querySelector('.markdown-body')!
    expect(brief.querySelector('h1')?.textContent).toBe('Goal')
    expect(brief.querySelector('strong')?.textContent).toBe('Important')
    expect(brief.querySelector('em')?.textContent).toBe('useful')
    expect(brief.querySelector('code')?.textContent).toBe('code')
    expect(brief.querySelectorAll('li')).toHaveLength(2)
    expect(brief.querySelector('a')?.getAttribute('href')).toBe('https://example.com')
    expect(brief.querySelector('pre code')?.textContent).toBe('const ready = true')
    await edit()
    expect(root.querySelector<HTMLTextAreaElement>('form textarea')?.value).toBe(task.brief)
    expect(teams.updateTask).not.toHaveBeenCalled()
  })

  it('updates rendered text, escapes HTML and rejects unsafe links', async () => {
    task.brief = '<script>alert(1)</script>\n\n[unsafe](javascript:alert)'
    await mount()
    const brief = root.querySelector('.markdown-body')!
    expect(brief.querySelector('script')).toBeNull()
    expect(brief.querySelector('a')).toBeNull()
    expect(brief.textContent).toContain('<script>alert(1)</script>')
    task.brief = '**Updated**'
    await flush()
    expect(brief.querySelector('strong')?.textContent).toBe('Updated')
    task.brief = ''
    await flush()
    expect(brief.textContent).toBe('—')
  })
})

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  localStorage.clear()
  task = reactive(taskFixture())
  teams = teamsFixture()
  root = document.createElement('div')
  document.body.append(root)
})
afterEach(async () => {
  vi.restoreAllMocks()
  app?.unmount()
  app = undefined
  root.remove()
  vi.useRealTimers()
  expect(await rendererCloseBarrier.prepare()).toBe(true)
})

describe('mounted TaskDetail close guard', () => {
  it('revalidates late title/brief edits and pending actions without flushing new work', async () => {
    await mount()
    await edit()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(rendererCloseBarrier.validate()).toBe(true)
    await type('form input:not([type=checkbox])', 'Late title')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(teams.updateTask).not.toHaveBeenCalled()
    button(t('common.cancel'), root.querySelector('form')!).click()
    await flush()
    await edit()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    await type('form textarea', 'Late brief')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(teams.updateTask).not.toHaveBeenCalled()
    await type('form textarea', task.brief)
    expect(rendererCloseBarrier.validate()).toBe(true)
    teams.busy = true
    expect(rendererCloseBarrier.validate()).toBe(false)
    teams.busy = false
    teams.error = 'New save failed'
    expect(rendererCloseBarrier.validate()).toBe(false)
  })

  it('retains dirty explicit-save fields until existing Cancel is chosen, with no new prompt', async () => {
    await mount()
    const confirm = vi.spyOn(window, 'confirm')
    await edit()
    await type('form input:not([type=checkbox])', 'Draft title')
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(root.querySelector('form')).not.toBeNull()
    expect(task.title).toBe('Task title')
    button(t('common.cancel'), root.querySelector('form')!).click()
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(teams.updateTask).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('drains a delayed brief save once and does not unmount or close the editor', async () => {
    await mount()
    await edit()
    await type('form textarea', 'Last sentence')
    const save = deferred<boolean>()
    teams.updateTask.mockImplementationOnce(async (_id, changes) => {
      const result = await save.promise
      if (result) Object.assign(task, changes)
      return result
    })
    let settled = false
    const closing = rendererCloseBarrier.prepare().then(value => { settled = true; return value })
    await flush()
    expect(settled).toBe(false)
    expect(teams.updateTask).toHaveBeenCalledExactlyOnceWith('task', { brief: 'Last sentence' })
    expect(root.querySelector('form textarea')).not.toBeNull()
    save.resolve(true)
    expect(await closing).toBe(true)
    expect(task.brief).toBe('Last sentence')
    expect(root.querySelector('form')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(1200)
    expect(teams.updateTask).toHaveBeenCalledTimes(1)
  })

  it.each(['false', 'reject'] as const)('fails closed after a brief save returns %s, retaining typed text', async mode => {
    await mount()
    await edit()
    await type('form textarea', 'Keep failed brief')
    if (mode === 'false') teams.updateTask.mockResolvedValueOnce(false)
    else teams.updateTask.mockRejectedValueOnce(new Error('Save refused'))
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(root.querySelector<HTMLTextAreaElement>('form textarea')?.value).toBe('Keep failed brief')
    expect(task.brief).toBe('Original brief')
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    // Existing typing/retry path must recover even after a rejected serial save.
    await type('form textarea', 'Retried brief')
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    expect(task.brief).toBe('Retried brief')
  })

  it('waits for an already-running brief save after Cancel rather than ACKing the empty debounce', async () => {
    await mount()
    await edit()
    await type('form textarea', 'Cancelled editor, durable brief')
    const save = deferred<boolean>()
    teams.updateTask.mockImplementationOnce(async (_id, changes) => {
      const result = await save.promise
      if (result) Object.assign(task, changes)
      return result
    })
    button(t('common.cancel'), root.querySelector('form')!).click()
    await flush()
    let settled = false
    const closing = rendererCloseBarrier.prepare().then(value => { settled = true; return value })
    await flush()
    expect(settled).toBe(false)
    save.resolve(true)
    expect(await closing).toBe(true)
  })

  it('blocks a pending manual Save and allows close once that save is confirmed', async () => {
    await mount()
    await edit()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    await type('form input:not([type=checkbox])', 'Saved title')
    const save = deferred<boolean>()
    teams.updateTask.mockImplementationOnce(async (_id, changes) => {
      const result = await save.promise
      if (result) Object.assign(task, changes)
      return result
    })
    root.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    // The reactive task can be refreshed before the original save/refresh finishes.
    // Even with no remaining patch, editSaving invalidates the old approval.
    task.title = 'Saved title'
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    save.resolve(true)
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
  })

  it('accepts already-persisted comments but refuses storage failure', async () => {
    task.status = 'needs_review'
    await mount()
    const field = await type('textarea', 'Review draft')
    const key = projectStorageKey('D:/close-task', 'voxarium_studio.task.comment.task')!
    expect(localStorage.getItem(key)).toBe('Review draft')
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    await type('textarea', 'Cannot persist this')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(field.value).toBe('Cannot persist this')
    storage.mockRestore()
  })

  it('keeps real brief attachment upload pending/error closed until the existing retry succeeds', async () => {
    await mount()
    expect(await rendererCloseBarrier.prepare()).toBe(true)
    const upload = deferred<never>()
    transport.upload.mockReturnValueOnce(upload.promise)
    const picker = root.querySelector<HTMLInputElement>('input[type=file]')!
    await pick(picker)
    expect(rendererCloseBarrier.validate()).toBe(false)
    upload.reject(new Error('Brief upload refused'))
    await flush()
    expect(root.querySelector('[role=alert]')?.textContent).toBe('Brief upload refused')
    expect(rendererCloseBarrier.validate()).toBe(false)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    transport.upload.mockResolvedValueOnce({ reference: 'stored', name: 'screen.png', image: false, size: 5 })
    await pick(picker)
    expect(root.querySelector('[role=alert]')).toBeNull()
    expect(teams.updateTask).toHaveBeenCalledWith('task', { attachments: ['stored'] })
    expect(await rendererCloseBarrier.prepare()).toBe(true)
  })

  it('keeps review attachment upload failure closed without clearing the comment', async () => {
    task.status = 'needs_review'
    await mount()
    await type('textarea', 'Review stays')
    const upload = deferred<never>()
    transport.reviewUpload.mockReturnValueOnce(upload.promise)
    await pick(root.querySelectorAll<HTMLInputElement>('input[type=file]')[1]!)
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    upload.reject(new Error('Review upload refused'))
    await flush()
    expect(await rendererCloseBarrier.prepare()).toBe(false)
    expect(root.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Review stays')
  })
})

// §10.8: all TaskDetail hosts share the header dropdown, never a footer widget.
describe('remote control placement', () => {
  it('puts the dropdown before edit and close, and routes toggles to the current task', async () => {
    task.is_running = true
    await mount()
    const header = root.querySelector('header')!
    const actions = [...header.querySelectorAll<HTMLButtonElement>('button')]
    const remote = actions.findIndex(element => element.title === t('taskDetail.remote.title'))
    const edit = actions.findIndex(element => element.title === t('taskDetail.header.edit'))
    const close = actions.findIndex(element => element.title === t('common.close'))
    expect(remote).toBeGreaterThanOrEqual(0)
    expect(remote).toBeLessThan(edit)
    expect(edit).toBeLessThan(close)
    expect(root.querySelector('footer')?.textContent).not.toContain(t('taskDetail.remote.title'))
    expect(root.querySelector('[role=menu]')).toBeNull()
    actions[remote]!.click()
    await flush()
    const toggle = root.querySelector<HTMLButtonElement>('[role=menuitemcheckbox]')!
    expect(toggle.disabled).toBe(false)
    toggle.click()
    await flush()
    expect(teams.remoteControl).toHaveBeenCalledWith('task', true)
  })

  it('does not offer session controls for a human task', async () => {
    task.assignee = { kind: 'human' } as NonNullable<Task['assignee']>
    await mount()
    expect(root.querySelector(`button[title="${t('taskDetail.remote.title')}"]`)).toBeNull()
    expect(root.querySelector('footer')?.textContent).not.toContain(t('taskDetail.remote.title'))
  })
})