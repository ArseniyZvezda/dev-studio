/**
 * The board's two live figures: what the finished work cost, and what each agent is doing.
 *
 * Both are derived rather than fetched, and the derivation is the part worth pinning. The
 * cost has to ignore work in flight, and the activity line has to come off the message
 * stream for *every* task rather than only the open one — which is the half a browser check
 * cannot reach without billing a real session, since nothing but a live agent emits those
 * events.
 *
 * The API and the socket are stubs, so what is under test is the store's decisions.
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentMessage, Team, TeamDetail, TeamMember, TeamTask } from '@/api/types'
import { liveLine } from '@/lib/transcript'

const api = {
  teams: vi.fn(),
  team: vi.fn(),
  setAutostartTasks: vi.fn(),
  startAllTasks: vi.fn(),
  compressTask: vi.fn(),
  cancelTask: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  deleteMember: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

/** Captured so a test can play an event the way the socket would. */
const handlers = new Map<string, (payload: never) => void>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: (payload: never) => void) => {
    handlers.set(type, handler)
  }),
}))

const { useTeamsStore } = await import('./teams')

function task(overrides: Partial<TeamTask> = {}): TeamTask {
  return {
    id: 'task_1',
    number: 1,
    display_id: 'DP-01',
    team_id: 'team_1',
    assignee_id: 'member_1',
    created_by: null,
    title: 'Tune the dash',
    brief: '',
    write_paths: [],
    status: 'in_progress',
    depends_on: [],
    auto_start: true,
    resume_on_limit: false,
    limit_reset_at: null,
    limit_resume_at: null,
    model: null,
    effort: null,
    agent_session_id: null,
    remote_control_url: null,
    worktree_path: null,
    review_comment: null,
    result_summary: null,
    result_type: null,
    error: null,
    total_cost_usd: 0,
    num_turns: 0,
    total_tokens: 0,
    archived: false,
    created_at: '2026-08-24T10:00:00+00:00',
    started_at: null,
    finished_at: null,
    is_running: true,
    ...overrides,
  }
}

/** The event the backend publishes for every task, not only the one on screen. */
function messageEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'msg_1',
    task_id: 'task_1',
    member_id: 'member_1',
    role: 'assistant',
    kind: 'tool_use',
    text: 'Bash',
    data: { name: 'Bash', input: { command: 'npm test' } },
    created_at: '2026-08-24T10:00:01+00:00',
    ...overrides,
  }
}

async function storeWith(tasks: TeamTask[]) {
  const team: TeamDetail = {
    id: 'team_1',
    name: 'Default',
    description: '',
    autostart_tasks: false,
    created_at: '2026-08-24T09:00:00+00:00',
    members: [],
    tasks,
  }
  api.teams.mockResolvedValue([team as Team])
  api.team.mockResolvedValue(team)
  const store = useTeamsStore()
  await store.load()
  store.subscribe()
  return store
}

beforeEach(() => {
  setActivePinia(createPinia())
  handlers.clear()
  for (const stub of Object.values(api)) stub.mockReset()
})

describe('explicit write-file declarations', () => {
  it('sends only the declared write files, despite shared read references in the brief', async () => {
    const store = await storeWith([])
    await store.createTask({
      assigneeId: 'member_1', title: 'Read docs/design.md',
      brief: 'Use README.md for context; change src/player.py.',
      writePaths: ['src/player.py'], autoStart: false,
    })
    expect(api.createTask).toHaveBeenCalledExactlyOnceWith('team_1', expect.objectContaining({
      write_paths: ['src/player.py'],
    }))
  })

  it('defaults to no write files and sends an explicit empty list when clearing an existing list', async () => {
    const store = await storeWith([task({ write_paths: ['src/player.py'], is_running: false })])
    await store.createTask({
      assigneeId: 'member_1', title: 'Review README.md', brief: 'Read src/player.py.', autoStart: false,
    })
    expect(api.createTask).toHaveBeenCalledExactlyOnceWith('team_1', expect.objectContaining({
      write_paths: [],
    }))
    expect(await store.updateTask('task_1', { write_paths: [] })).toBe(true)
    expect(api.updateTask).toHaveBeenCalledExactlyOnceWith('team_1', 'task_1', { write_paths: [], actor_id: null })
  })
})

