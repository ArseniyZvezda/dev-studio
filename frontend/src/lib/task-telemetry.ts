import type { AgentMessage } from '@/api/types'

/** The result carries the provenance of a cost; unknown usage must not read as free. */
export function taskCost(total: number, messages: AgentMessage[]): string {
  const results = messages.filter((message) => message.kind === 'result')
  const incomplete = results.some((message) => message.data?.cost_unavailable === true)
  const estimated = results.some((message) => message.data?.cost_estimated === true)
  if (incomplete && !total) return '—'
  const prefix = incomplete ? '≥' : estimated ? '≈' : ''
  // Cheap runs would otherwise round to $0.000. Preserve the familiar precision above it.
  const digits = total > 0 && total < 0.001 ? 6 : 3
  return `${prefix}$${total.toFixed(digits)}`
}
