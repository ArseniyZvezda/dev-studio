/**
 * What the cost chart's axes say — data, not layout, which is why it is testable here.
 */
import { describe, expect, it } from 'vitest'

import type { CostPoint } from '@/api/types'
import { dayDomain, dayMs, dayTicks, formatAxisCost, niceTicks, smoothCostPath } from '@/lib/cost-chart'

const DAY = 86_400_000

const point = (day: string, cumulative: number): CostPoint => ({
  day,
  spent_usd: 0,
  cumulative_usd: cumulative,
  runs: 1,
})

describe('dayDomain', () => {
  it('runs from the first spend to today, so a project that stopped spending shows a flat tail', () => {
    const domain = dayDomain([point('2026-08-24', 1), point('2026-09-03', 2)], '2026-09-07')
    expect(domain).toEqual({ start: dayMs('2026-08-24'), end: dayMs('2026-09-07') })
  })

  it('is never shorter than a week, taken backwards so the axis never enters the future', () => {
    const domain = dayDomain([point('2026-09-07', 1)], '2026-09-07')
    expect(domain).toEqual({ start: dayMs('2026-09-01'), end: dayMs('2026-09-07') })
    const recent = dayDomain([point('2026-09-05', 1)], '2026-09-07')
    expect(recent).toEqual({ start: dayMs('2026-09-01'), end: dayMs('2026-09-07') })
  })

  it('reads an empty series as a week ending today', () => {
    const domain = dayDomain([], '2026-09-07')
    expect(domain.end).toBe(dayMs('2026-09-07'))
    expect(domain.end - domain.start).toBe(6 * DAY)
  })
})

describe('dayTicks', () => {
  it('steps whole days and always starts at the first one', () => {
    const start = dayMs('2026-09-01')
    const ticks = dayTicks(start, start + 6 * DAY)
    expect(ticks.map((ms) => (ms - start) / DAY)).toEqual([0, 2, 4, 6])
  })

  it('does not put a second label on top of the last stepped one', () => {
    const start = dayMs('2026-09-01')
    // 14 days at 6 ticks steps by 3: 0, 3, 6, 9, 12 — the end is 2 days on, more than
    // half a step, so it is added; at 13 days it would be 1 day on and is not.
    expect(dayTicks(start, start + 14 * DAY).map((ms) => (ms - start) / DAY)).toEqual([
      0, 3, 6, 9, 12, 14,
    ])
    expect(dayTicks(start, start + 13 * DAY).map((ms) => (ms - start) / DAY)).toEqual([
      0, 3, 6, 9, 12,
    ])
  })
})

describe('niceTicks', () => {
  it('lands on numbers a reader can say, with the top tick at or above the maximum', () => {
    expect(niceTicks(2.219)).toEqual([0, 1, 2, 3])
    expect(niceTicks(0.41)).toEqual([0, 0.2, 0.4, 0.6])
    expect(niceTicks(12.5)).toEqual([0, 5, 10, 15])
    expect(niceTicks(0.004)).toEqual([0, 0.001, 0.002, 0.003, 0.004])
  })

  it('draws an axis even when nothing has been spent', () => {
    expect(niceTicks(0)).toEqual([0, 1])
  })
})

describe('formatAxisCost', () => {
  it('says only as much as tells ticks apart', () => {
    expect([0, 0.2, 1, 2.5, 15].map(formatAxisCost)).toEqual(['$0', '$0.20', '$1', '$2.5', '$15'])
  })

  it('groups a tick in the thousands', () => {
    expect(formatAxisCost(8000)).toBe('$8,000')
  })
})


describe('smoothCostPath', () => {
  it('handles no data and a single point', () => {
    expect(smoothCostPath([])).toBe('')
    expect(smoothCostPath([{ px: 12, py: 34 }])).toBe('M12.0 34.0')
  })

  it('keeps control points within each interval and the tail flat', () => {
    const points = [{ px: 0, py: 200 }, { px: 5, py: 100 }, { px: 90, py: 20 }, { px: 120, py: 20 }]
    const segments = smoothCostPath(points).split(' C').slice(1)
    expect(segments).toHaveLength(3)
    segments.forEach((segment, i) => {
      const values = segment.split(' ').map(Number)
      for (let j = 0; j < 6; j += 2) {
        expect(values[j]).toBeGreaterThanOrEqual(points[i]!.px)
        expect(values[j]).toBeLessThanOrEqual(points[i + 1]!.px)
        expect(values[j + 1]).toBeGreaterThanOrEqual(points[i + 1]!.py)
        expect(values[j + 1]).toBeLessThanOrEqual(points[i]!.py)
      }
    })
    expect(segments[2]!.split(' ').map(Number).filter((_, i) => i % 2)).toEqual([20, 20, 20])
  })
})
