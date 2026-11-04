/**
 * Selecting whole blocks (block-editor spec §4.7), against the editor's real schema.
 *
 * Two of these are regressions for defects that cost a document. `Ctrl+A` used to hand the
 * key to ProseMirror's own `selectAll` — the whole file as *text* — and a block selection
 * used to leave that text selection where it was, so the next keystroke replaced everything
 * it covered instead of the blocks the user could see selected. §8.3 calls losing content a
 * critical bug, and both are one step apart from an ordinary "select all, type over it".
 *
 * The view is not involved: what is asserted is the state these produce, which is what the
 * keymap dispatches and what any gesture acting on `state.selection` will meet.
 */

import { getSchema } from '@tiptap/core'
import { Node as PmDoc } from '@tiptap/pm/model'
import { EditorState, TextSelection } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'

import { parseDocument } from '@/lib/md/document'
import { mdastToProseMirror } from '@/lib/md/prosemirror'

import { collectRows, rangeOf, rowAt } from './blocks'
import { selectAllStep, setBlockSelection } from './block-selection'
import { createExtensions } from './extensions'
import { createSlashState } from './slash-menu'

const schema = getSchema(createExtensions({ slash: createSlashState() }))

function stateOf(markdown: string, caretIn?: string): EditorState {
  const doc = PmDoc.fromJSON(schema, mdastToProseMirror(parseDocument(markdown).root) as never)
  const state = EditorState.create({ doc })
  if (caretIn === undefined) return state

  // Put the caret inside the row whose text starts with `caretIn`, the way a click would.
  const row = collectRows(doc).find((entry) => entry.node.textContent.startsWith(caretIn))
  if (!row) throw new Error(`No row starting with ${caretIn}`)
  const inside = TextSelection.near(doc.resolve(row.pos), 1)
  return state.apply(state.tr.setSelection(inside))
}

describe('Ctrl+A steps out one level at a time (§4.7)', () => {
  it('takes the block’s own text first', () => {
    const state = stateOf('one\n\ntwo\n\nthree\n', 'two')
    const step = selectAllStep(state, null)

    expect(step?.kind).toBe('text')
    expect(state.doc.textBetween(step!.range.from, step!.range.to)).toBe('two')
  })

  it('takes every block once the text is already covered', () => {
    const state = stateOf('one\n\ntwo\n\nthree\n', 'two')
    const first = selectAllStep(state, null)!
    const covered = state.apply(
      state.tr.setSelection(TextSelection.create(state.doc, first.range.from, first.range.to)),
    )

    const second = selectAllStep(covered, null)
    expect(second?.kind).toBe('blocks')
    // The whole document, not the block it started in — the step that used to be missing.
    expect(second!.range).toEqual({ from: 0, to: covered.doc.content.size })
  })

  it('takes every block when one is already selected', () => {
    const state = stateOf('one\n\ntwo\n', 'one')
    const row = collectRows(state.doc)[0]
    const step = selectAllStep(state, rangeOf(row))

    expect(step?.kind).toBe('blocks')
    expect(step!.range.to).toBe(state.doc.content.size)
  })

  it('finds a list item’s text, which does not begin where the item does', () => {
    // `pos + 1` is inside the item and before its paragraph. Guessing it left the "is the
    // text already covered" test permanently false, so the second press never came.
    const state = stateOf('- alpha\n- beta\n', 'beta')
    const step = selectAllStep(state, null)

    expect(step?.kind).toBe('text')
    expect(state.doc.textBetween(step!.range.from, step!.range.to)).toBe('beta')

    const covered = state.apply(
      state.tr.setSelection(TextSelection.create(state.doc, step!.range.from, step!.range.to)),
    )
    expect(selectAllStep(covered, null)?.kind).toBe('blocks')
  })
})

describe('the text selection follows the block selection', () => {
  it('is moved inside the selected blocks', () => {
    // Otherwise a paste, an IME commit or a browser autocorrect acts on a selection the user
    // cannot see — the blocks look selected and four other paragraphs are replaced.
    const state = stateOf('one\n\ntwo\n\nthree\n', 'one')
    const wide = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 1, 12)))
    expect(wide.selection.to - wide.selection.from).toBeGreaterThan(4)

    const target = rangeOf(rowAt(wide.doc, 6)!)
    const selected = wide.apply(setBlockSelection(wide.tr, { range: target }))

    expect(selected.selection.empty).toBe(true)
    expect(selected.selection.from).toBeGreaterThanOrEqual(target.from)
    expect(selected.selection.to).toBeLessThanOrEqual(target.to)
  })

  it('is left alone when the selection is being cleared', () => {
    const state = stateOf('one\n\ntwo\n', 'two')
    const before = state.selection.from
    const cleared = state.apply(setBlockSelection(state.tr, { range: null, anchor: null }))
    expect(cleared.selection.from).toBe(before)
  })
})
