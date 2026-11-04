import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, reactive } from 'vue'
import { useTaskClock } from './task-clock'

afterEach(() => vi.useRealTimers())

describe('task clock', () => {
  it('ticks through silent work, stops at completion, resumes and cleans up on close', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-26T12:00:00Z'))
    const current = reactive({
      id: 't', status: 'in_progress' as 'in_progress' | 'needs_review',
      limit_reset_at: null, limit_resume_at: null,
    })
    const scope = effectScope()
    const now = scope.run(() => useTaskClock(() => current))!
    const initial = now.value
    vi.advanceTimersByTime(3000)
    expect(now.value).toBe(initial + 3000)

    current.status = 'needs_review'
    await nextTick()
    vi.advanceTimersByTime(5000)
    expect(now.value).toBe(initial + 3000)
    expect(vi.getTimerCount()).toBe(0)

    current.status = 'in_progress'
    await nextTick()
    vi.advanceTimersByTime(1000)
    expect(now.value).toBe(initial + 9000)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps the existing limit countdown ticking while the task is blocked', async () => {
    vi.useFakeTimers()
    const task = reactive({ id: 't', status: 'blocked' as const,
      limit_reset_at: '2026-10-26T13:00:00Z' as string | null, limit_resume_at: null })
    const scope = effectScope()
    const now = scope.run(() => useTaskClock(() => task))!
    const initial = now.value
    vi.advanceTimersByTime(2000)
    expect(now.value).toBe(initial + 2000)
    task.limit_reset_at = null
    await nextTick()
    expect(vi.getTimerCount()).toBe(0)
    scope.stop()
  })
})
