// @vitest-environment jsdom
/** VS-144: Files touched is a folded subsection, not a separate scrolling pane (§10.4). */
import { createPinia } from 'pinia'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { TaskChanges as Changes } from '@/api/types'
import { t } from '@/i18n'
import TaskChanges from './TaskChanges.vue'

vi.mock('@/api/client', () => ({ api: { taskChanges: vi.fn(), taskCommit: vi.fn() } }))
vi.mock('@/ws/connection', () => ({ onEvent: () => () => {}, ws: { state: 'connected' } }))

const fixture = (): Changes => ({
  source: 'tools', root: 'D:/project', workdir: 'D:/project', branch: null,
  files: [{ path: 'src/main.ts', kind: 'modified', staged: false, origin: null, insertions: 2, deletions: 0 }],
  temp_files: [], temp_root: null, error: null, reason: null,
})

let app: App | undefined
const select = vi.fn()
let host: HTMLDivElement
let props: { teamId: string; taskId: string; selected: string | null; revision: number }
async function flush() {
  for (let step = 0; step < 4; step++) { await Promise.resolve(); await nextTick() }
}
async function mount() {
  app = createApp({ render: () => h(TaskChanges, { ...props, onSelect: select }) })
  app.use(createPinia()).mount(host)
  await flush()
}
const toggle = () => host.querySelector<HTMLButtonElement>('[data-files-toggle]')!

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(api.taskChanges).mockResolvedValue(fixture())
  vi.mocked(api.taskCommit).mockRejectedValue(new Error('No commit draft'))
  host = document.createElement('div')
  props = reactive({ teamId: 'team', taskId: 'task', selected: null, revision: 0 })
})
afterEach(() => {
  app?.unmount()
  app = undefined
  localStorage.clear()
})

describe('Files touched in Status', () => {
  it('starts folded with a count, no adjacent refresh button and no nested scroller', async () => {
    await mount()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(toggle().textContent).toContain(t('tasks.changes.touched'))
    expect(toggle().textContent).toContain('1')
    expect(host.textContent).not.toContain('main.ts')
    expect(toggle().parentElement!.querySelectorAll('button')).toHaveLength(1)
    expect(host.querySelector('[class*="overflow-y-"]')).toBeNull()
    expect(host.querySelector('[data-status-files]')!.classList.contains('flex-1')).toBe(false)
    // Commit controls remain available independently of the file-list fold.
    expect(host.querySelector(`section[aria-label="${t('tasks.commit.heading')}"]`)).not.toBeNull()
  })

  it('opens and closes the list, preserves selection and remembers the reader choice', async () => {
    await mount()
    toggle().click()
    await flush()
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('button[title="src/main.ts"]')).not.toBeNull()
    host.querySelector<HTMLButtonElement>('button[title="src/main.ts"]')!.click()
    expect(select).toHaveBeenLastCalledWith('src/main.ts')
    props.selected = 'src/main.ts'
    await flush()
    expect(localStorage.getItem('voxarium_studio.status.files')).toBe('open')
    app!.unmount()
    await mount()
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    toggle().click()
    await flush()
    expect(host.querySelector('button[title="src/main.ts"]')).toBeNull()
    expect(localStorage.getItem('voxarium_studio.status.files')).toBe('closed')
    expect(props.selected).toBe('src/main.ts')
  })

  it('automatically reloads a changed revision while folded without reopening the list', async () => {
    await mount()
    vi.mocked(api.taskChanges).mockResolvedValue({ ...fixture(), files: [] })
    props.revision++
    await flush()
    expect(api.taskChanges).toHaveBeenCalledTimes(2)
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(toggle().textContent).toContain('0')
  })

  it('keeps load errors visible even when the file list is folded', async () => {
    vi.mocked(api.taskChanges).mockRejectedValue(new Error('Cannot read task changes'))
    await mount()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(host.textContent).toContain('Cannot read task changes')
  })
})
