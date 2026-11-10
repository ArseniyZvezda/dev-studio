/**
 * The one verbosity dial every transcript panel shares (§10.4, §10.9).
 *
 * One setting rather than one per panel: the task dialog, an ordinary chat, the Producer's
 * conversation and the Producer page's task cards are the same widget, and a page showing
 * several of them at once would otherwise read the same run two ways side by side. A
 * module-level ref, so turning it in one panel turns it in all of them; remembered in
 * `localStorage` because it is the reader's preference, not the project's.
 */
import { ref, watch, type Ref } from 'vue'

import { readVerbosity, type Verbosity } from '@/lib/transcript'

const STORAGE_KEY = 'voxarium_studio.transcript.verbosity'

let shared: Ref<Verbosity> | null = null

function stored(): unknown {
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function useVerbosity(): Ref<Verbosity> {
  if (shared) return shared
  const level = ref<Verbosity>(readVerbosity(stored()))
  watch(level, (value) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, value)
    } catch {
      // Unremembered, the dial still turns.
    }
  })
  shared = level
  return level
}
