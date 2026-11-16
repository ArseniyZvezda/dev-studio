/** One immutable project per renderer; legacy selection is a separate adapter (§5.3). */
export interface ProjectScope {
  readonly project_id: string
  readonly epoch: string
}

export class StaleProjectResponseError extends Error {
  constructor() { super('Project session is no longer current'); this.name = 'StaleProjectResponseError' }
}

function checkedId(value: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('Invalid project handle')
  return value
}

export class ProjectSession {
  private readonly id: string | null
  private binding: ProjectScope | null = null
  private revision = 0
  private invalid = false

  constructor(projectId: string | null = null) {
    this.id = projectId === null ? null : checkedId(projectId)
  }

  static fromPath(path: string): ProjectSession {
    if (path !== '/p' && !path.startsWith('/p/')) return new ProjectSession()
    const id = path.split('/')[2] ?? ''
    return new ProjectSession(checkedId(decodeURIComponent(id)))
  }

  get projectId(): string | null { return this.id }
  get isLegacy(): boolean { return this.projectId === null }
  get routeBase(): string { return this.isLegacy ? '' : `/p/${this.projectId}` }
  get scope(): ProjectScope | null { return this.binding }
  get generation(): number { return this.revision }

  bind(scope: ProjectScope): void {
    if (this.invalid) throw new StaleProjectResponseError()
    if (this.isLegacy || scope.project_id !== this.projectId || typeof scope.epoch !== 'string' || !scope.epoch) {
      throw new Error('Conflicting project scope')
    }
    if (this.binding && !this.matches(scope)) throw new Error('Project binding is immutable')
    this.binding = Object.freeze({ project_id: scope.project_id, epoch: scope.epoch })
  }

  /** Called before legacy reload or when this renderer is disposed, never on tab focus. */
  invalidate(): void { this.revision += 1; this.invalid = true }

  capture(): () => void {
    const generation = this.revision
    const assertCurrent = () => {
      if (this.invalid || generation !== this.revision) throw new StaleProjectResponseError()
    }
    assertCurrent()
    return assertCurrent
  }

  matches(value: { project_id?: unknown; epoch?: unknown }): boolean {
    if (this.invalid) return false
    if (this.isLegacy) return value.project_id === undefined && value.epoch === undefined
    return this.binding !== null && value.project_id === this.binding.project_id && value.epoch === this.binding.epoch
  }
}

export const rendererSession = ProjectSession.fromPath(typeof window === 'undefined' ? '/' : window.location.pathname)
