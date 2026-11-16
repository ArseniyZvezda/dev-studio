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

  it.each(['compressed summary', 'context_summary'])('uses %s as an inclusive boundary across UTC timestamp spellings', kind => {
    const summary = message('summary', 2, { kind, created_at: '2026-11-04T10:00:02+00:00' })
    const boundary = message('boundary', 2, { created_at: '2026-11-04T10:00:02Z' })
    const live = message('live', 3, { created_at: '2026-11-04T10:00:03Z' })
    const snapshot = [message('old', 1), boundary]
    const incoming = [live, summary, boundary]

    expect(mergeTaskConversation(snapshot, incoming)).toEqual([summary, live])
    expect(mergeTaskConversation(incoming, snapshot)).toEqual([summary, live])
    expect(snapshot.map(entry => entry.id)).toEqual(['old', 'boundary'])
    expect(incoming.map(entry => entry.id)).toEqual(['live', 'summary', 'boundary'])
  })

  it.each([
    ['context_summary', 'compressed summary'],
    ['compressed summary', 'context_summary'],
  ])('does not restore the previous %s after a newer %s', (oldKind, newKind) => {
    const old = message('old', 1)
    const first = message('summary-1', 2, { kind: oldKind })
    const between = message('between', 3)
    const second = message('summary-2', 4, { kind: newKind })
    const live = message('live', 5)
    const stale = [old, first, between]
    const current = [second, live]

    expect(mergeTaskConversation(stale, current)).toEqual(current)
    expect(mergeTaskConversation(current, stale)).toEqual(current)
    const recovered = mergeTaskConversation(stale, [live, second, first, old])
    expect(mergeTaskConversation(recovered, stale)).toEqual(current)
    expect(mergeTaskConversation(recovered, current)).toEqual(current)
  })

  it('keeps the later summary when repeated compaction has the same boundary', () => {
    const previous = message('summary-1', 2, { kind: 'context_summary', created_at: '2026-11-04T10:00:02Z' })
    const latest = message('summary-2', 2, { kind: 'compressed summary', created_at: '2026-11-04T10:00:02+00:00' })
    const live = message('live', 3)
    expect(mergeTaskConversation([previous], [latest, live])).toEqual([latest, live])
  })

  it('orders snapshot and replay history chronologically and preserves equal-time order', () => {
    const first = message('first', 1)
    const second = message('second', 2, { created_at: '2026-11-04T10:00:02Z' })
    const peer = message('peer', 2, { created_at: '2026-11-04T10:00:02+00:00' })
    const last = message('last', 4)
    expect(mergeTaskConversation([last, second], [peer, first, second])).toEqual([first, second, peer, last])
  })

  it.each([false, true])('retains newest telemetry after compaction with reversed sources: %s', reversed => {
    const summary = message('summary', 2, { kind: 'compressed summary' })
    const stale = message('state_usage', 3, { kind: 'usage_update', text: 'old', created_at: '2026-11-04T10:00:03Z' })
    const fresh = message('state_usage', 5, { kind: 'usage_update', text: 'new', created_at: '2026-11-04T10:00:05+00:00' })
    const live = message('live', 4)
    const snapshot = [fresh, summary, live]
    const replay = [stale, message('old', 1)]
    expect(reversed ? mergeTaskConversation(replay, snapshot) : mergeTaskConversation(snapshot, replay))
      .toEqual([summary, live, fresh])
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
    ], 'default')
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
    const rows = transcriptRows([report, cancelled, continued, next, error], 'default')
    expect(rows.map(row => row.key)).toEqual(['report', 'cancelled', 'continued', 'next', 'error'])
    expect(resultTypeOf(rows[0]!.message)).toBe('done')
    expect(resultTypeOf(rows[3]!.message)).toBeNull()
  })

  it('deduplicates identical reports while retaining the closing summary type', () => {
    const rows = transcriptRows([
      message('report', 1, { kind: 'summary', text: 'Done', data: { result_type: 'done' } }),
      message('closing', 2, { text: 'Done' }),
    ], 'default')
    expect(rows.map(row => row.key)).toEqual(['closing'])
    expect(resultTypeOf(rows[0]!.message)).toBe('done')
  })
})
