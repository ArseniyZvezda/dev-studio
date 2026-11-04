import { describe, expect, it } from 'vitest'

import { RESULT_TYPES, isResultType, resultTypeColor, resultTypeOf } from './task-result'

describe('task result types', () => {
  it('knows exactly the three the backend accepts', () => {
    expect(RESULT_TYPES).toEqual(['done', 'question', 'error'])
    for (const type of RESULT_TYPES) expect(isResultType(type)).toBe(true)
    expect(isResultType('maybe')).toBe(false)
    expect(isResultType(null)).toBe(false)
  })

  it('draws them green, blue and red through the semantic tokens', () => {
    expect(resultTypeColor('done')).toBe('var(--vxs-success)')
    expect(resultTypeColor('question')).toBe('var(--vxs-info)')
    expect(resultTypeColor('error')).toBe('var(--vxs-danger)')
  })

  it('reads the type off a summary entry and nothing else', () => {
    expect(resultTypeOf({ kind: 'summary', data: { result_type: 'question' } })).toBe('question')
    expect(resultTypeOf({ kind: 'summary', data: {} })).toBeNull()
    expect(resultTypeOf({ kind: 'summary', data: { result_type: 'maybe' } })).toBeNull()
    expect(resultTypeOf({ kind: 'text', data: { result_type: 'done' } })).toBeNull()
  })
})
