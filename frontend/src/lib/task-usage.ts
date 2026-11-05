import type { AgentMessage } from '@/api/types'
import { parseUtc } from '@/lib/status'

export interface TaskUsage {
  contextTokens: number | null
  contextWindow: number | null
  totalTokens: number
  uncachedTokens: number
  cachedTokens: number
  unclassifiedTokens: number
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

/** §10.4: lifetime spending and the latest request's context are different counters. */
export function taskUsage(task: {
  total_tokens: number
  total_uncached_tokens?: number
  total_cached_tokens?: number
  messages: AgentMessage[]
}): TaskUsage {
  const result: TaskUsage = {
    contextTokens: null,
    contextWindow: null,
    totalTokens: count(task.total_tokens) ?? 0,
    uncachedTokens: count(task.total_uncached_tokens) ?? 0,
    cachedTokens: count(task.total_cached_tokens) ?? 0,
    unclassifiedTokens: 0,
  }
  if (result.uncachedTokens + result.cachedTokens > result.totalTokens) {
    result.uncachedTokens = result.cachedTokens = 0
  }
  const hasBreakdown = task.total_uncached_tokens !== undefined && task.total_cached_tokens !== undefined
  const repairedAt = task.messages.reduce((latest, message) => {
    if (message.kind !== 'accounting') return latest
    const repair = message.data?.repair
    if (!repair || typeof repair !== 'object' || Array.isArray(repair)) return latest
    const cutoff = (repair as Record<string, unknown>).cutoff_at
    const time = typeof cutoff === 'string' ? parseUtc(cutoff) : NaN
    return Number.isFinite(time) ? Math.max(latest, time) : latest
  }, -Infinity)
  let foundTotals = false
  let foundContext = false
  for (let index = task.messages.length - 1; index >= 0; index -= 1) {
    const message = task.messages[index]!
    if (['compressed summary', 'context_summary'].includes(message.kind)) break
    if (message.kind === 'system' && message.text === 'usage_update') {
      // Only the newest snapshot can be ahead of the task row. Legacy snapshots predate
      // repaired accounting and must never resurrect a total that the repair reduced.
      const total = count(message.data?.task_total_tokens)
      const uncached = count(message.data?.task_total_uncached_tokens)
      const cached = count(message.data?.task_total_cached_tokens)
      if (!foundTotals && total !== null && parseUtc(message.created_at) > repairedAt) {
        const improvesBreakdown = uncached !== null && cached !== null
          && uncached + cached > result.uncachedTokens + result.cachedTokens
        if (uncached !== null && cached !== null && uncached + cached <= total
          && (total > result.totalTokens || (total === result.totalTokens && improvesBreakdown))) {
          result.totalTokens = total
          result.uncachedTokens = uncached
          result.cachedTokens = cached
        } else if (!hasBreakdown && uncached === null && cached === null) {
          result.totalTokens = Math.max(result.totalTokens, total)
        }
        foundTotals = true
      }
      const context = count(message.data?.context_tokens)
      if (!foundContext) {
        // A new run can report spending before it knows its context. That unknown is
        // authoritative too: do not resurrect an older run's or member's context.
        result.contextTokens = context
        const window = count(message.data?.context_window)
        result.contextWindow = context !== null && window !== null && window > 0 ? window : null
        foundContext = true
      }
      continue
    }
    if (foundContext || message.role !== 'assistant' || message.kind === 'result' ||
      message.data?.parent_tool_use_id) continue
    // Older Claude/Qwen transcripts predate usage_update. Each assistant usage is one
    // request; result and Codex turn.completed usage instead describe a whole run.
    const raw = message.data?.usage
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const usage = raw as Record<string, unknown>
    const input = count(usage.input_tokens)
    const cached = count(usage.cache_read_input_tokens)
    if (input === null && cached === null) continue
    result.contextTokens = (input ?? 0) + (cached ?? 0) + (count(usage.cache_creation_input_tokens) ?? 0)
    foundContext = true
  }
  // Missing historical classification stays unknown; it is never counted as uncached.
  result.unclassifiedTokens = result.totalTokens - result.uncachedTokens - result.cachedTokens
  return result
}
