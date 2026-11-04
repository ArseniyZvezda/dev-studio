/**
 * What a stored pane width is allowed to be.
 *
 * The value comes out of `localStorage`, which is to say out of whatever the last build,
 * the last window size or a curious user left there — so the contract is stated as
 * refusals, and every one of them ends in a width the pane can be rendered at.
 */
import { describe, expect, it } from 'vitest'

import { MIN_REMAINDER, clampPaneWidth, paneWidth } from '@/lib/pane-width'

describe('paneWidth', () => {
  it('falls back when nobody has sized the pane yet', () => {
    expect(paneWidth(null, 288, 200, 560)).toBe(288)
  })

  it('falls back on a value that is not a width', () => {
    // The corrupt case is the one that matters: `Number('wide')` is NaN and a NaN width
    // renders as a pane 0px wide, which is a pane nobody can drag back.
    expect(paneWidth('wide', 256, 200, 480)).toBe(256)
    expect(paneWidth('', 256, 200, 480)).toBe(256)
  })

  it('clamps a stored width into the range this pane allows', () => {
    // 900 is a width from a wider window, or from before a max came down.
    expect(paneWidth('900', 256, 200, 480)).toBe(480)
    expect(paneWidth('40', 256, 200, 480)).toBe(200)
  })

  it('returns a width inside the range unchanged', () => {
    expect(paneWidth('288', 288, 200, 560)).toBe(288)
    expect(paneWidth('331', 288, 200, 560)).toBe(331)
  })

  it('does not apply the viewport ceiling — that is not its job', () => {
    // A width the current window cannot take is still the width the user chose, and a wider
    // window later has to give it back. `clampPaneWidth` is what decides what fits *now*.
    expect(paneWidth('560', 288, 200, 560)).toBe(560)
  })
})

describe('clampPaneWidth', () => {
  it('leaves a width the window has room for alone', () => {
    expect(clampPaneWidth(288, 200, 560, 2560)).toBe(288)
  })

  it('keeps the centre at least MIN_REMAINDER wide', () => {
    // The case the persistence made reachable: 560px chosen on a maximised monitor, restored
    // in an 800px window. Unclamped it leaves the centre ~40px and the seam out of reach.
    expect(clampPaneWidth(560, 200, 560, 800)).toBe(800 - MIN_REMAINDER)
  })

  it('never goes below the minimum this pane allows, however narrow the window', () => {
    // A pane nobody can see is worse than a centre that has to scroll: `min` wins last.
    expect(clampPaneWidth(560, 200, 560, 300)).toBe(200)
  })

  it('applies the maximum this pane allows as well', () => {
    expect(clampPaneWidth(900, 200, 560, 2560)).toBe(560)
  })
})
