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
  it('reads saved and newer live sent/received/cache counters as one complete snapshot', () => {
    const task = { total_tokens: 1000, total_uncached_tokens: 300, total_cached_tokens: 700,
      total_input_tokens: 200, total_output_tokens: 100, messages: [] as AgentMessage[] }
    expect(taskUsage(task)).toMatchObject({ inputTokens: 200, outputTokens: 100, cacheReadTokens: 700 })
    task.messages.push(telemetry({ task_total_tokens: 1500, task_total_uncached_tokens: 500,
      task_total_cached_tokens: 1000, task_total_input_tokens: 350, task_total_output_tokens: 150 }))
    expect(taskUsage(task)).toMatchObject({ inputTokens: 350, outputTokens: 150, cacheReadTokens: 1000 })
  })

  it('does not replace known saved directions with a same-total legacy snapshot', () => {
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 300, total_cached_tokens: 700,
      total_input_tokens: 200, total_output_tokens: 100, messages: [telemetry({ task_total_tokens: 1000,
        task_total_uncached_tokens: 300, task_total_cached_tokens: 700 })] }))
      .toMatchObject({ inputTokens: 200, outputTokens: 100, cacheReadTokens: 700 })
  })

  it('keeps missing and inconsistent historical direction splits unknown', () => {
    for (const task of [
      { total_tokens: 1000 },
      { total_tokens: 1000, total_uncached_tokens: 0, total_cached_tokens: 0,
        total_input_tokens: 0, total_output_tokens: 0 },
      { total_tokens: 1000, total_uncached_tokens: 800, total_cached_tokens: 700 },
    ]) {
      expect(taskUsage({ ...task, messages: [] }))
        .toMatchObject({ inputTokens: null, outputTokens: null, cacheReadTokens: null })
    }
    expect(taskUsage({ total_tokens: 0, messages: [] }))
      .toMatchObject({ inputTokens: 0, outputTokens: 0, cacheReadTokens: 0 })
  })

  it('derives complete old usage without repeating streamed responses or final result counters', () => {
    const usage = { input_tokens: 100, cache_creation_input_tokens: 50, cache_read_input_tokens: 200, output_tokens: 10 }
    const messages = [
      message({ data: { provider_response_id: 'response1', usage } }),
      message({ data: { provider_response_id: 'response1', usage: { ...usage, output_tokens: 30 } } }),
      message({ kind: 'result', data: { total_tokens: 380, usage: { ...usage, output_tokens: 30 } } }),
      telemetry({ total_tokens: 380, task_total_tokens: 380 }),
    ]
    expect(taskUsage({ total_tokens: 380, messages }))
      .toMatchObject({ inputTokens: 150, outputTokens: 30, cacheReadTokens: 200 })
    // Additional unexplained lifetime spending cannot be assigned to a direction.
    expect(taskUsage({ total_tokens: 500, messages })).toMatchObject({ inputTokens: null, outputTokens: null })
  })

  it('uses retained repaired evidence and keeps subsequent runs after compaction', () => {
    const result = (input: number, output: number, cached: number, created_at: string) =>
      message({ kind: 'result', created_at, data: { total_tokens: input + output + cached,
        usage: { input_tokens: input, output_tokens: output, cache_read_input_tokens: cached } } })
    const messages = [result(900, 90, 9000, '2026-11-04T09:00:00Z'),
      message({ kind: 'accounting', created_at: '2026-11-04T11:00:00Z', data: {
        repair: { cutoff_at: '2026-11-04T10:00:00Z' },
        evidence: [result(100, 10, 1000, '2026-11-04T09:00:00Z')],
      } }),
      message({ kind: 'compressed summary', created_at: '2026-11-04T12:00:00Z', data: {
        evidence: [result(200, 20, 2000, '2026-11-04T11:30:00Z')],
      } }),
      result(300, 30, 3000, '2026-11-04T13:00:00Z')]
    expect(taskUsage({ total_tokens: 6660, messages }))
      .toMatchObject({ inputTokens: 600, outputTokens: 60, cacheReadTokens: 6000 })
  })

  it('rejects old evidence that matches the total but contradicts the confirmed cache partition', () => {
    const messages = [message({ kind: 'result', data: { total_tokens: 1000,
      usage: { input_tokens: 100, output_tokens: 100, cache_read_input_tokens: 800 } } })]
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 300, total_cached_tokens: 700, messages }))
      .toMatchObject({ inputTokens: null, outputTokens: null, cacheReadTokens: 700 })
  })

  it('keeps historical tokens unclassified rather than pretending they were uncached', () => {
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 200, total_cached_tokens: 500, messages: [] }))
      .toEqual({ totalTokens: 1000, uncachedTokens: 200, cachedTokens: 500, unclassifiedTokens: 300,
        contextTokens: null, contextWindow: null, inputTokens: null, outputTokens: null, cacheReadTokens: 500 })
    expect(taskUsage({ total_tokens: 1000, messages: [] }).unclassifiedTokens).toBe(1000)
  })

  it('does not resurrect the inflated total from legacy telemetry after history repair', () => {
    const messages = [telemetry({ task_total_tokens: 9000, context_tokens: 400 })]
    expect(taskUsage({ total_tokens: 1200, total_uncached_tokens: 200, total_cached_tokens: 1000, messages }))
      .toEqual({ totalTokens: 1200, uncachedTokens: 200, cachedTokens: 1000, unclassifiedTokens: 0,
        contextTokens: 400, contextWindow: null, inputTokens: null, outputTokens: null, cacheReadTokens: 1000 })
  })

  it('accepts newly classified tokens without requiring total spending to increase', () => {
    const messages = [telemetry({ task_total_tokens: 1000, task_total_uncached_tokens: 200,
      task_total_cached_tokens: 700 })]
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 0, total_cached_tokens: 0, messages }))
      .toMatchObject({ totalTokens: 1000, uncachedTokens: 200, cachedTokens: 700, unclassifiedTokens: 100 })
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 200, total_cached_tokens: 800, messages }))
      .toMatchObject({ totalTokens: 1000, uncachedTokens: 200, cachedTokens: 800, unclassifiedTokens: 0 })
  })

  it('treats an inconsistent stored breakdown as unknown, matching project statistics', () => {
    expect(taskUsage({ total_tokens: 1000, total_uncached_tokens: 800, total_cached_tokens: 700, messages: [] }))
      .toMatchObject({ totalTokens: 1000, uncachedTokens: 0, cachedTokens: 0, unclassifiedTokens: 1000 })
  })

  it('reads the newest live breakdown as one snapshot without combining repeated totals', () => {
    const messages = [
      telemetry({ task_total_tokens: 5000, task_total_uncached_tokens: 2000, task_total_cached_tokens: 3000 }),
      telemetry({ task_total_tokens: 1500, task_total_uncached_tokens: 400, task_total_cached_tokens: 1000 }),
    ]
    expect(taskUsage({ total_tokens: 1200, total_uncached_tokens: 200, total_cached_tokens: 1000, messages }))
      .toMatchObject({ totalTokens: 1500, uncachedTokens: 400, cachedTokens: 1000, unclassifiedTokens: 100 })
  })

  it('ignores telemetry covered by the newest repair and accepts subsequent live snapshots', () => {
    const messages = [
      telemetry({ task_total_tokens: 9000, task_total_uncached_tokens: 2000, task_total_cached_tokens: 7000 }),
      message({ kind: 'accounting', data: { accounting_version: 2,
        repair: { cutoff_at: '2026-11-04T11:00:00' } } }),
    ]
    const task = { total_tokens: 1200, total_uncached_tokens: 200, total_cached_tokens: 1000, messages }
    expect(taskUsage(task).totalTokens).toBe(1200)
    messages.push(telemetry({ task_total_tokens: 1600, task_total_uncached_tokens: 300,
      task_total_cached_tokens: 1300 }, '2026-11-04T11:01:00Z'))
    expect(taskUsage(task)).toMatchObject({ totalTokens: 1600, uncachedTokens: 300, cachedTokens: 1300 })
  })

  it('updates from the socket before the task snapshot and does not charge the final total twice', () => {
    let messages = [telemetry({ total_tokens: 200, task_total_tokens: 1200,
      context_tokens: 180, context_window: 272000 })]
    expect(taskUsage({ total_tokens: 1000, messages })).toMatchObject({
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
    expect(taskUsage({ total_tokens: 850, messages })).toMatchObject({
      contextTokens: 350, contextWindow: null, totalTokens: 850,
    })
  })

  it('distinguishes an empty reported context from unavailable telemetry', () => {
    expect(taskUsage({ total_tokens: 0, messages: [] }).contextTokens).toBeNull()
    expect(taskUsage({ total_tokens: 0, messages: [telemetry({ context_tokens: 0 })] }).contextTokens).toBe(0)
    expect(taskUsage({ total_tokens: 10, messages: [telemetry({
      context_tokens: NaN, context_window: -2, task_total_tokens: Infinity,
    })] })).toMatchObject({ contextTokens: null, contextWindow: null, totalTokens: 10 })
  })

  it('keeps a new run context unknown instead of restoring older assistant or member telemetry', () => {
    const messages = [
      telemetry({ task_total_tokens: 500, context_tokens: 300, context_window: 2000 }),
      message({ data: { usage: { input_tokens: 400 } } }),
      { ...telemetry({ task_total_tokens: 800 }), member_id: 'new_agent' },
      message({ kind: 'result', data: { usage: { input_tokens: 700 } } }),
    ]
    expect(taskUsage({ total_tokens: 800, messages })).toMatchObject({
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
    expect(taskUsage({ total_tokens: 1000, messages })).toMatchObject({
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
