/**
 * Selecting whole blocks, and the operations that apply to a selection (§4.7, §4.8).
 *
 * ProseMirror has no selection type for "these four sibling blocks". `NodeSelection` holds
 * exactly one node and `CellSelection` is for tables, so the block selection lives in plugin
 * state as a sibling range and paints itself with decorations. That is deliberate rather
 * than a workaround: the text selection stays valid underneath, so `Ctrl+A` can step out of
 * the block and back, and nothing has to reimplement copy or delete for a custom class.
 *
 * The range is always **siblings under one parent** (see `rangeBetween`). A selection that
 * spans two depths is not one thing that can be dragged, duplicated or moved as a unit, and
 * pretending otherwise produces operations whose result nobody can predict.
 */

import { Extension } from '@tiptap/core'
import { Plugin, PluginKey, TextSelection, type EditorState, type Transaction } from '@tiptap/pm/state'
import { Decoration, DecorationSet, type EditorView } from '@tiptap/pm/view'

import {
  rangeBetween,
  rangeOf,
  rowAt,
  rowsIn,
  type BlockRange,
  type BlockRef,
} from './blocks'

export const blockSelectionKey = new PluginKey<BlockSelectionState>('voxarium_studioBlockSelection')

export interface BlockSelectionState {
  range: BlockRange | null
  /** Where a `Shift+click` range started, so extending it twice keeps one anchor. */
  anchor: number | null
  /** Blocks currently being dragged, shown dimmed (§4.2). */
  dragging: BlockRange | null
}

interface SetMeta {
  range?: BlockRange | null
  anchor?: number | null
  dragging?: BlockRange | null
}

export function getBlockSelection(state: EditorState): BlockRange | null {
  return blockSelectionKey.getState(state)?.range ?? null
}

export function setBlockSelection(tr: Transaction, meta: SetMeta): Transaction {
  tr.setMeta(blockSelectionKey, meta)
  // **The text selection is moved inside the blocks**, and that is a safety property rather
  // than tidiness. A block selection is drawn by a decoration; the real `state.selection` is
  // wherever it was, and every gesture that does not go through this module's keymap — a
  // paste, an IME commit, a browser autocorrect — acts on *that*. Left alone, blocks look
  // selected while a paste replaces four other paragraphs the user cannot see are selected.
  // §8.3 calls silently losing content a critical bug, so the two are kept from disagreeing.
  if (meta.range) {
    const inside = TextSelection.near(tr.doc.resolve(meta.range.from), 1)
    if (inside.from >= meta.range.from && inside.to <= meta.range.to) tr.setSelection(inside)
  }
  return tr
}

/**
 * Mark the blocks a drag has picked up.
 *
 * A decoration rather than a class on the DOM node, and that is not a style preference:
 * **ProseMirror strips attributes it did not write** when it next syncs a node, so a class
 * added by hand disappears on the very next update and the source block silently stops
 * looking dragged. Decorations are the only marking that survives.
 */
export function setDragRange(tr: Transaction, dragging: BlockRange | null): Transaction {
  return tr.setMeta(blockSelectionKey, { dragging })
}

function selectRange(view: EditorView, range: BlockRange | null, anchor?: number | null): boolean {
  view.dispatch(setBlockSelection(view.state.tr, { range, anchor }))
  return true
}

/**
 * The whole document as one range, for the second step of `Ctrl+A`.
 *
 * The document's own bounds rather than its top-level *rows*: a list is not a row (its items
 * are), so a document that is only a checklist has no row at depth 0 at all — and "select
 * every block" answered with nothing on exactly the documents made of one list.
 */
function wholeDocument(state: EditorState): BlockRange | null {
  return state.doc.content.size ? { from: 0, to: state.doc.content.size } : null
}

/**
 * The text a row holds, as positions.
 *
 * `between` searches inward for valid text positions rather than assuming `pos + 1`, which
 * is wrong for every row that is not a plain paragraph: a list item's text starts one depth
 * further in, and a callout's starts inside its first child.
 */
function textRangeOf(state: EditorState, row: BlockRef): BlockRange {
  const selection = TextSelection.between(
    state.doc.resolve(row.pos),
    state.doc.resolve(row.end),
  )
  return { from: selection.from, to: selection.to }
}

export type SelectAllStep =
  | { kind: 'text'; range: BlockRange }
  | { kind: 'blocks'; range: BlockRange }

