import { describe, expect, it } from 'vitest'
import type { AgentMessage } from '@/api/types'
import { mergeTaskConversation } from '@/lib/task-conversation'
import { transcriptRows } from '@/lib/transcript'
import { resultTypeOf } from '@/lib/task-result'

const message = (id: string, at: number, extra: Partial<AgentMessage> = {}): AgentMessage => ({
  id, task_id: 'task', member_id: 'agent', role: 'assistant', kind: 'text', text: id,
  data: {}, created_at: `2026-11-04T10:00:0${at}`, ...extra,
})

describe('Producer task conversation reconciliation', () => {
  it('preserves a live message arriving during the REST request and deduplicates replay', () => {
    const first = message('first', 1)
    const live = message('live', 2)
    expect(mergeTaskConversation([first], [first, live]).map(entry => entry.id)).toEqual(['first', 'live'])
  })

  it('does not resurrect compacted history from a stale snapshot or socket replay', () => {
    const old = message('old', 1)
    const summary = message('summary', 2, { kind: 'compressed summary' })
    const next = message('next', 3)
    expect(mergeTaskConversation([old], [summary, next, old]).map(entry => entry.id)).toEqual(['summary', 'next'])
    expect(mergeTaskConversation([summary, next], [old]).map(entry => entry.id)).toEqual(['summary', 'next'])
  })

  it('keeps the newest telemetry when REST and socket overlap', () => {
    const stale = message('state_usage', 1, { text: 'old' })
    const fresh = message('state_usage', 2, { text: 'new' })
    expect(mergeTaskConversation([fresh], [stale])).toEqual([fresh])
    expect(mergeTaskConversation([stale], [fresh])).toEqual([fresh])
  })
})

describe('Shared task conversation summaries', () => {
  it.each(['done', 'question', 'error'] as const)('preserves the closing reply projection for %s', resultType => {
    const report = message('report', 1, { kind: 'summary', text: 'Detailed report', data: { result_type: resultType } })
    const closing = message('closing', 3, { text: 'Closing reply' })
    const rows = transcriptRows([
      report,
      message('tool-result', 2, { kind: 'tool_result', role: 'system' }),
      closing,
      message('result', 4, { kind: 'result', role: 'system' }),
    ], 'advanced')
    expect(rows.map(row => row.key)).toEqual(['report', 'closing'])
    expect(rows.map(row => resultTypeOf(row.message))).toEqual([null, resultType])
    expect(report.kind).toBe('summary')
    expect(closing.kind).toBe('text')
  })

  it('keeps cancellation and errors in order without lending their result to a resumed turn', () => {
    const report = message('report', 1, { kind: 'summary', data: { result_type: 'done' } })
    const cancelled = message('cancelled', 2, { kind: 'cancelled', role: 'system' })
    const continued = message('continued', 3, { kind: 'continued', role: 'system' })
    const next = message('next', 4)
    const error = message('error', 5, { kind: 'error', role: 'system' })
    const rows = transcriptRows([report, cancelled, continued, next, error], 'compact')
    expect(rows.map(row => row.key)).toEqual(['report', 'cancelled', 'continued', 'next', 'error'])
    expect(resultTypeOf(rows[0]!.message)).toBe('done')
    expect(resultTypeOf(rows[3]!.message)).toBeNull()
  })

  it('deduplicates identical reports while retaining the closing summary type', () => {
    const rows = transcriptRows([
      message('report', 1, { kind: 'summary', text: 'Done', data: { result_type: 'done' } }),
      message('closing', 2, { text: 'Done' }),
    ], 'compact')
    expect(rows.map(row => row.key)).toEqual(['closing'])
    expect(resultTypeOf(rows[0]!.message)).toBe('done')
  })
})
