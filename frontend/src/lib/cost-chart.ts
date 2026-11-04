/**
 * The arithmetic behind the Statistics page's cost chart (§10.4).
 *
 * Kept out of the component so it can be checked without a DOM: which days the axis
 * spans, where its ticks fall and what they say are all decisions a reader can be misled
 * by, and none of them needs a browser to be wrong in.
 */

import type { CostPoint } from '@/api/types'
import { formatDate, formatNumber } from '@/i18n'

const DAY_MS = 86_400_000

/** Smooth monotone interpolation (§10.4): no invented peaks between recorded totals. */
export function smoothCostPath(points: readonly { px: number; py: number }[]): string {
  if (!points.length) return ''
  const slopes = points.slice(1).map((point, i) =>
    (point.py - points[i]!.py) / (point.px - points[i]!.px),
  )
  const tangents = points.map((_, i) => {
    const left = slopes[i - 1] ?? slopes[0] ?? 0
    const right = slopes[i] ?? left
    return left * right <= 0 ? 0 : Math.sign(left) * Math.min(Math.abs(left), Math.abs(right))
  })
  const coord = (x: number, y: number) => `${x.toFixed(1)} ${y.toFixed(1)}`
  const parts = [`M${coord(points[0]!.px, points[0]!.py)}`]
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1]!
    const b = points[i]!
    const third = (b.px - a.px) / 3
    parts.push(`C${coord(a.px + third, a.py + tangents[i - 1]! * third)} ${coord(b.px - third, b.py - tangents[i]! * third)} ${coord(b.px, b.py)}`)
  }
  return parts.join(' ')
}

/** Milliseconds since the epoch for an ISO calendar date, at UTC midnight. */
export function dayMs(day: string): number {
  return Date.parse(`${day}T00:00:00Z`)
}

/** Today's ISO calendar date in UTC — the same frame the backend buckets days in. */
export function todayIso(now = new Date()): string {
  return now.toISOString().slice(0, 10)
}

/**
 * The x domain: from the first day something was spent up to today, and never shorter
 * than a week. Running it to today rather than to the last spend is the honest reading —
 * a curve that stops a month ago is a project that stopped spending a month ago, and a
 * flat tail says so. The week floor is taken *backwards*, so a single point has somewhere
 * to be drawn and the axis never runs into days that have not happened.
 */
export function dayDomain(series: CostPoint[], today: string): { start: number; end: number } {
  const todayMs = dayMs(today)
  const first = series.length ? dayMs(series[0]!.day) : todayMs
  const last = series.length ? dayMs(series[series.length - 1]!.day) : todayMs
  const start = Math.min(first, todayMs - 6 * DAY_MS)
  const end = Math.max(last, todayMs)
  return { start, end }
}

/**
 * Day ticks across the domain: whole days, evenly stepped, at most about `count`, and
 * always including the first day. The last day is added only when the stepped ticks stop
 * well short of it, so two labels never sit on top of each other at the right edge.
 */
export function dayTicks(start: number, end: number, count = 6): number[] {
  const days = Math.round((end - start) / DAY_MS)
  if (days <= 0) return [start]
  const step = Math.max(1, Math.ceil(days / Math.max(1, count - 1)))
  const ticks: number[] = []
  for (let day = 0; day <= days; day += step) ticks.push(start + day * DAY_MS)
  const last = ticks[ticks.length - 1]!
  if (end - last >= (step * DAY_MS) / 2) ticks.push(end)
  return ticks
}

/**
 * Round tick values for a 0..max axis: zero, then steps of a size a reader can say
 * (1, 2, 2.5 or 5 times a power of ten), the last one at or above `max` so the top of the
 * plot is a labelled line rather than a value nobody can read off.
 */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1]
  const rough = max / count
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const residual = rough / magnitude
  const factor = residual <= 1 ? 1 : residual <= 2 ? 2 : residual <= 2.5 ? 2.5 : residual <= 5 ? 5 : 10
  const step = factor * magnitude
  const top = Math.ceil(max / step - 1e-9) * step
  const ticks: number[] = []
  for (let value = 0; value <= top + step * 1e-6; value += step) {
    ticks.push(Number(value.toFixed(10)))
  }
  return ticks
}

/**
 * A tick label: `$0`, `$0.20`, `$1`, `$2.5`, `$15`. Shorter than `formatCost`, which
 * carries the precision a total deserves; an axis carries only what tells ticks apart.
 */
export function formatAxisCost(value: number): string {
  if (!value) return '$0'
  if (value < 1) return `$${formatNumber(value, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  // Grouped like the totals beside the chart, so `$8,000` on the axis and `$6,933.06` at the
  // line's end read as the same kind of number.
  return `$${formatNumber(value, { maximumFractionDigits: 1 })}`
}

/** `Sep 4` — the day, in the interface language and the UTC frame the series was bucketed in. */
export function formatDay(ms: number): string {
  return formatDate(ms, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  })
}
