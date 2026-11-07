import { describe, expect, it } from 'vitest'
import type { ContextSource } from '@/api/types'
import { contextMap, retainedFragment } from './context-map'

describe('context size map', () => {
  it('preserves token area, covers the canvas and never overlaps, including tiny sources', () => {
    const input = [7000, 2000, 720, 120, 31, 3, 1].map((tokens, id) => ({ id: String(id), tokens }))
    const blocks = contextMap(input)
    const total = input.reduce((sum, item) => sum + item.tokens, 0)
    expect(blocks).toHaveLength(input.length)
    expect(blocks.reduce((sum, block) => sum + block.width * block.height, 0)).toBeCloseTo(6200)
    for (const block of blocks) {
      expect(block.width * block.height / 6200).toBeCloseTo(block.tokens / total, 10)
      expect(block.x + block.width).toBeLessThanOrEqual(100.000001)
      expect(block.y + block.height).toBeLessThanOrEqual(62.000001)
      for (const other of blocks.filter(other => other.id !== block.id)) {
        const overlapX = Math.min(block.x + block.width, other.x + other.width) - Math.max(block.x, other.x)
        const overlapY = Math.min(block.y + block.height, other.y + other.height) - Math.max(block.y, other.y)
        expect(Math.min(overlapX, overlapY)).toBeLessThanOrEqual(0.000001)
      }
    }
  })
  it('handles missing and zero counts without fake blocks', () => {
    expect(contextMap([])).toEqual([])
    expect(contextMap([{ id: 'zero', tokens: 0 }, { id: 'nan', tokens: NaN }])).toEqual([])
    expect(contextMap([{ id: 'one', tokens: 1 }])).toEqual([{ id: 'one', tokens: 1, x: 0, y: 0, width: 100, height: 62 }])
  })
})

describe('the inspected fragment after a refresh (§10.4)', () => {
  const selected: ContextSource = { id: '3', category: 'history', origin: 'response_item[7].text',
    label: 'assistant', text: 'abc', tokens: 1, truncated: false }

  it('follows a surviving record to its new position and clears the choice when compaction replaces it', () => {
    const replacement = { ...selected, origin: 'compacted[9].replacement_history[3].text' }
    const survivor = { ...selected, id: '4' }
    expect(retainedFragment(selected, [replacement, survivor])).toBe(survivor)
    expect(retainedFragment(selected, [replacement])).toBeUndefined()
  })

  it('requires the same position and full text or key for inventories without provenance', () => {
    const legacy = { ...selected, origin: undefined }
    expect(retainedFragment(legacy, [{ ...legacy }])).toEqual(legacy)
    expect(retainedFragment(legacy, [{ ...legacy, text: 'different' }])).toBeUndefined()
    expect(retainedFragment(legacy, [{ ...legacy, id: '4' }])).toBeUndefined()
    const partial = { ...legacy, truncated: true, text_key: 'first' }
    expect(retainedFragment(partial, [{ ...partial, text_key: 'second' }])).toBeUndefined()
    expect(retainedFragment(partial, [{ ...partial, text: '' }])?.text_key).toBe('first')
  })
})
