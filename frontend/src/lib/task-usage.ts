import type { AgentMessage } from '@/api/types'
import { parseUtc } from '@/lib/status'
import { outputAccounting } from '@/lib/transcript'
import { normalizedUsage, tokenDirections, usageRecord, type TokenDirections, type UsageFields } from '@/lib/token-directions'

export interface TaskUsage extends TokenDirections {
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
  total_input_tokens?: number
  total_output_tokens?: number
  messages: AgentMessage[]
}): TaskUsage {
  const result: TaskUsage = {
    contextTokens: null,
    contextWindow: null,
    totalTokens: count(task.total_tokens) ?? 0,
    uncachedTokens: count(task.total_uncached_tokens) ?? 0,
    cachedTokens: count(task.total_cached_tokens) ?? 0,
    unclassifiedTokens: 0,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
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
  let latestSnapshot: Record<string, unknown> | null = null
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
        latestSnapshot = message.data ?? null
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
  Object.assign(result, tokenDirections(task.total_tokens === result.totalTokens ? task : {}, result.totalTokens))
  if (latestSnapshot?.task_total_tokens === result.totalTokens) {
    const snapshot = tokenDirections({
      total_input_tokens: latestSnapshot.task_total_input_tokens,
      total_output_tokens: latestSnapshot.task_total_output_tokens,
      total_cached_tokens: latestSnapshot.task_total_cached_tokens,
      total_uncached_tokens: latestSnapshot.task_total_uncached_tokens,
    }, result.totalTokens)
    if (snapshot.inputTokens !== null || result.inputTokens === null) Object.assign(result, snapshot)
  }
  if (result.inputTokens === null || result.outputTokens === null) {
    // Old rows predate directional counters. Retained canonical evidence can fill them
    // only when it explains the whole lifetime total; a partial transcript stays unknown.
    const derived = transcriptDirections(task.messages, result.totalTokens)
    if (derived && (result.uncachedTokens === 0 && result.unclassifiedTokens > 0
        || derived.inputTokens! + derived.outputTokens! === result.uncachedTokens)
      && (result.cachedTokens === 0 && result.unclassifiedTokens > 0
        || derived.cacheReadTokens === result.cachedTokens)) Object.assign(result, derived)
  }
  return result
}

function transcriptDirections(messages: AgentMessage[], total: number): TokenDirections | null {
  const sum: UsageFields = [0, 0, 0, 0]
  let segment: UsageFields = [0, 0, 0, 0]
  const responses = new Map<string, UsageFields>()
  for (const message of outputAccounting(messages)) {
    const data = message.data ?? {}
    const raw = usageRecord(data.usage)
    const fields = raw ? normalizedUsage(raw) : null
    if (message.kind === 'result') {
      const reportedTotal = count(data.total_tokens)
      const reported = reportedTotal === null ? null : tokenDirections(data, reportedTotal)
      const resultFields: UsageFields | null = reported?.inputTokens !== null && reported?.inputTokens !== undefined
        && reported.outputTokens !== null && reported.cacheReadTokens !== null
        ? [reported.inputTokens, 0, reported.cacheReadTokens, reported.outputTokens] : fields
      // Creation and ordinary input are one direction, so compare their sum when a
      // newer aggregate has already folded cache creation into total_input_tokens.
      const combined: UsageFields = [segment[0] + segment[1], 0, segment[2], segment[3]]
      if (resultFields) {
        combined[0] = Math.max(combined[0], resultFields[0] + resultFields[1])
        combined[2] = Math.max(combined[2], resultFields[2])
        combined[3] = Math.max(combined[3], resultFields[3])
      }
      if (reportedTotal !== null && combined.reduce((value, part) => value + part, 0) !== reportedTotal) return null
      for (let index = 0; index < sum.length; index += 1) sum[index]! += combined[index]!
      segment = [0, 0, 0, 0]
    } else if (fields && (message.role === 'assistant'
      || (message.kind === 'system' && message.text === 'turn.completed'))) {
      const id = data.provider_response_id
      const previous = typeof id === 'string' && id ? responses.get(id) : null
      for (let index = 0; index < segment.length; index += 1) {
        segment[index]! += Math.max(0, fields[index]! - (previous?.[index] ?? 0))
      }
      if (typeof id === 'string' && id) {
        responses.set(id, fields.map((value, index) => Math.max(value, previous?.[index] ?? 0)) as UsageFields)
      }
    }
  }
  for (let index = 0; index < sum.length; index += 1) sum[index]! += segment[index]!
  if (sum.reduce((value, part) => value + part, 0) !== total) return null
  return { inputTokens: sum[0] + sum[1], outputTokens: sum[3], cacheReadTokens: sum[2] }
}
