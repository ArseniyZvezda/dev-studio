import { describe, expect, it } from 'vitest'
import type { AgentMessage } from '@/api/types'
import { taskUsage } from './task-usage'
import { liveLine, tailLine, transcriptRows, upsertTranscript } from './transcript'

const message = (overrides: Partial<AgentMessage> = {}): AgentMessage => ({
  id: 'm', task_id: 'task', member_id: 'agent', role: 'assistant', kind: 'text',
  text: 'Working', data: {}, created_at: '2026-11-04T10:00:00Z', ...overrides,
})

const telemetry = (data: Record<string, unknown>, created_at = '2026-11-04T10:01:00Z') =>
  message({ id: 'state_usage', role: 'system', kind: 'system', text: 'usage_update', data, created_at })

describe('task header usage (§10.4)', () => {
  it('updates from the socket before the task snapshot and does not charge the final total twice', () => {
    let messages = [telemetry({ total_tokens: 200, task_total_tokens: 1200,
      context_tokens: 180, context_window: 272000 })]
    expect(taskUsage({ total_tokens: 1000, messages })).toEqual({
      contextTokens: 180, contextWindow: 272000, totalTokens: 1200,
    })
    messages = upsertTranscript(messages, telemetry({ total_tokens: 450, task_total_tokens: 1450,
      context_tokens: 300, context_window: 272000 }, '2026-11-04T10:02:00Z'))
    expect(messages).toHaveLength(1)
    expect(taskUsage({ total_tokens: 1000, messages }).totalTokens).toBe(1450)
    expect(taskUsage({ total_tokens: 1450, messages }).totalTokens).toBe(1450)
    expect(taskUsage({ total_tokens: 1600, messages }).totalTokens).toBe(1600)
  })

  it('uses the latest reported context without treating cumulative turn or result usage as context', () => {
    const messages = [telemetry({ task_total_tokens: 1000, context_tokens: 100, context_window: 2000 }),
      message({ role: 'system', kind: 'system', text: 'turn.completed', data: { usage: { input_tokens: 800 } } }),
      message({ kind: 'result', data: { usage: { input_tokens: 900 } } })]
    expect(taskUsage({ total_tokens: 1000, messages }).contextTokens).toBe(100)
    expect(taskUsage({ total_tokens: 1000, messages: messages.slice(1) }).contextTokens).toBeNull()
  })

  it('preserves old assistant context accounting and keeps unavailable capacity unknown', () => {
    const messages = [message({ data: { usage: {
      input_tokens: 100, cache_read_input_tokens: 200, cache_creation_input_tokens: 50,
      output_tokens: 500,
    } } })]
    expect(taskUsage({ total_tokens: 850, messages })).toEqual({
      contextTokens: 350, contextWindow: null, totalTokens: 850,
    })
  })

  it('distinguishes an empty reported context from unavailable telemetry', () => {
    expect(taskUsage({ total_tokens: 0, messages: [] }).contextTokens).toBeNull()
    expect(taskUsage({ total_tokens: 0, messages: [telemetry({ context_tokens: 0 })] }).contextTokens).toBe(0)
    expect(taskUsage({ total_tokens: 10, messages: [telemetry({
      context_tokens: NaN, context_window: -2, task_total_tokens: Infinity,
    })] })).toEqual({ contextTokens: null, contextWindow: null, totalTokens: 10 })
  })

  it('keeps a new run context unknown instead of restoring older assistant or member telemetry', () => {
    const messages = [
      telemetry({ task_total_tokens: 500, context_tokens: 300, context_window: 2000 }),
      message({ data: { usage: { input_tokens: 400 } } }),
      { ...telemetry({ task_total_tokens: 800 }), member_id: 'new_agent' },
      message({ kind: 'result', data: { usage: { input_tokens: 700 } } }),
    ]
    expect(taskUsage({ total_tokens: 800, messages })).toEqual({
      contextTokens: null, contextWindow: null, totalTokens: 800,
    })
  })

  it('ignores subagent context when reading legacy assistant usage', () => {
    const root = message({ data: { usage: { input_tokens: 200 } } })
    const child = message({ data: { parent_tool_use_id: 'delegate_call', usage: { input_tokens: 50 } } })
    expect(taskUsage({ total_tokens: 250, messages: [root, child] }).contextTokens).toBe(200)
    expect(taskUsage({ total_tokens: 50, messages: [child] }).contextTokens).toBeNull()
  })

  it('does not reuse context from before compression while retaining the lifetime token total', () => {
    const messages = [telemetry({ task_total_tokens: 1000, context_tokens: 900 }),
      message({ kind: 'compressed summary', text: 'Current work' })]
    expect(taskUsage({ total_tokens: 1000, messages })).toEqual({
      contextTokens: null, contextWindow: null, totalTokens: 1000,
    })
  })

  it('keeps telemetry out of the activity and transcript and does not sum its cumulative output again', () => {
    const report = telemetry({ task_total_tokens: 1000, usage: { output_tokens: 500 } })
    const messages = [message({ data: { usage: { output_tokens: 50 } } }), report]
    expect(tailLine(report)).toBe('')
    expect(liveLine(messages, 'compact')).toEqual({ activity: 'Working', seq: 1, tokens: 50 })
    for (const level of ['compact', 'advanced', 'verbose'] as const) {
      expect(transcriptRows(messages, level)).toHaveLength(1)
    }
  })
})
