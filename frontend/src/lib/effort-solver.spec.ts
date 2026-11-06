import { describe, expect, it } from 'vitest'

import { DEFAULT_EFFORT_SOLVER_WEIGHTS, effortSolverResult, validEffortSolverWeights } from './effort-solver'

describe('Effort Solver input and result validation', () => {
  it('accepts default weights and zeroing individual criteria', () => {
    expect(validEffortSolverWeights(DEFAULT_EFFORT_SOLVER_WEIGHTS)).toBe(true)
    expect(validEffortSolverWeights({ complexity: 0, creativity: 0, depth: 0, coverage: 1 })).toBe(true)
  })

  it.each([-1, NaN, Infinity, ''])('refuses invalid weight %s', depth => {
    expect(validEffortSolverWeights({ ...DEFAULT_EFFORT_SOLVER_WEIGHTS, depth: depth as number })).toBe(false)
  })

  it('refuses a zero total', () => {
    expect(validEffortSolverWeights({ complexity: 0, creativity: 0, depth: 0, coverage: 0 })).toBe(false)
  })

  it.each([0, 11, 1.5, '8', true])('does not interpret invalid score %s as a valid result', depth => {
    expect(effortSolverResult({ scores: { complexity: 9, creativity: 6, depth, coverage: 8 }, effort: 4 })).toBeNull()
  })
})
