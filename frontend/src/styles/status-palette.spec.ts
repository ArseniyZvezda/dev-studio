import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('review status palette', () => {
  it('uses semantic blue in default and accessible light/dark palettes', () => {
    const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8')
    const reviewColors = [...css.matchAll(/--vxs-status-review:\s*([^;]+);/g)].map(match => match[1])
    expect(reviewColors).toEqual(Array(3).fill('var(--vxs-info)'))
  })
})
