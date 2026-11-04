/**
 * What a task says it has running, folded out of the runtime's own lifecycle frames.
 *
 * The fold is where the truth is decided — which work holds the run open, which frame ends
 * a piece of it, and what happens to work that was still running when the run stopped — so
 * it is checked as data rather than by looking at the panel that draws it.
 */
import { describe, expect, it } from 'vitest'

import type { AgentMessage } from '@/api/types'
import { running, subagentActivity, subagents } from '@/lib/subagents'

let sequence = 0

const message = (overrides: Partial<AgentMessage> = {}): AgentMessage => ({
  id: `msg_${(sequence += 1)}`,
  task_id: 'task_1',
  member_id: 'team_default__dev',
  role: 'assistant',
  kind: 'text',
  text: '',
  data: {},
  created_at: '2026-09-04T10:00:00',
  ...overrides,
})

/** A `system` frame as the backend stores one: the subtype in `text`, the payload verbatim. */
const frame = (subtype: string, data: Record<string, unknown>) =>
  message({ role: 'system', kind: 'system', text: subtype, data })

const started = (id: string, overrides: Record<string, unknown> = {}) =>
  frame('task_started', {
    task_id: id,
    task_type: 'local_agent',
    description: 'Reading group 1',
    ...overrides,
  })

/** The result frame that ends a *turn* while delegated work keeps going (§10.3). */
const heldResult = () => message({ role: 'system', kind: 'result', data: { continued: true } })
const finalResult = () => message({ role: 'system', kind: 'result', data: {} })

describe('what a session has running', () => {
  it('opens on task_started and closes on the notification', () => {
    const list = subagents([
      started('task_a'),
      frame('task_progress', {
        task_id: 'task_a',
        last_tool_name: 'Grep',
        usage: { total_tokens: 4200, tool_uses: 7 },
      }),
      heldResult(),
      frame('task_notification', {
        task_id: 'task_a',
        status: 'completed',
        summary: 'Read 20 files.',
      }),
    ])

    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({
      id: 'task_a',
      description: 'Reading group 1',
      typeLabel: 'sub-agent',
      status: 'completed',
      holdsRun: true,
      tokens: 4200,
      toolUses: 7,
      lastTool: 'Grep',
      summary: 'Read 20 files.',
    })
    expect(running(list)).toEqual([])
  })

  it('takes a terminal state from a task_updated patch as well', () => {
    // A workflow stopped with `TaskStop` reports `killed` there and sometimes nowhere else.
    const list = subagents([
      started('wf_1', { task_type: 'local_workflow', description: 'review-changes' }),
      frame('task_updated', { task_id: 'wf_1', patch: { status: 'killed' } }),
    ])

    expect(list[0].typeLabel).toBe('workflow')
    // `killed` and `stopped` are the two vocabularies for one thing; the UI says one.
    expect(list[0].status).toBe('stopped')
  })

  it('ignores a non-terminal patch', () => {
    const list = subagents([
      started('task_a'),
      frame('task_updated', { task_id: 'task_a', patch: { status: 'running' } }),
    ])

    expect(running(list)).toHaveLength(1)
  })

  it('shows a background shell without claiming the run waits for it', () => {
    // It is worth seeing — a dev server is running — but it never settles, so nothing is
    // held open for it. That is the same set the backend defers on.
    const list = subagents([
      started('shell_1', { task_type: 'background_shell', description: 'npm run dev' }),
    ])

    expect(list[0]).toMatchObject({ typeLabel: 'shell', status: 'running', holdsRun: false })
  })

  it('does not invent work whose start is not in the transcript', () => {
    expect(subagents([frame('task_notification', { task_id: 'gone', status: 'completed' })])).toEqual(
      [],
    )
  })
})

describe('what the end of a run does to it', () => {
  it('leaves work running across a result that only ended a turn', () => {
    const list = subagents([started('task_a'), heldResult()])
    expect(running(list)).toHaveLength(1)
  })

  it('stops work that was still running when the run ended', () => {
    // The CLI subprocess dies with the run and takes its sub-agents with it, so "running"
    // would be a claim about a process that is gone — and on a resumed task it would stay
    // on the screen forever.
    const list = subagents([started('task_a'), finalResult()])
    expect(list[0].status).toBe('stopped')
    expect(running(list)).toEqual([])
  })

  it('stops it on an error or a cancellation too', () => {
    for (const kind of ['error', 'cancelled']) {
      const list = subagents([started('task_a'), message({ role: 'system', kind })])
      expect(running(list)).toEqual([])
    }
  })

  it('keeps a run that started work again after the last one ended', () => {
    const list = subagents([
      started('task_a'),
      finalResult(),
      started('task_b', { description: 'Reading group 2' }),
    ])

    expect(list.map((entry) => entry.status)).toEqual(['stopped', 'running'])
  })
})

describe('the line a board card shows', () => {
  it('names the work rather than the frame', () => {
    expect(subagentActivity(started('task_a'))).toBe('sub-agent started · Reading group 1')
    expect(
      subagentActivity(
        frame('task_progress', {
          task_id: 'task_a',
          task_type: 'local_agent',
          description: 'Reading group 1',
          last_tool_name: 'Grep',
        }),
      ),
    ).toBe('sub-agent · Reading group 1 · Grep')
    expect(
      subagentActivity(
        frame('task_notification', {
          task_id: 'task_a',
          status: 'completed',
          summary: 'Read 20 files.',
        }),
      ),
    ).toBe('task completed · Read 20 files.')
  })

  it('stays silent for a frame that is not delegated work', () => {
    // An `init` is the session describing its own configuration, not doing anything.
    expect(subagentActivity(frame('init', { tools: ['Read'] }))).toBe('')
    expect(subagentActivity(message({ kind: 'text', text: 'Starting.' }))).toBe('')
  })
})
