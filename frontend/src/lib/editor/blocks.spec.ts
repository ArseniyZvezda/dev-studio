/**
 * What the gutter picks up and where a drop can land (block-editor spec §4.1, §4.2, §4.7).
 *
 * Against the editor's real schema and real documents: a test schema would let the row rules
 * drift from the nodes they describe, and these rules are only interesting because they
 * disagree with ProseMirror's node tree in specific, deliberate ways.
 *
 * Results are asserted as Markdown wherever a whole document is involved. A move is correct
 * when the file it produces is the one the user was shown a line for, and comparing trees
 * says much less than comparing the four lines that came out.
 */

import { getSchema } from '@tiptap/core'
import { Node as PmDoc } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'

import { parseDocument, serializeDocument } from '@/lib/md/document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'

import {
  collectRows,
  insertionsAfter,
  moveRange,
  rangeBetween,
  rangeOf,
  rowAt,
  rowsIn,
} from './blocks'
import { createExtensions } from './extensions'
import { createSlashState } from './slash-menu'

// The slash menu needs somewhere to publish its state; a schema does not care what.
const schema = getSchema(createExtensions({ slash: createSlashState() }))

function docOf(markdown: string): PmDoc {
  return PmDoc.fromJSON(schema, mdastToProseMirror(parseDocument(markdown).root) as never)
}

function markdownOf(doc: PmDoc): string {
  return serializeDocument(proseMirrorToMdast(doc.toJSON() as PmNode), {
    frontmatter: '',
    eol: '\n',
    finalNewline: true,
  })
}

/** Rows as `type@depth`, which is the whole of what the gutter needs to know. */
function shapeOf(markdown: string): string[] {
  const doc = docOf(markdown)
  return collectRows(doc).map((row) => `${row.node.type.name}@${row.depth}`)
}

describe('what counts as a row', () => {
  it('takes top-level blocks', () => {
    expect(shapeOf('# Title\n\nText.\n\n---\n')).toEqual([
      'heading@0',
      'paragraph@0',
      'horizontalRule@0',
    ])
  })

  it('takes list items, not the list', () => {
    // Nobody reaches for a handle to move "the list"; they move a bullet.
    expect(shapeOf('- one\n- two\n')).toEqual(['listItem@1', 'listItem@1'])
  })

  it('does not take a list item’s own paragraph', () => {
    // That paragraph *is* the item — two handles on one line would be a lie about what
    // each of them moves.
    expect(shapeOf('- one\n')).toEqual(['listItem@1'])
  })

  it('takes an item’s second block, and nested items', () => {
    const rows = shapeOf('- one\n\n  ```\n  code\n  ```\n\n- two\n\n  - nested\n')
    expect(rows).toEqual(['listItem@1', 'codeBlock@2', 'listItem@1', 'listItem@3'])
  })

  it('takes a callout and everything inside it, but not its title', () => {
    expect(shapeOf(':::note[Title]\nBody.\n\n- item\n:::\n')).toEqual([
      'callout@0',
      'paragraph@1',
      'listItem@2',
    ])
  })

  it('takes a toggle the same way', () => {
    expect(shapeOf(':::toggle[T]\nBody.\n:::\n')).toEqual(['toggle@0', 'paragraph@1'])
  })

  it('takes a table as one row', () => {
    // Cells are not rows: a table is moved whole, and its shape is edited another way (§4.5).
    expect(shapeOf('| a |\n| --- |\n| 1 |\n')).toEqual(['table@0'])
  })

  it('takes an unsupported block, so §3.4 can still move and delete it', () => {
    expect(shapeOf('<div>raw</div>\n')).toEqual(['unsupportedBlock@0'])
  })
})

describe('finding the row at a position', () => {
  it('maps a caret inside a list item’s text to the item', () => {
    const doc = docOf('- one\n- two\n')
    const items = collectRows(doc)
    expect(rowAt(doc, items[1].pos + 2)?.pos).toBe(items[1].pos)
  })

  it('maps a caret inside a callout’s paragraph to the paragraph, not the callout', () => {
    const doc = docOf(':::note[T]\nBody.\n:::\n')
    const [, paragraph] = collectRows(doc)
    expect(rowAt(doc, paragraph.pos + 1)?.node.type.name).toBe('paragraph')
  })
})

describe('a range of blocks', () => {
  it('is the two rows and everything between them', () => {
    const doc = docOf('a\n\nb\n\nc\n')
    const rows = collectRows(doc)
    const range = rangeBetween(doc, rows[0], rows[2])
    expect(rowsIn(doc, range)).toHaveLength(3)
  })

  it('lifts to siblings when the ends sit at different depths', () => {
    // From a nested bullet to the paragraph after the list: the answer has to be one run of
    // siblings, or "move this" has no single meaning.
    const doc = docOf('- one\n  - nested\n\nafter\n')
    const rows = collectRows(doc)
    const nested = rows.find((row) => row.depth === 3)!
    const after = rows.find((row) => row.node.type.name === 'paragraph' && row.depth === 0)!

    // The whole list comes along: it is the outer item's sibling that the paragraph shares
    // a parent with, so the run is [list, paragraph] and every row inside it is covered.
    const range = rangeBetween(doc, nested, after)
    expect(range.from).toBe(0)
    expect(rowsIn(doc, range).map((row) => row.node.type.name)).toEqual([
      'listItem',
      'listItem',
      'paragraph',
    ])
  })

  it('is symmetric', () => {
    const doc = docOf('a\n\nb\n')
    const rows = collectRows(doc)
    expect(rangeBetween(doc, rows[1], rows[0])).toEqual(rangeBetween(doc, rows[0], rows[1]))
  })
})

