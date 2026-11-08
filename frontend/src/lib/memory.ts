/**
 * Project memory (§10.12) — the page's pure rules, mirrored from `teams/memory.py` where the
 * backend has the final word, so the form can say what is wrong before the request does.
 */

import type { MemoryEntry } from '@/api/types'

export const MEMORY_NAME_MAX = 64
export const MEMORY_DESCRIPTION_MAX = 300
export const MEMORY_CONTENT_MAX = 10_000

const NAME = /^[a-z0-9][a-z0-9-]*$/

/** Whether the backend will take `name` as an entry's id. */
export function isMemoryName(name: string): boolean {
  return name.length <= MEMORY_NAME_MAX && NAME.test(name)
}

/**
 * What a typed name becomes as an id: `Build uses UE 5.4` → `build-uses-ue-5-4`. Letters outside
 * a–z are dropped rather than transliterated, so a name written in Russian has to be given in
 * Latin — the id is a file name the backend and every agent spell the same way.
 */
export function toMemoryName(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, MEMORY_NAME_MAX)
    .replace(/-+$/, '')
}

/** Entries whose name or description holds every word of `query`; all of them for none. */
export function filterMemory<T extends Pick<MemoryEntry, 'name' | 'description'>>(
  entries: T[],
  query: string,
): T[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return entries
  return entries.filter((entry) => {
    const text = `${entry.name} ${entry.description}`.toLowerCase()
    return terms.every((term) => text.includes(term))
  })
}

/** A watcher path inside the memory folder — the folder itself counts. */
export function inMemoryFolder(path: string, folder: string): boolean {
  const base = folder.replace(/\/+$/, '')
  return path === base || path.startsWith(`${base}/`)
}

/** How full the index is, 0–1, for the meter; a cap of zero reads as full. */
export function indexFill(chars: number, cap: number): number {
  if (cap <= 0) return 1
  return Math.min(1, Math.max(0, chars / cap))
}
