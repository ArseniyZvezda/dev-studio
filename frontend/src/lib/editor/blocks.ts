/**
 * What counts as a "block" to drag, select and move (block-editor spec §4.1, §4.2, §4.7).
 *
 * ProseMirror has nodes; the user has *rows* — the things a gutter appears beside and a
 * drag handle picks up. The two are not the same, and the difference is this module:
 *
 * - A **list is not a row, its items are.** Nobody reaches for a handle to move "the list";
 *   they move a bullet, and the list is where bullets live.
 * - A **list item's own text is the item**, so the paragraph inside it is not a row of its
 *   own. Its *other* children — a nested list, a second paragraph, a code block — are.
 * - A **callout or toggle is a row and so is everything inside it**, because that is the
 *   point of a container you can drop blocks into. Its title is not: it belongs to the
 *   container the way a list item's first paragraph belongs to the item.
 *
 * Positions here are always the position *before* a node, which is what ProseMirror's
 * `resolve`, `nodeAt` and `insert` all agree on.
 */

import type { Node } from '@tiptap/pm/model'
import type { EditorState, Transaction } from '@tiptap/pm/state'

export interface BlockRef {
  /** Position before the node. */
  pos: number
  node: Node
  /** `pos + node.nodeSize` — the position after it. */
  end: number
  /** ProseMirror depth of the node, for indentation and ancestry questions. */
  depth: number
}

/** A run of sibling rows, `from` before the first and `to` after the last. */
export interface BlockRange {
  from: number
  to: number
}

const LIST_WRAPPERS = new Set(['bulletList', 'orderedList', 'taskList'])
const LIST_ITEMS = new Set(['listItem', 'taskItem'])
const CONTAINERS = new Set(['blockquote', 'callout', 'toggle'])

/** Nodes that can take a dropped block, which is what makes nesting by cursor x possible. */
export function acceptsBlocks(node: Node): boolean {
  return CONTAINERS.has(node.type.name) || LIST_ITEMS.has(node.type.name)
}

function collect(parent: Node, contentStart: number, depth: number, out: BlockRef[]): void {
  parent.forEach((child, offset, index) => {
    const pos = contentStart + offset
    const name = child.type.name

    // The wrapper is scaffolding; its items are the rows.
    if (LIST_WRAPPERS.has(name)) {
      collect(child, pos + 1, depth + 1, out)
      return
    }
    // A directive's title belongs to the directive, the way an item's first line belongs
    // to the item. Neither is a row you can pull out on its own.
    if (name === 'directiveTitle') return
    // Inside a list item, the first paragraph *is* the item.
    if (index === 0 && name === 'paragraph' && LIST_ITEMS.has(parent.type.name)) return

    out.push({ pos, node: child, end: pos + child.nodeSize, depth })

    if (LIST_ITEMS.has(name) || CONTAINERS.has(name)) collect(child, pos + 1, depth + 1, out)
  })
}

/**
 * Every row in the document, in document order.
 *
 * Memoised on the document, and that is a §9 requirement rather than tidiness: this walks
 * the whole tree, and the gutter calls it on **every pointer move** while a drag calls it
 * again for each candidate drop position. On a 3000-line document that is the difference
 * between a smooth drag and a stuttering one.
 *
 * A `WeakMap` is exactly right here because ProseMirror documents are immutable — every
 * transaction produces a new one, so the entry cannot go stale, and the old document is
 * collectable the moment nothing else holds it.
 */
const rowCache = new WeakMap<Node, BlockRef[]>()

export function collectRows(doc: Node): BlockRef[] {
  const cached = rowCache.get(doc)
  if (cached) return cached

  const out: BlockRef[] = []
  collect(doc, 0, 0, out)
  rowCache.set(doc, out)
  return out
}

/**
 * The row containing a position — the thing a handle beside that line would pick up.
 *
 * The innermost match wins: a paragraph inside a callout is its own row, and rows come out
 * of `collectRows` outermost first, so the last one containing the position is the answer.
 */
