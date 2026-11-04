import { describe, expect, it } from 'vitest'
import type { AgentMessage } from '@/api/types'
import { taskCost } from './task-telemetry'

const result = (data: Record<string, unknown>): AgentMessage => ({
  id: 'r', task_id: 't', member_id: 'm', role: 'assistant', kind: 'result',
  text: '', data, created_at: '2026-10-26T12:00:00Z',
})

describe('taskCost', () => {
  it('shows exact runtime costs and zero as before', () => {
    expect(taskCost(1.234, [result({})])).toBe('$1.234')
    expect(taskCost(0, [])).toBe('$0.000')
  })

  it('marks estimates and keeps cheap Codex runs visible', () => {
    expect(taskCost(0.000107, [result({ cost_estimated: true })])).toBe('≈$0.000107')
    expect(taskCost(1.234, [result({ cost_estimated: true })])).toBe('≈$1.234')
  })

  it('distinguishes unavailable cost from zero and a partially priced task', () => {
    const messages = [result({ cost_unavailable: true })]
    expect(taskCost(0, messages)).toBe('—')
    expect(taskCost(0.03, [...messages, result({ cost_estimated: true })])).toBe('≥$0.030')
  })
})
