import { describe, expect, it } from 'vitest'
import type { AgentChat, TeamTask } from '@/api/types'
import { chatUsage } from './chat-usage'
import { producerSessionUsage } from './producer-usage'

const chat = { id: 'session', total_tokens: 100, total_uncached_tokens: 30,
  total_input_tokens: 20, total_output_tokens: 10, total_cached_tokens: 70,
  total_cost_usd: 0.5 } as AgentChat
function task(id: string, patch: Partial<TeamTask> = {}): TeamTask {
  return { ...chat, id, producer_chat_id: chat.id, status: 'done', ...patch } as TeamTask
}

describe('Producer session spending', () => {
  it('includes running, completed and archived work only from this conversation', () => {
    const tasks = [task('running', { status: 'in_progress' }), task('done'),
      task('archive', { archived: true }), task('cancelled', { status: 'cancelled' }),
      task('other-session', { producer_chat_id: 'other' }), task('manual', { producer_chat_id: null })]
    expect(producerSessionUsage(chat, [], tasks)).toMatchObject({ inputTokens: 100,
      outputTokens: 50, cacheReadTokens: 350, costUsd: 2.5 })
    expect(chatUsage(chat, [])).toMatchObject({ inputTokens: 20,
      outputTokens: 10, cacheReadTokens: 70, costUsd: 0.5 })
    expect(producerSessionUsage({ ...chat, id: 'other' }, [], tasks).inputTokens).toBe(40)
  })

  it('keeps historical unknown directions unknown instead of displaying a partial total', () => {
    const legacy = task('legacy', { total_tokens: 150, total_uncached_tokens: 80,
      total_input_tokens: undefined, total_output_tokens: undefined })
    expect(producerSessionUsage(chat, [], [legacy])).toMatchObject({
      inputTokens: null, outputTokens: null, cacheReadTokens: 140, costUsd: 1 })
  })

  it('uses durable chat totals after history compaction and shows zero for an empty session', () => {
    expect(chatUsage(chat, [])).toMatchObject(producerSessionUsage(chat, [], []))
    expect(chatUsage({ ...chat, total_tokens: 0, total_uncached_tokens: 0,
      total_input_tokens: 0, total_output_tokens: 0, total_cached_tokens: 0, total_cost_usd: 0 }, []))
      .toMatchObject({ inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, costUsd: 0 })
  })
})
