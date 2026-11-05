/** §10.4: sent tokens include cache creation; reads are their own third counter. */
export interface TokenDirections {
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
}

export function tokenCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

export function usageRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

/** Only a complete, reconciled split may describe the whole reported spend. */
export function tokenDirections(data: Record<string, unknown>, total: number): TokenDirections {
  const input = tokenCount(data.total_input_tokens)
  const output = tokenCount(data.total_output_tokens)
  const cached = tokenCount(data.total_cached_tokens)
  if (input !== null && output !== null && cached !== null && input + output + cached === total) {
    return { inputTokens: input, outputTokens: output, cacheReadTokens: cached }
  }
  const raw = usageRecord(data.usage)
  const fields = raw && normalizedUsage(raw)
  if (fields && fields.reduce((sum, value) => sum + value, 0) === total) {
    return { inputTokens: fields[0] + fields[1], outputTokens: fields[3], cacheReadTokens: fields[2] }
  }
  if (total === 0) return { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0 }
  // A zero migrated field does not establish that unknown historical spend had no cache reads.
  const uncached = tokenCount(data.total_uncached_tokens)
  return { inputTokens: null, outputTokens: null,
    cacheReadTokens: cached !== null && cached <= total && (uncached === null || uncached + cached <= total)
      && (cached > 0 || uncached === total) ? cached : null }
}

export type UsageFields = [number, number, number, number]
const FIELDS = ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'] as const

export function normalizedUsage(usage: Record<string, unknown>): UsageFields | null {
  const counts = FIELDS.map(key => tokenCount(usage[key]))
  return counts.every(value => value === null) ? null : counts.map(value => value ?? 0) as UsageFields
}