describe('confirmed member removal', () => {
  it('transfers tasks only with explicit confirmation and refreshes the board', async () => {
    const assigned = task({ status: 'done', is_running: false, archived: true })
    const store = await storeWith([assigned])
    store.editingMemberId = 'member_1'
    api.team.mockResolvedValueOnce({
      ...store.team,
      tasks: [{ ...assigned, assignee_id: 'team_1__director' }],
    })
    expect(await store.removeMember('member_1', true)).toBe(true)
    expect(api.deleteMember).toHaveBeenCalledExactlyOnceWith('team_1', 'member_1', true)
    expect(store.tasks[0]?.assignee_id).toBe('team_1__director')
    expect(store.tasks[0]?.archived).toBe(true)
    expect(store.editingMemberId).toBeNull()
  })

  it('retains the member editor and tasks when deletion fails', async () => {
    const store = await storeWith([task()])
    store.editingMemberId = 'member_1'
    api.deleteMember.mockRejectedValueOnce(new Error('Cannot stop the running task'))
    expect(await store.removeMember('member_1')).toBe(false)
    expect(api.deleteMember).toHaveBeenCalledExactlyOnceWith('team_1', 'member_1', false)
    expect(store.editingMemberId).toBe('member_1')
    expect(store.tasks[0]?.assignee_id).toBe('member_1')
    expect(store.error).toBe('Cannot stop the running task')
    expect(store.busy).toBe(false)
  })
})

describe('context compression progress', () => {
  it('allows cancellation while Compress owns busy and prevents duplicate cancellation', async () => {
    const store = await storeWith([task({ status: 'pending', is_running: true, is_compacting: true })])
    let finishCompression!: () => void
    let finishCancellation!: () => void
    api.compressTask.mockImplementationOnce(() => new Promise<void>((resolve) => { finishCompression = resolve }))
    api.cancelTask.mockImplementationOnce(() => new Promise<void>((resolve) => { finishCancellation = resolve }))
    const compression = store.act('compress', 'task_1')
    const cancellation = store.cancelCompression('task_1')
    expect(store.busy).toBe(true)
    expect(store.cancellingCompressionTaskId).toBe('task_1')
    expect(api.cancelTask).toHaveBeenCalledExactlyOnceWith('team_1', 'task_1')
    expect(await store.cancelCompression('task_1')).toBe(false)
    finishCancellation()
    expect(await cancellation).toBe(true)
    expect(store.cancellingCompressionTaskId).toBeNull()
    expect(store.busy).toBe(true)
    expect(store.compressingTaskId).toBe('task_1')
    finishCompression()
    await compression
    expect(store.busy).toBe(false)
  })

  it('keeps compression active when cancellation fails', async () => {
    const store = await storeWith([task({ is_compacting: true })])
    api.cancelTask.mockRejectedValueOnce(new Error('Cannot cancel'))
    expect(await store.cancelCompression('task_1')).toBe(false)
    expect(store.error).toBe('Cannot cancel')
    expect(store.cancellingCompressionTaskId).toBeNull()
  })

  it.each([true, false])('stays busy throughout the request and clears on success=%s', async (success) => {
    const store = await storeWith([task({ status: 'pending', is_running: false })])
    let finish!: () => void
    api.compressTask.mockImplementationOnce(() => new Promise<void>((resolve, reject) => {
      finish = () => success ? resolve() : reject(new Error('Compression failed'))
    }))
    const request = store.act('compress', 'task_1')
    expect(store.compressingTaskId).toBe('task_1')
    expect(store.busy).toBe(true)
    await Promise.resolve()
    expect(store.compressingTaskId).toBe('task_1')
    finish()
    expect(await request).toBe(success)
    expect(store.compressingTaskId).toBeNull()
    expect(store.busy).toBe(false)
    expect(api.compressTask).toHaveBeenCalledWith('team_1', 'task_1', null)
  })
})

