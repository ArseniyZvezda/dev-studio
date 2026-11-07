/** Balanced binary treemap: area is proportional to tokens, including tiny sources (§10.4). */
import type { ContextSource } from '@/api/types'

/** IDs are positions in the current inventory. Compaction can reuse a position for a
 * different source or move an unchanged record to another position (§10.4). */
export function retainedFragment(previous: ContextSource, sources: readonly ContextSource[]): ContextSource | undefined {
  return sources.find(source => {
    if (source.category !== previous.category) return false
    if (previous.origin) return source.origin === previous.origin
    // Older inventories have no field path. A matching preview alone is ambiguous.
    if (source.id !== previous.id || source.label !== previous.label) return false
    if (previous.text_key || source.text_key) return previous.text_key === source.text_key
    return !source.truncated && !previous.truncated && source.text === previous.text
  })
}

export interface WeightedBlock { id: string; tokens: number }
export type MapBlock<T extends WeightedBlock = WeightedBlock> = T & { x: number; y: number; width: number; height: number }

export function contextMap<T extends WeightedBlock>(items: T[], width = 100, height = 62): MapBlock<T>[] {
  const sorted = items.filter(item => Number.isFinite(item.tokens) && item.tokens > 0)
    .slice().sort((a, b) => b.tokens - a.tokens || a.id.localeCompare(b.id))
  const result: MapBlock<T>[] = []
  function place(list: T[], x: number, y: number, w: number, h: number): void {
    if (!list.length) return
    if (list.length === 1) { result.push({ ...list[0]!, x, y, width: w, height: h }); return }
    const total = list.reduce((sum, item) => sum + item.tokens, 0)
    let split = 1
    let first = list[0]!.tokens
    while (split < list.length - 1 && Math.abs(first + list[split]!.tokens - total / 2) < Math.abs(first - total / 2)) {
      first += list[split++]!.tokens
    }
    const fraction = first / total
    if (w >= h) {
      place(list.slice(0, split), x, y, w * fraction, h)
      place(list.slice(split), x + w * fraction, y, w * (1 - fraction), h)
    } else {
      place(list.slice(0, split), x, y, w, h * fraction)
      place(list.slice(split), x, y + h * fraction, w, h * (1 - fraction))
    }
  }
  if (width > 0 && height > 0) place(sorted, 0, 0, width, height)
  return result
}
