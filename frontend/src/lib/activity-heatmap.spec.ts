/**
 * Where the activity heatmap puts each day and how dark it draws it — data, not layout,
 * which is why it is testable here.
 */
import { describe, expect, it } from 'vitest'

import type { ActivityDay } from '@/api/types'
import {
  describeCell,
  formatMonth,
  heatmapWeeks,
  levelOf,
  monthLabels,
  weekStart,
  weeksThatFit,
  windowTotals,
} from '@/lib/activity-heatmap'
import { dayMs } from '@/lib/cost-chart'

const DAY = 86_400_000

const day = (iso: string, overrides: Partial<ActivityDay> = {}): ActivityDay => ({
  day: iso,
  runs: 1,
  finished: 0,
  spent_usd: 0,
  activity: 1,
  ...overrides,
})

// 2026-09-07 is a Monday; the whole file leans on that.
const TODAY = '2026-09-07'

describe('weekStart', () => {
  it('is the Monday of the week, at UTC midnight', () => {
    expect(weekStart(dayMs('2026-09-07'))).toBe(dayMs('2026-09-07'))
    expect(weekStart(dayMs('2026-09-13'))).toBe(dayMs('2026-09-07'))
    expect(weekStart(dayMs('2026-09-06'))).toBe(dayMs('2026-08-31'))
  })

  it('floors a moment inside a day to that day first', () => {
    expect(weekStart(dayMs('2026-09-09') + 5 * 3_600_000)).toBe(dayMs('2026-09-07'))
  })
})

describe('heatmapWeeks', () => {
  it('ends in the week that holds today, oldest column first, seven cells per column', () => {
    const weeks = heatmapWeeks([], TODAY, 3)
    expect(weeks).toHaveLength(3)
    expect(weeks.map((week) => week.start)).toEqual([
      dayMs('2026-08-24'),
      dayMs('2026-08-31'),
      dayMs('2026-09-07'),
    ])
    expect(weeks[2]!.cells.map((cell) => cell.day)).toEqual([
      '2026-09-07',
      '2026-09-08',
      '2026-09-09',
      '2026-09-10',
      '2026-09-11',
      '2026-09-12',
      '2026-09-13',
    ])
  })

  it('marks the days after today as future and nothing before it', () => {
    const last = heatmapWeeks([], '2026-09-09', 2)[1]!
    expect(last.cells.map((cell) => cell.future)).toEqual([
      false,
      false,
      false,
      true,
      true,
      true,
      true,
    ])
  })

  it('puts a day in its weekday row and shades it against the busiest day', () => {
    const weeks = heatmapWeeks(
      [day('2026-09-02', { activity: 4, runs: 4 }), day('2026-09-06', { activity: 1 })],
      TODAY,
      2,
    )
    const wednesday = weeks[0]!.cells[2]!
    const sunday = weeks[0]!.cells[6]!
    expect(wednesday.day).toBe('2026-09-02')
    expect(wednesday.weekday).toBe(2)
    expect(wednesday.level).toBe(4)
    expect(sunday.day).toBe('2026-09-06')
    expect(sunday.level).toBe(1)
    expect(weeks[1]!.cells[0]!.entry).toBeNull()
    expect(weeks[1]!.cells[0]!.level).toBe(0)
  })

  it('never draws fewer than one column', () => {
    expect(heatmapWeeks([], TODAY, 0)).toHaveLength(1)
  })
})

describe('levelOf', () => {
  it('is nothing for nothing and the darkest step for the busiest day', () => {
    expect(levelOf(0, 5)).toBe(0)
    expect(levelOf(3, 0)).toBe(0)
    expect(levelOf(5, 5)).toBe(4)
    expect(levelOf(1, 1)).toBe(4)
  })

  it('steps in proportion, and never below the first shade for a day with anything', () => {
    expect([1, 3, 5, 8, 10].map((count) => levelOf(count, 10))).toEqual([1, 2, 2, 4, 4])
    expect([1, 2, 3, 4].map((count) => levelOf(count, 4))).toEqual([1, 2, 3, 4])
    expect(levelOf(1, 100)).toBe(1)
  })
})

describe('weeksThatFit', () => {
  it('counts whole columns between the label gutter and the month pad, within a floor and a year', () => {
    // 32px gutter and 24px pad, then 14px per column with the last needing only 11px.
    expect(weeksThatFit(56 + 11)).toBe(8)
    expect(weeksThatFit(56 + 20 * 14 - 3)).toBe(20)
    expect(weeksThatFit(56 + 20 * 14 - 4)).toBe(19)
    expect(weeksThatFit(5000)).toBe(53)
    expect(weeksThatFit(0)).toBe(8)
  })
})

describe('monthLabels', () => {
  it('labels the first column of each month by its Monday', () => {
    // Mondays: Aug 10, 17, 24, 31, Sep 7 — the September label lands on the last column.
    const labels = monthLabels(heatmapWeeks([], TODAY, 5))
    expect(labels.map((label) => label.index)).toEqual([0, 4])
    // In the interface language — the same one the cost chart's ticks use.
    expect(labels[1]!.label).toBe(formatMonth(dayMs('2026-09-07')))
    expect(labels[0]!.label).toBe(formatMonth(dayMs('2026-08-10')))
  })

  it('drops a first-column label that another would overlap', () => {
    // Mondays: Aug 31, Sep 7 — August gets one column, which is not room for a word.
    const labels = monthLabels(heatmapWeeks([], TODAY, 2))
    expect(labels.map((label) => label.index)).toEqual([1])
  })
})

describe('windowTotals', () => {
  it('adds up what is on the grid and counts the days that had anything', () => {
    const weeks = heatmapWeeks(
      [
        day('2026-09-01', { runs: 2, finished: 1, spent_usd: 0.5, activity: 3 }),
        day('2026-09-03', { runs: 0, finished: 0, spent_usd: 0.25, activity: 0 }),
        day('2026-01-01', { runs: 9, activity: 9 }),
      ],
      TODAY,
      2,
    )
    expect(windowTotals(weeks)).toEqual({ runs: 2, finished: 1, spent_usd: 0.75, activeDays: 2 })
  })
})

describe('describeCell', () => {
  const cellFor = (entry: ActivityDay | null) =>
    heatmapWeeks(entry ? [entry] : [], TODAY, 1).flatMap((week) => week.cells)[0]!

  it('says what a day was made of, in the order runs, tasks, money', () => {
    expect(
      describeCell(cellFor(day(TODAY, { runs: 2, finished: 1, spent_usd: 0.41, activity: 3 }))),
    ).toBe('2 runs · 1 task finished · $0.41')
    expect(describeCell(cellFor(day(TODAY, { runs: 1, activity: 1 })))).toBe('1 run')
  })

  it('reads an empty day, and a day that only booked a residual, honestly', () => {
    expect(describeCell(cellFor(null))).toBe('No activity')
    expect(
      describeCell(cellFor(day(TODAY, { runs: 0, finished: 0, spent_usd: 0.05, activity: 0 }))),
    ).toBe('$0.05')
  })
})

describe('the grid and the cost chart agree on days', () => {
  it('uses the same UTC midnight for a date', () => {
    const cell = heatmapWeeks([day(TODAY)], TODAY, 1)[0]!.cells[0]!
    expect(cell.ms).toBe(dayMs(TODAY))
    expect(cell.ms % DAY).toBe(0)
  })
})