describe('board-wide release', () => {
  it('offers only idle agent tasks in Todo or Pending, including queued dependencies', async () => {
    const store = await storeWith([
      task({ id: 'todo', status: 'todo', is_running: false }),
      task({ id: 'queued', status: 'pending', is_running: false, depends_on: ['upstream'] }),
      task({ id: 'submitted', status: 'pending', is_running: true }),
      task({ id: 'human', status: 'todo', is_running: false, assignee_id: 'human' }),
      task({ id: 'review', status: 'needs_review', is_running: false }),
      task({ id: 'archived', status: 'done', is_running: false, archived: true }),
      task({ id: 'blocked', status: 'blocked', is_running: false }),
    ])
    store.teams[0]!.members = [
      { id: 'member_1', kind: 'agent' }, { id: 'human', kind: 'human' },
    ] as TeamMember[]
    expect(store.startableTasks.map((entry) => entry.id)).toEqual(['todo', 'queued'])
  })

  it('keeps the saved autostart value on failure and applies cross-window changes', async () => {
    const store = await storeWith([])
    api.setAutostartTasks.mockRejectedValueOnce(new Error('Cannot save'))
    expect(await store.setAutostartTasks(true)).toBe(false)
    expect(store.team?.autostart_tasks).toBe(false)
    expect(store.error).toBe('Cannot save')
    expect(store.busy).toBe(false)
    api.setAutostartTasks.mockResolvedValueOnce({ ...store.team, autostart_tasks: true })
    expect(await store.setAutostartTasks(true)).toBe(true)
    expect(api.setAutostartTasks).toHaveBeenLastCalledWith('team_1', true)
    expect(store.team?.autostart_tasks).toBe(true)
    handlers.get('team.updated')!({ team_id: 'other', autostart_tasks: false } as never)
    expect(store.team?.autostart_tasks).toBe(true)
    handlers.get('team.updated')!({ team_id: 'team_1', autostart_tasks: false } as never)
    expect(store.team?.autostart_tasks).toBe(false)
  })

  it('submits one bulk request and prevents another while it is in flight', async () => {
    const store = await storeWith([task({ status: 'todo', is_running: false })])
    let finish!: () => void
    api.startAllTasks.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve }))
    const request = store.startAllTasks()
    expect(store.busy).toBe(true)
    expect(await store.startAllTasks()).toBe(false)
    finish()
    expect(await request).toBe(true)
    expect(api.startAllTasks).toHaveBeenCalledExactlyOnceWith('team_1')
    expect(api.team).toHaveBeenCalledTimes(2)
    expect(store.busy).toBe(false)
    expect(store.team?.autostart_tasks).toBe(false)
  })
})

describe('doneCost', () => {
  it('counts what the results cost and nothing that is still in flight', async () => {
    const store = await storeWith([
      task({ id: 'a', status: 'done', total_cost_usd: 1.25 }),
      task({ id: 'b', status: 'done', total_cost_usd: 0.004 }),
      // Neither of these bought the project a result, so neither is in the figure: one is
      // still running and would move the total under the reader, the other was abandoned.
      task({ id: 'c', status: 'in_progress', total_cost_usd: 9 }),
      task({ id: 'd', status: 'cancelled', total_cost_usd: 3 }),
    ])
    expect(store.doneCost).toBeCloseTo(1.254, 6)
  })

  it('is zero on a board that has finished nothing', async () => {
    const store = await storeWith([task({ status: 'todo' })])
    expect(store.doneCost).toBe(0)
  })

  it('keeps counting a task that was archived off the board', async () => {
    // Archiving takes the card off the board and nothing off the books (§10.4): the
    // columns draw `boardTasks`, the figure sums `tasks`, and the archive is its own list.
    const store = await storeWith([
      task({ id: 'a', status: 'done', total_cost_usd: 1.0 }),
      task({ id: 'b', status: 'done', total_cost_usd: 0.5, archived: true }),
      task({ id: 'c', status: 'todo' }),
    ])
    expect(store.doneCost).toBeCloseTo(1.5, 6)
    expect(store.boardTasks.map((entry) => entry.id)).toEqual(['a', 'c'])
    expect(store.archivedTasks.map((entry) => entry.id)).toEqual(['b'])
  })
})

