/**
 * The arithmetic behind the Statistics page's activity heatmap (§10.4).
 *
 * Which weeks the grid spans, which row a day sits in, how dark its cell is and where the
 * month labels fall are all decisions a reader can be misled by, and none of them needs a
 * browser to be wrong in — so they live here, beside `cost-chart.ts`, and not in the
 * component.
 *
 * Weeks start on Monday and days are UTC calendar days, the frame the backend bucketed
 * them in; the grid therefore agrees with the cost chart's table twin about which day a
 * late-evening run belongs to.
 */

import type { ActivityDay } from '@/api/types'
import { formatDate, t } from '@/i18n'
import { dayMs } from '@/lib/cost-chart'
import { formatCost } from '@/lib/status'

const DAY_MS = 86_400_000
const WEEK_MS = 7 * DAY_MS
/** 2024-01-01, a Monday at UTC midnight: the date a weekday row is named by formatting. */
const A_MONDAY = Date.UTC(2024, 0, 1)

/** How many shades a cell can wear: none, then four steps of the one hue. */
export type Level = 0 | 1 | 2 | 3 | 4

export interface HeatCell {
  /** ISO calendar date, UTC. */
  day: string
  ms: number
  /** 0 = Monday … 6 = Sunday: the row. */
  weekday: number
  /** After today — drawn as nothing, since the day has not happened. */
  future: boolean
  entry: ActivityDay | null
  level: Level
}

export interface HeatWeek {
  /** Monday 00:00 UTC. */
  start: number
  /** Always seven, Monday first. */
  cells: HeatCell[]
}

/** Monday 00:00 UTC of the week `ms` falls in. */
export function weekStart(ms: number): number {
  const midnight = Math.floor(ms / DAY_MS) * DAY_MS
  const weekday = (new Date(midnight).getUTCDay() + 6) % 7
  return midnight - weekday * DAY_MS
}

/**
 * The shade for a day, against the busiest day on the grid: four steps of one hue, none
 * for nothing. Proportional rather than quantile-based so a reader can compare two cells
 * by eye — the darkest is the busiest day, half as dark is about half as busy.
 */
export function levelOf(count: number, max: number): Level {
  if (count <= 0 || max <= 0) return 0
  return Math.min(4, Math.max(1, Math.ceil((4 * count) / max))) as Level
}

/**
 * The grid: `weeks` columns of seven days, oldest first, ending in the week that holds
 * `today`. The days of that last week after today are marked `future`; a day the backend
 * listed nothing for carries no entry and level 0.
 */
export function heatmapWeeks(activity: ActivityDay[], today: string, weeks: number): HeatWeek[] {
  const byDay = new Map(activity.map((entry) => [entry.day, entry]))
  const busiest = Math.max(0, ...activity.map((entry) => entry.activity))
  const todayMs = dayMs(today)
  const lastWeek = weekStart(todayMs)
  const columns: HeatWeek[] = []
  for (let back = Math.max(1, weeks) - 1; back >= 0; back -= 1) {
    const start = lastWeek - back * WEEK_MS
    const cells: HeatCell[] = []
    for (let weekday = 0; weekday < 7; weekday += 1) {
      const ms = start + weekday * DAY_MS
      const day = new Date(ms).toISOString().slice(0, 10)
      const entry = byDay.get(day) ?? null
      cells.push({
        day,
        ms,
        weekday,
        future: ms > todayMs,
        entry,
        level: entry ? levelOf(entry.activity, busiest) : 0,
      })
    }
    columns.push({ start, cells })
  }
  return columns
}

export interface Geometry {
  /** Cell edge, px. */
  cell: number
  /** Gap between cells, px. */
  gap: number
  /** Room on the left for the weekday labels, px. */
  gutter: number
  /** Room past the last column for a month name that starts there, px. */
  pad: number
  min: number
  max: number
}

export const GEOMETRY: Geometry = { cell: 11, gap: 3, gutter: 32, pad: 24, min: 8, max: 53 }

/**
 * How many week columns fit in `width` pixels: never fewer than `min`, so a narrow pane
 * still shows a couple of months, and never more than a year and a week — GitHub's span,
 * and past it the grid says nothing a line chart would not.
 */
export function weeksThatFit(width: number, geometry: Geometry = GEOMETRY): number {
  const { cell, gap, gutter, pad, min, max } = geometry
  const usable = Math.max(0, width - gutter - pad)
  return Math.max(min, Math.min(max, Math.floor((usable + gap) / (cell + gap))))
}

export interface MonthLabel {
  /** Which column the label sits over. */
  index: number
  label: string
}

/** `Sep` — the month, in the interface language and the UTC frame the grid is drawn in. */
export function formatMonth(ms: number): string {
  return formatDate(ms, { month: 'short', timeZone: 'UTC' })
}

/** `Mon` — the name of a weekday row (0 = Monday … 6 = Sunday), in the interface language. */
export function formatWeekday(weekday: number): string {
  return formatDate(A_MONDAY + weekday * DAY_MS, { weekday: 'short', timeZone: 'UTC' })
}

/**
 * One label per month, over the first column whose Monday falls in it. The first
 * column's own label is dropped when another follows within three columns — the two
 * would overlap, and the later one is the month most of the grid's left edge is in.
 */
export function monthLabels(weeks: HeatWeek[]): MonthLabel[] {
  const labels: MonthLabel[] = []
  let previous = -1
  weeks.forEach((week, index) => {
    const month = new Date(week.start).getUTCMonth()
    if (month !== previous) {
      labels.push({ index, label: formatMonth(week.start) })
      previous = month
    }
  })
  if (labels.length > 1 && labels[1]!.index - labels[0]!.index < 3) labels.shift()
  return labels
}

export interface WindowTotals {
  runs: number
  finished: number
  spent_usd: number
  /** Days on the grid with any activity at all. */
  activeDays: number
}

/** What the visible grid adds up to — the caption under it. */
export function windowTotals(weeks: HeatWeek[]): WindowTotals {
  const totals: WindowTotals = { runs: 0, finished: 0, spent_usd: 0, activeDays: 0 }
  for (const week of weeks) {
    for (const cell of week.cells) {
      if (!cell.entry) continue
      totals.runs += cell.entry.runs
      totals.finished += cell.entry.finished
      totals.spent_usd += cell.entry.spent_usd
      if (cell.entry.activity > 0 || cell.entry.spent_usd > 0) totals.activeDays += 1
    }
  }
  return totals
}

/**
 * `2 runs · 1 task finished · $0.41`, or `No activity` — the tooltip's second line. Each part
 * is a whole plural message; only the separator between them is not.
 */
export function describeCell(cell: HeatCell): string {
  const entry = cell.entry
  if (!entry || (entry.activity <= 0 && entry.spent_usd <= 0)) {
    return t('statistics.activity.noActivity')
  }
  const parts: string[] = []
  if (entry.runs) parts.push(t('statistics.activity.runs', { count: entry.runs }))
  if (entry.finished) {
    parts.push(t('statistics.activity.tasksFinished', { count: entry.finished }))
  }
  if (entry.spent_usd > 0) parts.push(formatCost(entry.spent_usd))
  return parts.join(' · ')
}