describe('where a block can be dropped', () => {
  it('offers inside a list item, after it, and after the list', () => {
    // These three are what the horizontal cursor position chooses between (§4.2).
    const doc = docOf('- one\n\nafter\n')
    const item = collectRows(doc).find((row) => row.node.type.name === 'listItem')!
    const options = insertionsAfter(doc, item)

    expect(options.map((entry) => entry.depth)).toEqual([2, 1, 0])
    expect(options[0].inside).toBe(true)
  })

  it('offers nothing deeper than a paragraph, which holds no blocks', () => {
    const doc = docOf('one\n\ntwo\n')
    const first = collectRows(doc)[0]
    expect(insertionsAfter(doc, first).every((entry) => !entry.inside)).toBe(true)
  })

  it('stops climbing at an ancestor the row does not end', () => {
    // The first item is in the middle of its list, so "after the list" is not this gap.
    const doc = docOf('- one\n- two\n')
    const first = collectRows(doc)[0]
    expect(insertionsAfter(doc, first).map((entry) => entry.depth)).toEqual([2, 1])
  })

  it('offers a callout’s inside from its last child', () => {
    const doc = docOf(':::note[T]\nBody.\n:::\n\nafter\n')
    const body = collectRows(doc).find((row) => row.depth === 1)!
    expect(insertionsAfter(doc, body).map((entry) => entry.depth)).toEqual([1, 0])
  })
})

describe('moving blocks', () => {
  function move(markdown: string, from: number, to: { row: number; option: number }): string {
    const doc = docOf(markdown)
    const rows = collectRows(doc)
    const state = EditorState.create({ doc })
    const target = insertionsAfter(doc, rows[to.row])[to.option]
    const tr = state.tr
    expect(moveRange(tr, rangeOf(rows[from]), target.pos)).not.toBeNull()
    return markdownOf(tr.doc)
  }

  it('moves a block down past another', () => {
    expect(move('a\n\nb\n\nc\n', 0, { row: 1, option: 0 })).toBe('b\n\na\n\nc\n')
  })

  it('moves a block up', () => {
    const doc = docOf('a\n\nb\n\nc\n')
    const rows = collectRows(doc)
    const tr = EditorState.create({ doc }).tr
    // Before the first row is the only gap with nothing above it.
    moveRange(tr, rangeOf(rows[2]), rows[0].pos)
    expect(markdownOf(tr.doc)).toBe('c\n\na\n\nb\n')
  })

  it('drops a paragraph into a list item, which is what the x position buys', () => {
    const doc = docOf('- one\n\nloose\n')
    const rows = collectRows(doc)
    const item = rows.find((row) => row.node.type.name === 'listItem')!
    const loose = rows.find((row) => row.node.type.name === 'paragraph' && row.depth === 0)!
    const inside = insertionsAfter(doc, item).find((entry) => entry.inside)!

    const tr = EditorState.create({ doc }).tr
    moveRange(tr, rangeOf(loose), inside.pos)
    expect(markdownOf(tr.doc)).toBe('- one\n\n  loose\n')
  })

  it('lifts a nested block back out', () => {
    const doc = docOf('- one\n\n  nested\n')
    const rows = collectRows(doc)
    const nested = rows.find((row) => row.depth === 2)!
    const item = rows.find((row) => row.node.type.name === 'listItem')!
    const after = insertionsAfter(doc, item).find((entry) => entry.depth === 0)!

    const tr = EditorState.create({ doc }).tr
    moveRange(tr, rangeOf(nested), after.pos)
    expect(markdownOf(tr.doc)).toBe('- one\n\nnested\n')
  })

  it('moves a callout with everything in it', () => {
    const source = ':::warning[Blocked]\nBody.\n:::\n\nafter\n'
    const doc = docOf(source)
    const rows = collectRows(doc)
    const callout = rows[0]
    const after = rows.find((row) => row.node.type.name === 'paragraph' && row.depth === 0)!

    const tr = EditorState.create({ doc }).tr
    moveRange(tr, rangeOf(callout), after.end)
    expect(markdownOf(tr.doc)).toBe('after\n\n:::warning[Blocked]\nBody.\n:::\n')
  })

  it('refuses to drop a container inside itself', () => {
    // §4.2 shows no indicator for this; the guard is here so no path can reach it anyway.
    const doc = docOf(':::note[T]\nBody.\n:::\n')
    const callout = collectRows(doc)[0]
    const tr = EditorState.create({ doc }).tr
    expect(moveRange(tr, rangeOf(callout), callout.pos + 2)).toBeNull()
  })

  it('moves several selected blocks as one', () => {
    const doc = docOf('a\n\nb\n\nc\n\nd\n')
    const rows = collectRows(doc)
    const range = rangeBetween(doc, rows[0], rows[1])
    const tr = EditorState.create({ doc }).tr
    moveRange(tr, range, rows[3].end)
    expect(markdownOf(tr.doc)).toBe('c\n\nd\n\na\n\nb\n')
  })
})