describe('the activity line', () => {
  it('updates lifetime token totals for a closed task without adding repeated snapshots', async () => {
    const store = await storeWith([task()])
    const message = messageEvent({ role: 'system', kind: 'system', text: 'usage_update', data: {
      task_total_tokens: 100, task_total_uncached_tokens: 30, task_total_cached_tokens: 70,
      task_total_input_tokens: 20, task_total_output_tokens: 10,
    } })
    handlers.get('team.member.message')?.(message as never)
    handlers.get('team.member.message')?.(message as never)
    expect(store.openTask).toBeNull()
    expect(store.tasks[0]).toMatchObject({ total_tokens: 100, total_uncached_tokens: 30,
      total_cached_tokens: 70, total_input_tokens: 20, total_output_tokens: 10 })
  })

  it('counts Studio-managed subagents without an open transcript and closes only the finished child', async () => {
    const store = await storeWith([task()])
    const emit = (text: string, data: Record<string, unknown>, kind = 'system') =>
      handlers.get('team.member.message')?.(messageEvent({ role: 'system', kind, text, data }) as never)
    emit('task_started', { task_id: 'codex_1', task_type: 'studio_agent', backend: 'codex' })
    emit('task_started', { task_id: 'qwen_1', task_type: 'studio_agent', backend: 'qwen_code' })
    expect(store.openTask).toBeNull()
    expect(store.runningSubagents['task_1']).toBe(2)
    emit('', { continued: true }, 'result')
    expect(store.runningSubagents['task_1']).toBe(2)
    emit('task_notification', { task_id: 'codex_1', task_type: 'studio_agent', status: 'completed' })
    expect(store.runningSubagents['task_1']).toBe(1)
    emit('task_notification', { task_id: 'qwen_1', task_type: 'studio_agent', status: 'stopped' })
    expect(store.runningSubagents['task_1']).toBeUndefined()
  })

  it('follows a task whose transcript is not open', async () => {
    const store = await storeWith([task()])
    expect(store.openTask).toBeNull()

    handlers.get('team.member.message')?.(messageEvent() as never)

    expect(store.activity['task_1']).toEqual({ text: 'Bash · npm test', seq: 1 })
  })

  it('counts every update, so the same tool twice still pulses twice', async () => {
    const store = await storeWith([task()])
    const message = handlers.get('team.member.message')

    message?.(messageEvent() as never)
    message?.(messageEvent({ id: 'msg_2' }) as never)

    // Same text, different seq — the card keys its animation on the number, not the words.
    expect(store.activity['task_1']).toEqual({ text: 'Bash · npm test', seq: 2 })
  })

  it('says what the task dialog’s last line says, whatever arrived', async () => {
    // A card used to hold "Bash · npm test" through the call's result, a note and every frame
    // a sub-agent sent, while the open task's line moved on. Both are the tail of the whole
    // feed now, so they are compared after every message, the counter behind the flash too.
    const store = await storeWith([task()])
    const message = handlers.get('team.member.message')
    const feed = [
      messageEvent(),
      messageEvent({
        id: 'msg_2',
        role: 'system',
        kind: 'tool_result',
        text: '12 passed\n0 failed',
        data: {},
      }),
      messageEvent({ id: 'msg_3', role: 'user', kind: 'note', text: 'try again', data: {} }),
      messageEvent({
        id: 'msg_4',
        role: 'system',
        kind: 'system',
        text: 'task_progress',
        data: {
          task_id: 'agent_1',
          task_type: 'local_agent',
          description: 'Reading group 1',
          last_tool_name: 'Grep',
        },
      }),
      // Nothing to say: the line stays where it was and does not flash again.
      messageEvent({ id: 'msg_5', kind: 'thinking', text: '', data: {} }),
    ] as AgentMessage[]

    feed.forEach((event, index) => {
      message?.(event as never)
      const dialog = liveLine(feed.slice(0, index + 1), 'compact')
      expect(store.activity['task_1']).toEqual({ text: dialog.activity, seq: dialog.seq })
    })
    expect(store.activity['task_1']).toEqual({
      text: 'sub-agent · Reading group 1 · Grep',
      seq: 4,
    })
  })

  it('is dropped when the session ends, so a finished card claims nothing', async () => {
    const store = await storeWith([task()])
    handlers.get('team.member.message')?.(messageEvent() as never)

    handlers.get('team.task.status_changed')?.({
      task_id: 'task_1',
      team_id: 'team_1',
      assignee_id: 'member_1',
      status: 'needs_review',
      error: null,
    } as never)

    expect(store.activity['task_1']).toBeUndefined()
  })

  it('survives a status change that leaves the task running', async () => {
    const store = await storeWith([task({ status: 'pending' })])
    handlers.get('team.member.message')?.(messageEvent() as never)

    handlers.get('team.task.status_changed')?.({
      task_id: 'task_1',
      team_id: 'team_1',
      assignee_id: 'member_1',
      status: 'in_progress',
      error: null,
    } as never)

    expect(store.activity['task_1']?.text).toBe('Bash · npm test')
  })
})