/**
 * What `Ctrl+A` should do next (§4.7: "the block's text, then every block").
 *
 * Exactly two steps, and the second one is reached from *either* of the first's outcomes:
 * whether the row's text was selected by this key or by a sweep of the mouse, pressing it
 * again means "all of it". Kept as a pure function of the state because the interesting part
 * is the stepping, and asserting it through a live editor would say much less.
 *
 * Returning `null` leaves the key to ProseMirror's own `selectAll`, whose answer — the whole
 * document as *text* — is what this replaces: a first press that swallows the document is
 * the step nobody asked for, and typing over it used to take the file with it.
 */
export function selectAllStep(state: EditorState, current: BlockRange | null): SelectAllStep | null {
  if (current) {
    const all = wholeDocument(state)
    return all ? { kind: 'blocks', range: all } : null
  }
  const row = rowAt(state.doc, state.selection.from)
  if (!row) return null

  const text = textRangeOf(state, row)
  const covered = state.selection.from <= text.from && state.selection.to >= text.to
  if (!covered) return { kind: 'text', range: text }

  const all = wholeDocument(state)
  return all ? { kind: 'blocks', range: all } : null
}

// ------------------------------------------------------------------ operations

function deleteSelection(view: EditorView, range: BlockRange): boolean {
  const tr = view.state.tr.delete(range.from, range.to)
  setBlockSelection(tr, { range: null, anchor: null })
  // The caret has to land somewhere the user expects — the start of whatever moved up.
  const at = Math.min(tr.mapping.map(range.from), tr.doc.content.size)
  tr.setSelection(TextSelection.near(tr.doc.resolve(at)))
  view.dispatch(tr.scrollIntoView())
  return true
}

function duplicateSelection(view: EditorView, range: BlockRange): boolean {
  const slice = view.state.doc.slice(range.from, range.to)
  const tr = view.state.tr.insert(range.to, slice.content)
  setBlockSelection(tr, { range: { from: range.to, to: range.to + slice.content.size }, anchor: null })
  view.dispatch(tr.scrollIntoView())
  return true
}

/**
 * Move a range one row up or down among its siblings.
 *
 * Keyboard-only movement is not a convenience here — §10 requires every action in §4.5 to
 * be reachable without a mouse, and dragging is the one that has no other keyboard form.
 */
function moveSelection(view: EditorView, range: BlockRange, direction: -1 | 1): boolean {
  const { doc } = view.state
  const $from = doc.resolve(range.from)
  const parent = $from.parent
  const parentStart = $from.start()

  // Siblings of the range, so a move stays inside the block it belongs to.
  const siblings: { pos: number; end: number }[] = []
  parent.forEach((child, offset) => {
    const pos = parentStart + offset
    siblings.push({ pos, end: pos + child.nodeSize })
  })

  const first = siblings.findIndex((entry) => entry.pos === range.from)
  const last = siblings.findIndex((entry) => entry.end === range.to)
  if (first === -1 || last === -1) return false

  const target = direction === -1 ? siblings[first - 1] : siblings[last + 1]
  if (!target) return false

  const slice = doc.slice(range.from, range.to)
  const tr = view.state.tr.delete(range.from, range.to)
  const at = direction === -1 ? target.pos : tr.mapping.map(target.end, -1)
  tr.insert(at, slice.content)
  setBlockSelection(tr, { range: { from: at, to: at + slice.content.size }, anchor: null })
  view.dispatch(tr.scrollIntoView())
  return true
}

// ------------------------------------------------------------------ the extension

