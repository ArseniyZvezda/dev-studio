/**
 * The bound on what gets highlighted (spec §6.2).
 *
 * A game project is full of generated text — baked lightmap dumps, cook logs, exported
 * meshes as ASCII. Tokenizing one of those locks the tab for as long as it takes, so the
 * pane declines and says so. Only the decision is testable without a browser, which is
 * exactly the part that must not drift.
 */

import { describe, expect, it } from 'vitest'

import {
  lineCount,
  MAX_HIGHLIGHT_CHARS,
  MAX_HIGHLIGHT_LINES,
  oversizeReason,
} from './use-highlight'

describe('lineCount', () => {
  it('counts the lines a file has, not the newlines in it', () => {
    expect(lineCount('')).toBe(1)
    expect(lineCount('one')).toBe(1)
    expect(lineCount('one\ntwo')).toBe(2)
    // A trailing newline does not open a line anyone can see, but it does end one.
    expect(lineCount('one\ntwo\n')).toBe(3)
  })

  it('stops counting once the answer cannot change the decision', () => {
    const text = 'x\n'.repeat(50_000)
    expect(lineCount(text, 10)).toBe(11)
  })
})

describe('oversizeReason', () => {
  it('highlights an ordinary file', () => {
    expect(oversizeReason('const x = 1\n')).toBeNull()
  })

  it('declines a file that is too many bytes', () => {
    expect(oversizeReason('x'.repeat(MAX_HIGHLIGHT_CHARS + 1))).toBe('size')
    expect(oversizeReason('x'.repeat(MAX_HIGHLIGHT_CHARS))).toBeNull()
  })

  it('declines a file that is too many lines', () => {
    // Short lines: well under the byte limit and still not worth 5000 rows of tokens.
    expect(oversizeReason('x\n'.repeat(MAX_HIGHLIGHT_LINES))).toBe('lines')
    expect(oversizeReason('x\n'.repeat(MAX_HIGHLIGHT_LINES - 2))).toBeNull()
  })

  it('reports the byte limit first, so the note names the reason that bit', () => {
    expect(oversizeReason('x'.repeat(MAX_HIGHLIGHT_CHARS + 1) + '\n'.repeat(9999))).toBe('size')
  })
})
