/**
 * One status vocabulary for pipeline nodes, runs and team tasks.
 *
 * The spec's point that both halves of the app share infrastructure (§3) applies to the
 * UI too: a node that is running and a task that is running should look the same, so
 * both resolve their colour through this table.
 */

import type { NodeProgress } from '@/api/types'
import { formatDate, formatNumber, hasMessage, t } from '@/i18n'

const STATUS_TOKENS: Record<string, string> = {
  todo: '--vxs-status-todo',
  pending: '--vxs-status-pending',
  running: '--vxs-status-running',
  in_progress: '--vxs-status-running',
  success: '--vxs-status-success',
  done: '--vxs-status-success',
  error: '--vxs-status-error',
  failed: '--vxs-status-error',
  cancelled: '--vxs-status-cancelled',
  skipped: '--vxs-status-skipped',
  cached: '--vxs-status-cached',
  paused: '--vxs-status-paused',
  needs_review: '--vxs-status-review',
  blocked: '--vxs-status-blocked',
}

export function statusColor(status: string): string {
  return `var(${STATUS_TOKENS[status] ?? '--vxs-status-pending'})`
}

/**
 * A status as a word, in the language on screen. The vocabulary is `status.*` in the locale
 * files, keyed by the status itself; one the files do not know — a newer backend's — is
 * still shown, capitalised, rather than as a key.
 */
export function statusLabel(status: string): string {
  const key = `status.${status}`
  return hasMessage(key) ? t(key) : status.charAt(0).toUpperCase() + status.slice(1)
}

/**
 * Statuses that mean "this is not finished".
 *
 * `paused` is in here because the run it belongs to is still in the job engine holding its
 * place in the graph — Cancel still applies to it, and treating it as an outcome would put
 * a Run button next to a run that has not ended.
 */
export function isActive(status: string): boolean {
  return (
    status === 'running' ||
    status === 'in_progress' ||
    status === 'pending' ||
    status === 'paused'
  )
}

/**
 * Statuses that mean "work is happening right now" — the two names the vocabulary has for it.
 *
 * Narrower than `isActive` on purpose: a pending task has not started and a paused run is
 * holding still, and neither is anything a breathing edge may claim is moving (§7, the
 * indication half of the motion section).
 */
export function isRunning(status: string | null | undefined): boolean {
  return status === 'running' || status === 'in_progress'
}

/**
 * Statuses that mean "finished, and well" — a node or a run that succeeded, a task in Done.
 * What a status has to *arrive at* for its edge to flash green once.
 */
export function isSuccessful(status: string | null | undefined): boolean {
  return status === 'success' || status === 'done'
}

/**
 * Timestamps from the API are UTC and now carry an explicit `Z` — the backend re-attaches
 * the timezone on load, since SQLite itself stores none. The fallback stays because a
 * database written before that fix, or a plain `new Date()` on a bare value, would
 * otherwise be read as *local* time and shift every duration by the viewer's offset.
 */
export function parseUtc(value: string): number {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  return new Date(hasZone ? value : `${value}Z`).getTime()
}

export function formatDuration(from: string | null, to: string | null, now = Date.now()): string {
  if (!from) return '—'
  const start = parseUtc(from)
  const end = to ? parseUtc(to) : now
  return formatSeconds(Math.max(0, Math.round((end - start) / 1000)))
}

/**
 * How long there is left until an instant — `2h 14m`, `9m 30s`, `12s`.
 *
 * Coarse above the hour and fine below the minute, which is how a countdown is read: an
 * hour out, moving seconds are noise, and in the last minute they are the whole
 * information. The minutes and seconds are zero-padded so a ticking chip keeps its width
 * instead of jittering the row it sits in.
 *
 * Anything already due reads `now` rather than a negative duration: a wait that is over is
 * a restart that has not landed yet, and `-3s` would describe neither. `from` is passed in
 * rather than read here so a caller ticking its own clock re-renders exactly once a second.
 */