export function rowAt(doc: Node, pos: number): BlockRef | null {
  let found: BlockRef | null = null
  for (const row of collectRows(doc)) {
    if (row.pos > pos) break
    if (pos < row.end) found = row
  }
  return found
}

/** The row a ProseMirror selection sits in. */
export function rowAtSelection(state: EditorState): BlockRef | null {
  return rowAt(state.doc, state.selection.from)
}

/** Rows fully covered by a range, in document order. */
export function rowsIn(doc: Node, range: BlockRange): BlockRef[] {
  return collectRows(doc).filter((row) => row.pos >= range.from && row.end <= range.to)
}

/**
 * Widen two rows into the sibling run that covers both.
 *
 * A range has to be siblings under one parent, or moving it is not one operation. When the
 * two ends live at different depths, both are lifted until they meet — selecting from a
 * nested bullet to the paragraph after the list selects the whole list, which is what the
 * user pointed at even if it is not what they clicked.
 */
export function rangeBetween(doc: Node, one: BlockRef, other: BlockRef): BlockRange {
  const [a, b] = one.pos <= other.pos ? [one, other] : [other, one]
  const $a = doc.resolve(a.pos)
  const shared = $a.sharedDepth(b.pos)

  return {
    from: a.depth === shared ? a.pos : $a.before(shared + 1),
    to: b.depth === shared ? b.end : doc.resolve(b.pos).after(shared + 1),
  }
}

/** The range covering exactly one row. */
export function rangeOf(row: BlockRef): BlockRange {
  return { from: row.pos, to: row.end }
}

// ------------------------------------------------------------------ moves

/**
 * Where a block may be inserted at one vertical gap, deepest first.
 *
 * This list is what turns horizontal cursor position into nesting (§4.2). After a row, a
 * block can go *into* it when it accepts blocks, *after* it as a sibling, or after any
 * ancestor the row happens to end — and only ancestors it ends, because inserting after an
 * ancestor the row is in the middle of is a different place on screen entirely.
 */
export interface Insertion {
  pos: number
  depth: number
  /**
   * A node whose left edge the dropped block would line up with, so the caller can measure
   * the indent from the DOM instead of guessing at a pixel constant.
   */
  reference: number
  /** True for the one insertion that goes *inside* the reference rather than beside it. */
  inside: boolean
}

export function insertionsAfter(doc: Node, row: BlockRef): Insertion[] {
  const out: Insertion[] = []

  if (acceptsBlocks(row.node)) {
    out.push({ pos: row.end - 1, depth: row.depth + 1, reference: row.pos, inside: true })
  }
  out.push({ pos: row.end, depth: row.depth, reference: row.pos, inside: false })

  // Outwards, one ancestor at a time, for as long as the row is the *last* thing in it.
  // Stop as soon as it is not: inserting after an ancestor the row sits in the middle of
  // would put the block somewhere else on screen entirely.
  const $end = doc.resolve(row.end)
  let edge = row.end
  for (let depth = $end.depth; depth >= 1; depth -= 1) {
    if ($end.end(depth) !== edge) break
    const reference = $end.before(depth)
    edge = $end.after(depth)
    out.push({ pos: edge, depth: depth - 1, reference, inside: false })
  }
  return out
}

/** Insertion points above the very first row — the only gap with nothing before it. */
export function insertionsBefore(row: BlockRef): Insertion[] {
  return [{ pos: row.pos, depth: row.depth, reference: row.pos, inside: false }]
}

/**
 * Move a range to a position, as one transaction.
 *
 * Deleting first and mapping the target through that deletion is what keeps a move
 * undoable in one step (§7) — and mapping rather than arithmetic is what makes a move
 * *upwards* land where the indicator said it would.
 */
export function moveRange(tr: Transaction, range: BlockRange, target: number): Transaction | null {
  if (target > range.from && target < range.to) return null // into itself
  const slice = tr.doc.slice(range.from, range.to)
  tr.delete(range.from, range.to)
  const at = tr.mapping.map(target, -1)
  tr.insert(at, slice.content)
  return tr
}
