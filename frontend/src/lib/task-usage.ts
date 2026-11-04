import type { AgentMessage } from '@/api/types'

export interface TaskUsage {
  contextTokens: number | null
  contextWindow: number | null
  totalTokens: number
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

/** §10.4: lifetime spending and the latest request's context are different counters. */
export function taskUsage(task: { total_tokens: number; messages: AgentMessage[] }): TaskUsage {
  const result: TaskUsage = {
    contextTokens: null,
    contextWindow: null,
    totalTokens: count(task.total_tokens) ?? 0,
  }
  let foundContext = false
  for (let index = task.messages.length - 1; index >= 0; index -= 1) {
    const message = task.messages[index]!
    if (['compressed summary', 'context_summary'].includes(message.kind)) break
    if (message.kind === 'system' && message.text === 'usage_update') {
      // The socket may arrive before the task snapshot. Both values are lifetime totals,
      // never increments; reading them twice must not charge the same request twice.
      const total = count(message.data?.task_total_tokens)
      if (total !== null) result.totalTokens = Math.max(result.totalTokens, total)
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
  return result
}