export function formatCountdown(target: string | null, from: number = Date.now()): string {
  if (!target) return '—'
  const seconds = Math.round((parseUtc(target) - from) / 1000)
  if (!Number.isFinite(seconds)) return '—'
  if (seconds <= 0) return t('units.now')
  if (seconds < 60) return t('units.seconds', { s: seconds })
  const minutes = Math.floor(seconds / 60)
  const pad = (value: number) => String(value).padStart(2, '0')
  if (minutes < 60) return t('units.minutesSeconds', { m: minutes, s: pad(seconds % 60) })
  return t('units.hoursMinutes', { h: Math.floor(minutes / 60), m: pad(minutes % 60) })
}

export function formatTime(value: string | null): string {
  if (!value) return '—'
  return formatDate(parseUtc(value), {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * The two halves of a node's progress line: what it is doing, and how far along.
 *
 * Split rather than joined so the renderer can let the left half be truncated and keep
 * the right half whole — on a node 240px wide, "Multiview diffusion" is the part that can
 * afford to lose its tail and "48% · ~56s" is not.
 *
 * Every field is optional, so both halves can come back empty; a caller renders nothing
 * when they do. `step`/`steps` are only spelled out when the node did not word the
 * position itself in `detail`, which is the more specific answer.
 */
export function formatProgress(progress: NodeProgress): { stage: string; amount: string } {
  const stage = [
    progress.label ?? '',
    progress.detail ?? (progress.steps ? `${progress.step ?? 0}/${progress.steps}` : ''),
  ]
    .filter(Boolean)
    .join(' · ')

  const amount = [
    progress.percent === null ? '' : `${Math.round(progress.percent)}%`,
    // A remaining-time estimate is a guess by nature; `~` is what stops it reading as a
    // promise the node cannot keep.
    progress.eta_s ? `~${formatSeconds(progress.eta_s)}` : '',
  ]
    .filter(Boolean)
    .join(' · ')

  return { stage, amount }
}

/** `45s`, `3m 12s`, `1h 6m` — a span of seconds at the precision a label has room for. */
function formatSeconds(seconds: number): string {
  const total = Math.round(seconds)
  if (total < 60) return t('units.seconds', { s: total })
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return t('units.minutesSeconds', { m: minutes, s: total % 60 })
  return t('units.hoursMinutes', { h: Math.floor(minutes / 60), m: minutes % 60 })
}

/**
 * What a session cost, in dollars, at the precision the number deserves.
 *
 * Sub-cent runs are real — a one-turn task lands around $0.004 — and rounding those to
 * `$0.00` reads as "free", so anything under a cent keeps three decimals. Above it the
 * figure is money and money has two, which is also what makes a column of totals line up.
 *
 * Grouped in the interface language's way (`$6,933.06`): a project's total reaches four and
 * five digits, and an ungrouped run of them has to be counted rather than read.
 */
export function formatCost(usd: number): string {
  if (!usd) return '$0'
  const digits = usd < 0.01 ? 3 : 2
  return `$${formatNumber(usd, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

/**
 * Compact token counts throughout the UI: `150K`, `1.2M`, including tooltips (§10.4).
 *
 * Three significant figures at most — `1.5K` and `12K` and `150K` are all the same width
 * on screen, which is what lets the figure sit in a row of other figures without the row
 * jumping as a run grows. Below a thousand the number is short enough to stand as it is.
 */
export function formatTokens(count: number | null | undefined): string {
  const value = typeof count === 'number' && Number.isFinite(count) ? Math.max(0, count) : 0
  if (value > 0 && value < 1) return `${Number(value.toPrecision(3))}`
  if (value < 999.95) return `${Number(value.toFixed(1))}`
  const scaled = (amount: number, unit: string) => {
    const text = amount < 10 ? amount.toFixed(1).replace(/\.0$/, '') : `${Math.round(amount)}`
    return `${text}${unit}`
  }
  if (value < 999_500) return scaled(value / 1000, 'K')
  if (value < 999_500_000) return scaled(value / 1_000_000, 'M')
  return scaled(value / 1_000_000_000, 'B')
}

const BYTE_UNITS = ['units.kilobytes', 'units.megabytes', 'units.gigabytes'] as const

export function formatBytes(size: number): string {
  if (size < 1024) return t('units.bytes', { value: size })
  let value = size / 1024
  let unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  // One decimal under ten, none above — in the language's own decimal separator.
  const digits = value < 10 ? 1 : 0
  const amount = formatNumber(value, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: false,
  })
  return t(BYTE_UNITS[unit]!, { value: amount })
}
