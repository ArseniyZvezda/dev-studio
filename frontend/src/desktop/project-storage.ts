/**
 * Browser-only project UI state (dev spec §5.3.2). The root must come from the
 * backend's canonical project metadata, never a user-entered path or runtime id.
 * Capture the resulting key before delayed work; do not resolve a selected project
 * when that work completes. Global preferences must not use this namespace.
 */
export function projectStorageKey(root: string | null | undefined, key: string): string | null {
  if (!root) return null
  // A tuple is unambiguous even when roots and entity ids contain separators.
  return `voxarium_studio.project.v1:${JSON.stringify([root, key])}`
}

/** No root means no persistence, not a fallback to an unowned legacy key. */
export function readProjectValue(key: string | null): string | null {
  if (key === null) return null
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeProjectValue(key: string | null, value: string | null): void {
  if (key === null) return
  try {
    if (typeof window === 'undefined') return
    if (value !== null) window.localStorage.setItem(key, value)
    else window.localStorage.removeItem(key)
  } catch {
    // Disabled/full browser storage must never prevent editing or sending.
  }
}
