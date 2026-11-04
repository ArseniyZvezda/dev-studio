import { describe, expect, it } from 'vitest'
import type { AgentChat, TeamTask } from '@/api/types'
import { producerActiveTasks, producerChatTasks, producerNeedsReply } from './producer'

function task(id: string, number: number, patch: Partial<TeamTask> = {}): TeamTask {
  return {
    id, number, display_id: `DP-${number}`, team_id: 'team', assignee_id: 'agent', created_by: null,
    title: id, brief: '', write_paths: [], status: 'todo', depends_on: [], auto_start: false, resume_on_limit: true,
    limit_reset_at: null, limit_resume_at: null, model: null, effort: null, agent_session_id: null,
    remote_control_url: null, worktree_path: null, review_comment: null, result_summary: null,
    result_type: null, error: null, total_cost_usd: 0, num_turns: 0, total_tokens: 0, archived: false,
    created_at: '', started_at: null, finished_at: null, ...patch,
  }
}

describe('Producer chat footer and desktop attention', () => {
  it('shows active conversations across the selected team and excludes archives and unopened drafts', () => {
    const board = [
      task('running-manual', 1, { status: 'in_progress' }),
      task('review-other-chat', 2, { status: 'needs_review', producer_chat_id: 'another-chat' }),
      task('blocked', 3, { status: 'blocked' }),
      task('queued', 13, { status: 'pending' }),
      task('approved-queue', 4, { status: 'todo', producer_chat_id: 'this-chat', producer_approved: true }),
      task('draft', 5, { status: 'todo', producer_chat_id: 'this-chat', producer_approved: false }),
      task('manual-todo', 6),
      task('done', 7, { status: 'done' }),
      task('cancelled', 8, { status: 'cancelled' }),
      task('archived-running', 9, { status: 'in_progress', archived: true }),
      task('archived-approved', 10, { status: 'todo', producer_approved: true, archived: true }),
      task('foreign-running', 11, { status: 'in_progress', team_id: 'other' }),
      task('foreign-approved', 12, { status: 'todo', producer_approved: true, team_id: 'other' }),
    ]
    expect(producerActiveTasks(board, 'team').map(item => item.id)).toEqual([
      'running-manual', 'review-other-chat', 'blocked', 'queued', 'approved-queue',
    ])
    expect(producerActiveTasks(board, 'other').map(item => item.id)).toEqual([
      'foreign-running', 'foreign-approved',
    ])
    expect(producerActiveTasks(board, 'missing')).toEqual([])
    expect(producerActiveTasks([], 'team')).toEqual([])
  })

  it('shows the entire chat team in progress and the exact saved watch list', () => {
    const chat = { producer_watch: [{ id: 'queued' }, { id: 'running' }, { id: 'foreign' }] } as AgentChat
    const board = [
      task('running', 1, { team_id: 'team', status: 'in_progress', producer_chat_id: 'another-chat' }),
      task('queued', 2, { team_id: 'team', status: 'pending' }),
      task('done', 3, { team_id: 'team', status: 'done' }),
      task('foreign', 4, { team_id: 'other', status: 'in_progress' }),
    ]
    const result = producerChatTasks(chat, board, 'team')
    expect(result.running.map(item => item.id)).toEqual(['running'])
    expect(result.waiting.map(item => item.id)).toEqual(['running', 'queued'])
    expect(producerChatTasks({} as AgentChat, [], 'team')).toEqual({ running: [], waiting: [] })
    expect(producerChatTasks(chat, board.map(t => ({ ...t, status: 'done' })), 'team').running).toEqual([])
  })

  it('keeps running tasks leftmost while preserving board order within both groups', () => {
    const board = [
      task('review', 1, { status: 'needs_review' }),
      task('running-first', 2, { status: 'in_progress' }),
      task('queued', 3, { status: 'pending' }),
      task('running-second', 4, { status: 'in_progress' }),
      task('blocked', 5, { status: 'blocked' }),
    ]
    expect(producerActiveTasks(board, 'team').map(item => item.id)).toEqual([
      'running-first', 'running-second', 'review', 'queued', 'blocked',
    ])
    expect(board.map(item => item.id)).toEqual([
      'review', 'running-first', 'queued', 'running-second', 'blocked',
    ])
    board[2]!.status = 'in_progress'
    expect(producerActiveTasks(board, 'team').map(item => item.id)).toEqual([
      'running-first', 'queued', 'running-second', 'review', 'blocked',
    ])
  })

  it('combines all active conversations and clears on reply, approval or stop', () => {
    const waiting = { producer_mode: true, producer_state: 'awaiting_user', is_running: false } as AgentChat
    const approval = { ...waiting, producer_state: 'awaiting_approval' } as AgentChat
    expect(producerNeedsReply([waiting, approval])).toBe(true)
    expect(producerNeedsReply([{ ...waiting, is_running: true }, approval])).toBe(true)
    expect(producerNeedsReply([{ ...waiting, is_running: true }])).toBe(false)
    expect(producerNeedsReply([{ ...waiting, producer_mode: false }])).toBe(false)
    expect(producerNeedsReply([{ ...waiting, producer_paused: true }])).toBe(false)
    expect(producerNeedsReply([{ ...waiting, producer_state: 'discussing' }])).toBe(true)
    for (const state of ['waiting', 'running', 'off', 'error'] as const) {
      expect(producerNeedsReply([{ ...waiting, producer_state: state }])).toBe(false)
    }
    expect(producerNeedsReply([])).toBe(false)
  })
})
