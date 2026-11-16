import { ProjectSession } from '@/session/project-session'

export type Query = Record<string, string | number | boolean | null | undefined>

/** Explicit process-wide operations; no implicit active-tab scope (§5.3.3). */
export function isGlobalApiPath(path: string): boolean {
  return path === '/settings/application' || path === '/changelog' || path === '/projects/open'
    || path === '/project/recent' || path.startsWith('/fs/') || path.startsWith('/runtime/')
    || path === '/updates' || path.startsWith('/updates/')
    || /^\/plugins\/[^/]+\/install-env$/.test(path)
    // studio-design §5.1/§6: only the unmounted Remote router's existing paths.
    // Canonical UUIDs match api/remote.py; no decoding or namespace-wide bypass.
    || path === '/remote/servers'
    || /^\/remote\/servers\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/(?:verify|projects)(?![\s\S])/.test(path)
}

function queryString(path: string, query?: Query): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) params.set(key, String(value))
  }
  return params.size ? `${path}?${params}` : path
}

/** Only API-relative paths are accepted: callers cannot escape the captured binding. */
export function scopedApiUrl(session: ProjectSession, path: string, query?: Query): string {
  session.capture()()
  if (!path.startsWith('/') || path.startsWith('//') || /[?#\\]/.test(path)
    || path.split('/').some(part => {
      const decoded = decodeURIComponent(part)
      return ['.', '..'].includes(decoded) || /[/\\]/.test(decoded)
    })
    || path.startsWith('/projects/') && path !== '/projects/open') throw new Error('Invalid API path')
  if (session.isLegacy || isGlobalApiPath(path)) return queryString(`/api${path}`, query)
  if (path === '/project/open') throw new Error('Legacy open is not available in a project renderer')
  if (!session.scope && path !== '/project') throw new Error('Project scope has not been bootstrapped')
  const scope = session.scope
  if (scope && ((query?.epoch !== undefined && query.epoch !== scope.epoch)
    || (query?.project_id !== undefined && query.project_id !== scope.project_id))) throw new Error('Conflicting project scope')
  return queryString(`/api/projects/${session.projectId}${path}`, scope
    ? { ...query, project_id: scope.project_id, epoch: scope.epoch } : query)
}

export function scopedWsUrl(session: ProjectSession, collab = false, cursor?: number): string {
  session.capture()()
  if (cursor !== undefined && (!Number.isSafeInteger(cursor) || cursor < 0)) throw new Error('Invalid replay cursor')
  const suffix = collab ? '/collab' : ''
  if (session.isLegacy) return queryString(`/ws${suffix}`, { since_seq: cursor })
  if (!session.scope) throw new Error('Project scope has not been bootstrapped')
  return queryString(`/ws/projects/${session.projectId}${suffix}`, { ...session.scope, since_seq: cursor })
}
