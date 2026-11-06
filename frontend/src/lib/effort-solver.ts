import type { EffortSolverWeights } from '@/api/types'

export const EFFORT_SOLVER_DIMENSIONS = ['complexity', 'creativity', 'depth', 'coverage'] as const
export const DEFAULT_EFFORT_SOLVER_WEIGHTS: Readonly<EffortSolverWeights> = {
  complexity: 2, creativity: 1.5, depth: 3, coverage: 2,
}

export function validEffortSolverWeights(weights: EffortSolverWeights): boolean {
  const values = EFFORT_SOLVER_DIMENSIONS.map(key => weights[key])
  const total = values.reduce((sum, value) => sum + value, 0)
  return values.every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)
    && Number.isFinite(total) && total > 0
}

/** Only the validated scores and result enter the Producer dialogue (§10.11). */
export function effortSolverResult(data: Record<string, unknown>): {
  scores: EffortSolverWeights; effort: 1 | 2 | 3 | 4 | 5
} | null {
  if (!data.scores || typeof data.scores !== 'object') return null
  const scores = data.scores as EffortSolverWeights
  if (!EFFORT_SOLVER_DIMENSIONS.every(key => Number.isInteger(scores[key]) && scores[key] >= 1 && scores[key] <= 10)) return null
  if (typeof data.effort !== 'number' || !Number.isInteger(data.effort) || data.effort < 1 || data.effort > 5) return null
  return { scores, effort: data.effort as 1 | 2 | 3 | 4 | 5 }
}
