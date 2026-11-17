import { describe, expect, it } from 'vitest'

import {
  effortSolverResult, spawnEffortSolver,
} from './effort-solver'

describe('Orchestrator input and result validation', () => {
  const scores = { complexity: 9, creativity: 6, depth: 8, coverage: 8 }
  it.each([0, 0.6, 0.8, 1])('preserves normalized weight %s alongside the selected effort', weight => {
    expect(effortSolverResult({ scores, weight, effort: 3 })).toEqual({ scores, weight, effort: 3 })
  })
  it.each([-0.1, 1.1, NaN, Infinity, '0.6', true, null])('rejects invalid weight %s', weight => {
    expect(effortSolverResult({ scores, weight, effort: 3 })).toBeNull()
  })
  it.each([0, 11, 1.5, '8', true])('does not interpret invalid score %s as a valid result', depth => {
    expect(effortSolverResult({ scores: { complexity: 9, creativity: 6, depth, coverage: 8 }, effort: 4 })).toBeNull()
  })
})

describe('a spawn_subagent result (§10.3)', () => {
  const scores = { complexity: 2, creativity: 2, depth: 3, coverage: 3 }

  it('reads the evaluation the spawn answered with beside the subagent row', () => {
    const text = JSON.stringify({ id: 'subagent_1', effort: 'medium',
      effort_solver: { scores, effort: 2, effort_level: 'medium' } })
    expect(spawnEffortSolver(text)).toEqual({ scores, effort: 2 })
  })

  it.each([
    ['nothing', undefined],
    ['an error', 'ValueError: Backend codex is unavailable'],
    ['a clipped answer', JSON.stringify({ effort_solver: { scores, effort: 2 } }).slice(0, 40)],
    ['a row from before the scores', JSON.stringify({ id: 'subagent_1', effort: 'medium' })],
    ['invalid scores', JSON.stringify({ effort_solver: { scores: { ...scores, depth: 11 }, effort: 2 } })],
    ['a list', '[]'],
  ])('draws no evaluation from %s', (_case, text) => {
    expect(spawnEffortSolver(text)).toBeNull()
  })
})


describe('Orchestrator capability and effort evaluation', () => {
  it.each([1, 2, 3])('reads model strength %s independently from effort', model => {
    expect(effortSolverResult({ model, effort: 4, effort_level: 'xhigh', reasoning: 'hidden' }))
      .toEqual({ model, effort: 4, effort_level: 'xhigh' })
  })
  it.each([0, 4, 1.5, '2', true, null])('rejects malformed model strength %s', model => {
    expect(effortSolverResult({ model, effort: 3 })).toBeNull()
  })
  it.each([0, 6, 2.5, '3', true])('rejects malformed effort %s', effort => {
    expect(effortSolverResult({ model: 2, effort })).toBeNull()
  })
  it('does not trust inconsistent effort labels or reinterpret new records as legacy ones', () => {
    expect(effortSolverResult({ model: 2, effort: 4, effort_level: 'low' })).toBeNull()
    expect(effortSolverResult({ model: 4, scores: { complexity: 2, creativity: 2, depth: 2, coverage: 2 }, effort: 3 })).toBeNull()
  })
  it('reads new evaluation data under the compatible effort_solver result key', () => {
    expect(spawnEffortSolver(JSON.stringify({ id: 'child', effort_solver: { model: 3, effort: 1, effort_level: 'low' } })))
      .toEqual({ model: 3, effort: 1, effort_level: 'low' })
  })
})
