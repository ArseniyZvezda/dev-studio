import type { EventPayloads } from '@/ws/events'

function normalise(path: string): string {
  const value = path.replace(/\\/g, '/').replace(/\/+$/, '')
  // Windows drive and UNC roots are case-insensitive; POSIX paths are not.
  return /^[a-z]:(?:\/|$)/i.test(value) || value.startsWith('//') ? value.toLowerCase() : value
}

function contains(directory: string, path: string): boolean {
  return directory === path || path.startsWith(`${directory}/`)
}

/** Match the managed project's watcher, even when the task's checkout is elsewhere (§10.4). */
export function taskTempChanged(
  tempRoot: string | null | undefined,
  projectRoot: string | null | undefined,
  event: EventPayloads['explorer.file_changed'],
): boolean {
  if (!tempRoot || !projectRoot) return false
  const project = normalise(projectRoot)
  const temp = normalise(tempRoot)
  if (!contains(project, temp)) return false
  if (event.root && normalise(event.root) !== project) return false

  // The omitted portion may include this task. Never infer "unchanged" from a partial batch.
  if (event.truncated) return true
  return event.changes.some(({ path }) => {
    const changed = normalise(`${project}/${path.replace(/^(\.\/)+/, '')}`)
    // Deleting/renaming a parent removes the task directory too. Deleted directories no
    // longer have is_dir=true, so this must use containment rather than the event's flag.
    return contains(temp, changed) || contains(changed, temp)
  })
}
