import type { EffortSolverScores } from '@/api/types'

export const EFFORT_SOLVER_DIMENSIONS = ['complexity', 'creativity', 'depth', 'coverage'] as const

/** Only the validated scores and result enter the Producer dialogue (§10.11). */
export function effortSolverResult(data: Record<string, unknown>): {
  scores?: EffortSolverScores; model?: 1 | 2 | 3; effort_level?: string; effort: 1 | 2 | 3 | 4 | 5; weight?: number
} | null {
  if (typeof data.effort !== 'number' || !Number.isInteger(data.effort) || data.effort < 1 || data.effort > 5) return null
  // §10.11: new evaluations select capability and reasoning independently.
  if (data.model !== undefined) {
    if (typeof data.model !== 'number' || !Number.isInteger(data.model) || data.model < 1 || data.model > 3) return null
    const levels = ['low', 'medium', 'high', 'xhigh', 'max']
    if (data.effort_level !== undefined && data.effort_level !== levels[data.effort - 1]) return null
    return { model: data.model as 1 | 2 | 3, effort: data.effort as 1 | 2 | 3 | 4 | 5,
      ...(typeof data.effort_level === 'string' ? { effort_level: data.effort_level } : {}) }
  }
  // Keep historical four-score records without inventing a model strength.
  if (!data.scores || typeof data.scores !== 'object') return null
  const scores = data.scores as EffortSolverScores
  if (!EFFORT_SOLVER_DIMENSIONS.every(key => Number.isInteger(scores[key]) && scores[key] >= 1 && scores[key] <= 10)) return null
  if (typeof data.effort !== 'number' || !Number.isInteger(data.effort) || data.effort < 1 || data.effort > 5) return null
  // Historical transcript entries have no normalized weight; do not reinterpret 1–5 as 0–1.
  if (data.weight !== undefined && (typeof data.weight !== 'number' || !Number.isFinite(data.weight) || data.weight < 0 || data.weight > 1)) return null
  return { scores, effort: data.effort as 1 | 2 | 3 | 4 | 5,
    ...(typeof data.weight === 'number' ? { weight: data.weight } : {}) }
}

/**
 * The evaluation `spawn_subagent` answered with (§10.3): its result is the subagent row as
 * JSON with `effort_solver` beside it. A result that is an error, clipped, or written before
 * the scores were returned has none, and the call row then draws no evaluation.
 */
export function spawnEffortSolver(resultText: string | undefined): ReturnType<typeof effortSolverResult> {
  if (!resultText) return null
  try {
    const parsed: unknown = JSON.parse(resultText)
    if (!parsed || typeof parsed !== 'object') return null
    const solver = (parsed as Record<string, unknown>).effort_solver
    return solver && typeof solver === 'object' ? effortSolverResult(solver as Record<string, unknown>) : null
  } catch {
    return null
  }
}