export const BlockSelection = Extension.create({
  name: 'blockSelection',

  addKeyboardShortcuts() {
    const range = () => getBlockSelection(this.editor.state)
    const view = () => this.editor.view

    return {
      /**
       * §4.7: `Esc` in text selects the block. `Esc` with a block already selected clears
       * it, which is what §10 means by "every menu closes and focus comes back".
       */
      Escape: () => {
        if (range()) {
          selectRange(view(), null, null)
          this.editor.commands.focus()
          return true
        }
        const row = rowAt(this.editor.state.doc, this.editor.state.selection.from)
        return row ? selectRange(view(), rangeOf(row), row.pos) : false
      },

      /** §4.7: stepwise — the block's text first, then every block in the document. */
      'Mod-a': () => {
        const state = this.editor.state
        const step = selectAllStep(state, range())
        if (!step) return false
        if (step.kind === 'blocks') return selectRange(view(), step.range, null)
        const tr = state.tr.setSelection(
          TextSelection.create(state.doc, step.range.from, step.range.to),
        )
        view().dispatch(tr)
        return true
      },

      'Mod-d': () => {
        const current = range() ?? rowRange(this.editor.state)
        return current ? duplicateSelection(view(), current) : false
      },

      'Mod-Shift-ArrowUp': () => {
        const current = range() ?? rowRange(this.editor.state)
        return current ? moveSelection(view(), current, -1) : false
      },

      'Mod-Shift-ArrowDown': () => {
        const current = range() ?? rowRange(this.editor.state)
        return current ? moveSelection(view(), current, 1) : false
      },

      Backspace: () => {
        const current = range()
        return current ? deleteSelection(view(), current) : false
      },

      Delete: () => {
        const current = range()
        return current ? deleteSelection(view(), current) : false
      },
    }
  },

  addProseMirrorPlugins() {
    return [
      new Plugin<BlockSelectionState>({
        key: blockSelectionKey,

        state: {
          init: () => ({ range: null, anchor: null, dragging: null }),
          apply(tr, value) {
            const meta = tr.getMeta(blockSelectionKey) as SetMeta | undefined
            if (meta) {
              return {
                range: 'range' in meta ? (meta.range ?? null) : value.range,
                anchor: 'anchor' in meta ? (meta.anchor ?? null) : value.anchor,
                dragging: 'dragging' in meta ? (meta.dragging ?? null) : value.dragging,
              }
            }
            if (!value.range) return value
            if (!tr.docChanged) {
              // Typing is impossible while blocks are selected, but clicking is not: any
              // new text selection outside the range drops it.
              return tr.selectionSet && !within(tr.selection.from, value.range)
                ? { ...value, range: null, anchor: null }
                : value
            }
            const from = tr.mapping.map(value.range.from, -1)
            const to = tr.mapping.map(value.range.to)
            return to > from
              ? { ...value, range: { from, to } }
              : { ...value, range: null, anchor: null }
          },
        },

        props: {
          decorations(state) {
            const plugin = this.getState(state)
            if (!plugin?.range && !plugin?.dragging) return DecorationSet.empty
            const decorations: Decoration[] = []
            if (plugin.range) {
              for (const row of rowsIn(state.doc, plugin.range)) {
                decorations.push(Decoration.node(row.pos, row.end, { class: 'block-selected' }))
              }
            }
            if (plugin.dragging) {
              for (const row of rowsIn(state.doc, plugin.dragging)) {
                decorations.push(Decoration.node(row.pos, row.end, { class: 'block-dragging' }))
              }
            }
            return DecorationSet.create(state.doc, decorations)
          },

          /** §4.7: `Shift+click` extends from the anchor to the block that was clicked. */
          handleClick(view, pos, event) {
            const plugin = blockSelectionKey.getState(view.state)
            if (!event.shiftKey) {
              if (plugin?.range) selectRange(view, null, null)
              return false
            }
            const target = rowAt(view.state.doc, pos)
            if (!target) return false
            const anchorRow = plugin?.anchor === null || plugin?.anchor === undefined
              ? rowAt(view.state.doc, view.state.selection.from)
              : rowAt(view.state.doc, plugin.anchor)
            if (!anchorRow) return false
            event.preventDefault()
            return selectRange(view, rangeBetween(view.state.doc, anchorRow, target), anchorRow.pos)
          },

          /** Nothing types into a block selection; a printable key would silently drop it. */
          handleKeyDown(view, event) {
            const range = blockSelectionKey.getState(view.state)?.range
            if (!range) return false
            if (event.key.length !== 1 || event.ctrlKey || event.metaKey || event.altKey) return false
            // Replace the blocks with what was typed — the one editing gesture that makes
            // sense on a block selection, and what every editor of this shape does.
            //
            // **The caret has to be put where the blocks were.** The text selection under a
            // block selection is whatever it was before, and it can reach outside the range:
            // then the key is let through, the browser inserts into *that* selection, and
            // everything it covers goes with it. That is how `Ctrl+A` twice and one letter
            // used to empty a document — content loss, which §8.3 calls a critical bug.
            const tr = view.state.tr.delete(range.from, range.to)
            setBlockSelection(tr, { range: null, anchor: null })
            const at = Math.min(tr.mapping.map(range.from), tr.doc.content.size)
            tr.setSelection(TextSelection.near(tr.doc.resolve(at)))
            view.dispatch(tr)
            return false
          },
        },
      }),
    ]
  },
})

function within(pos: number, range: BlockRange): boolean {
  return pos >= range.from && pos <= range.to
}

/** The row the caret is in, as a range — what a shortcut acts on with nothing selected. */
function rowRange(state: EditorState): BlockRange | null {
  const row: BlockRef | null = rowAt(state.doc, state.selection.from)
  return row ? rangeOf(row) : null
}
