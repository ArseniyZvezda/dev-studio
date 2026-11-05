/**
 * What a node's progress line says.
 *
 * Data, not layout, which is why it is testable here: the split into two halves is the
 * whole point — the stage name is the half a 240px node is allowed to truncate, and the
 * percentage is the half that must survive.
 */
import { describe, expect, it } from 'vitest'

import type { NodeProgress } from '@/api/types'
import {
  formatCountdown,
  formatDuration,
  formatProgress,
  formatTokens,
  isActive,
  isRunning,
  isSuccessful,
} from '@/lib/status'

const frame = (overrides: Partial<NodeProgress> = {}): NodeProgress => ({
  percent: null,
  label: null,
  detail: null,
  eta_s: null,
  step: null,
  steps: null,
  ...overrides,
})

describe('formatDuration', () => {
  const start = '2026-10-26T12:00:00Z'
  const later = Date.parse('2026-10-26T12:01:05Z')

  it('advances with a reactive clock even when no task events arrive', () => {
    expect(formatDuration(start, null, later)).toBe('1m 5s')
    expect(formatDuration(start, null, later + 5000)).toBe('1m 10s')
  })

  it('keeps a completed run fixed and accepts naive UTC timestamps', () => {
    expect(formatDuration('2026-10-26T12:00:00', start, later)).toBe('0s')
    expect(formatDuration(start, '2026-10-26T12:00:30Z', later)).toBe('30s')
    expect(formatDuration(null, null, later)).toBe('—')
  })
})

describe('formatProgress', () => {
  it('reads as one sentence about what the node is doing', () => {
    expect(
      formatProgress(frame({ label: 'Multiview diffusion', detail: '9/15', percent: 48.3, eta_s: 55.7 })),
    ).toEqual({ stage: 'Multiview diffusion · 9/15', amount: '48% · ~56s' })
  })

  it('spells out step/steps only when the node did not word the position itself', () => {
    expect(formatProgress(frame({ label: 'Baking', step: 3, steps: 8 })).stage).toBe('Baking · 3/8')
    expect(
      formatProgress(frame({ label: 'Baking', step: 3, steps: 8, detail: 'view 2' })).stage,
    ).toBe('Baking · view 2')
  })

  it('says nothing it was not told', () => {
    expect(formatProgress(frame())).toEqual({ stage: '', amount: '' })
    expect(formatProgress(frame({ label: 'Remeshing' }))).toEqual({
      stage: 'Remeshing',
      amount: '',
    })
  })

  it('keeps a long estimate readable', () => {
    expect(formatProgress(frame({ eta_s: 4000 })).amount).toBe('~1h 6m')
    expect(formatProgress(frame({ percent: 0, eta_s: 95 })).amount).toBe('0% · ~1m 35s')
  })
})

describe('formatTokens', () => {
  it('shows the figure the header has room for', () => {
    expect(formatTokens(150_000)).toBe('150K')
    expect(formatTokens(1_200_000)).toBe('1.2M')
  })

  it('keeps one decimal only while it is worth a digit', () => {
    expect(formatTokens(1_500)).toBe('1.5K')
    expect(formatTokens(12_400)).toBe('12K')
    expect(formatTokens(2_000_000)).toBe('2M')
    expect(formatTokens(999_600)).toBe('1M')
    expect(formatTokens(999_600_000)).toBe('1B')
    expect(formatTokens(185_000)).toBe('185K')
    expect(formatTokens(12_000_000)).toBe('12M')
  })

  it('leaves a small count as it is, and a missing one as zero', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(842)).toBe('842')
    expect(formatTokens(undefined)).toBe('0')
    expect(formatTokens(null)).toBe('0')
    expect(formatTokens(NaN)).toBe('0')
    expect(formatTokens(12.25)).toBe('12.3')
    expect(formatTokens(0.025)).toBe('0.025')
  })
})


describe('formatCountdown', () => {
  const from = Date.parse('2026-09-08T12:00:00Z')
  const at = (seconds: number) => new Date(from + seconds * 1000).toISOString()

  it('is coarse above the hour and fine below the minute', () => {
    expect(formatCountdown(at(2 * 3600 + 14 * 60 + 30), from)).toBe('2h 14m')
    expect(formatCountdown(at(9 * 60 + 30), from)).toBe('9m 30s')
    expect(formatCountdown(at(12), from)).toBe('12s')
  })

  it('pads the trailing figure so a ticking chip keeps its width', () => {
    expect(formatCountdown(at(60 * 60 + 4 * 60), from)).toBe('1h 04m')
    expect(formatCountdown(at(5 * 60 + 3), from)).toBe('5m 03s')
  })

  it('reads a wait that is over as now, never as a negative duration', () => {
    expect(formatCountdown(at(0), from)).toBe('now')
    expect(formatCountdown(at(-90), from)).toBe('now')
  })

  it('takes a naive UTC timestamp the way every other reader here does', () => {
    expect(formatCountdown('2026-09-08T12:05:00', from)).toBe('5m 00s')
    expect(formatCountdown(null, from)).toBe('—')
  })
})

/**
 * What the motion is allowed to claim. A breathing edge says "moving right now" and a green
 * flash says "just finished well", so both predicates are narrower than "not finished" — and
 * the pipeline and the board spell each state differently.
 */
describe('isRunning and isSuccessful', () => {
  it('breathes for work in flight under either vocabulary', () => {
    expect(isRunning('running')).toBe(true)
    expect(isRunning('in_progress')).toBe(true)
  })

  it('does not breathe for work that is unfinished but still', () => {
    for (const status of ['pending', 'paused', 'todo', 'needs_review', 'blocked']) {
      expect(isRunning(status)).toBe(false)
    }
    // The difference from `isActive`, which a Cancel button reads.
    expect(isActive('pending') && isActive('paused')).toBe(true)
    expect(isRunning(null)).toBe(false)
  })

  it('settles for a node that succeeded and a task that is done, and nothing else', () => {
    expect(isSuccessful('success')).toBe(true)
    expect(isSuccessful('done')).toBe(true)
    for (const status of ['cached', 'skipped', 'cancelled', 'error', 'failed', undefined]) {
      expect(isSuccessful(status)).toBe(false)
    }
  })
})
