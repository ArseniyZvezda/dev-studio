/**
 * The "Turn into" matrix (block-editor spec §4.6).
 *
 * Asserted as Markdown on both sides, because that is where the rules are actually stated:
 * "content including inline markup is preserved", "markup is lost going into code", "a
 * container unwraps rather than deletes". Comparing trees would say less than comparing the
 * two lines that came out.
 */

import { getSchema } from '@tiptap/core'
import { Node as PmDoc } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'

import { parseDocument, serializeDocument } from '@/lib/md/document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'

import { markdownOfRow } from './block-menu'
import { collectRows } from './blocks'
import { createExtensions } from './extensions'
import { createSlashState } from './slash-menu'
import { canTransform, currentListTarget, currentTarget, transformRow, type TransformTarget } from './transform'

const schema = getSchema(createExtensions({ slash: createSlashState() }))

function stateOf(markdown: string): EditorState {
  const doc = PmDoc.fromJSON(schema, mdastToProseMirror(parseDocument(markdown).root) as never)
  return EditorState.create({ doc })
}

function markdownOf(doc: PmDoc): string {
  return serializeDocument(proseMirrorToMdast(doc.toJSON() as PmNode), {
    frontmatter: '',
    eol: '\n',
    finalNewline: true,
  })
}

/** Turn the first row of a document into `target` and read the file back. */
function turn(markdown: string, target: TransformTarget, rowIndex = 0): string {
  const state = stateOf(markdown)
  const row = collectRows(state.doc)[rowIndex]
  const tr = state.tr
  expect(transformRow(tr, state, row, target)).toBe(true)
  return markdownOf(tr.doc)
}

describe('what takes part', () => {
  it.each([
    ['a paragraph', 'text\n', true],
    ['a heading', '# t\n', true],
    ['a quote', '> q\n', true],
    ['a list item', '- i\n', true],
    ['a callout', ':::note\nb\n:::\n', true],
    ['a toggle', ':::toggle\nb\n:::\n', true],
    ['a code block', '```\nx\n```\n', true],
    // §4.6 names these two as staying out of it.
    ['a table', '| a |\n| --- |\n| 1 |\n', false],
    ['a divider', '---\n', false],
    // §3.4: an unsupported block is held, never reshaped.
    ['an unsupported block', '<div>x</div>\n', false],
  ])('%s: %s', (_name, markdown, expected) => {
    const state = stateOf(markdown)
    expect(canTransform(collectRows(state.doc)[0].node)).toBe(expected)
  })
})

describe('the reversible group keeps its content', () => {
  // §4.6: "Text ↔ heading ↔ quote ↔ any list item ↔ callout ↔ toggle — content, including
  // inline markup, is preserved in full."
  const SOURCE = 'A line with **bold** and *italic* and `code`.\n'

  it.each<[TransformTarget, string]>([
    ['heading1', '# A line with **bold** and *italic* and `code`.\n'],
    ['heading2', '## A line with **bold** and *italic* and `code`.\n'],
    ['heading3', '### A line with **bold** and *italic* and `code`.\n'],
    ['blockquote', '> A line with **bold** and *italic* and `code`.\n'],
    ['bulletList', '- A line with **bold** and *italic* and `code`.\n'],
    ['orderedList', '1. A line with **bold** and *italic* and `code`.\n'],
    ['taskList', '- [ ] A line with **bold** and *italic* and `code`.\n'],
    ['callout', ':::note\nA line with **bold** and *italic* and `code`.\n:::\n'],
    ['toggle', ':::toggle\nA line with **bold** and *italic* and `code`.\n:::\n'],
  ])('text → %s', (target, expected) => {
    expect(turn(SOURCE, target)).toBe(expected)
  })

  it('comes back to text unchanged', () => {
    for (const target of ['heading2', 'blockquote', 'bulletList', 'callout'] as TransformTarget[]) {
      const there = turn(SOURCE, target)
      expect(turn(there, 'paragraph')).toBe(SOURCE)
    }
  })

  it('turns a heading into a quote without losing its level’s text', () => {
    expect(turn('## Rules\n', 'blockquote')).toBe('> Rules\n')
  })

  it('changes a list’s kind rather than nesting a second list inside it', () => {
    expect(turn('- one\n- two\n', 'orderedList')).toBe('1. one\n2. two\n')
    expect(turn('1. one\n2. two\n', 'taskList')).toBe('- [ ] one\n- [ ] two\n')
    expect(turn('- [x] done\n', 'bulletList')).toBe('- done\n')
  })

  it('keeps a ticked box ticked when the list stays a checklist', () => {
    expect(turn('- [x] done\n- [ ] todo\n', 'taskList')).toBe('- [x] done\n- [ ] todo\n')
  })

  it('lifts a list item out of its list when it stops being one', () => {
    expect(turn('- one\n', 'paragraph')).toBe('one\n')
    expect(turn('- one\n', 'heading2')).toBe('## one\n')
  })
})

describe('containers unwrap rather than delete', () => {
  // §4.6: "when a container is turned into a simple block its content is not deleted, it is
  // unfolded to the top level."
  it('unfolds a callout with several blocks', () => {
    expect(turn(':::warning[Blocked]\nFirst.\n\nSecond.\n:::\n', 'paragraph')).toBe(
      'First.\n\nSecond.\n',
    )
  })

  it('unfolds a toggle, dropping only its title', () => {
    expect(turn(':::toggle[Details]\nHidden.\n:::\n', 'paragraph')).toBe('Hidden.\n')
  })

  it('unfolds a quote', () => {
    expect(turn('> First.\n>\n> Second.\n', 'paragraph')).toBe('First.\n\nSecond.\n')
  })

  it('keeps a list inside a callout when the callout becomes a quote', () => {
    // One list with two items, so it stays tight — the content moved container, nothing else.
    expect(turn(':::note\n- one\n- two\n:::\n', 'blockquote')).toBe('> - one\n> - two\n')
  })

  it('carries a callout’s whole content into another container', () => {
    expect(turn(':::note[T]\nBody.\n:::\n', 'toggle')).toBe(':::toggle\nBody.\n:::\n')
  })
})

describe('code, in and out', () => {
  it('loses the markup on the way in, and says nothing about it', () => {
    // §4.6 asks for no warning — one Ctrl+Z is the answer, and undo is ProseMirror's.
    expect(turn('A line with **bold**.\n', 'codeBlock')).toBe('```\nA line with bold.\n```\n')
  })

  it('joins several blocks into one code block, a line each', () => {
    expect(turn(':::note\nFirst.\n\nSecond.\n:::\n', 'codeBlock')).toBe(
      '```\nFirst.\nSecond.\n```\n',
    )
  })

  it('comes back out as text, unescaped', () => {
    // §4.6: "content is inserted as text, without escaping" — the asterisks stay literal
    // characters rather than becoming emphasis, and the serializer escapes them for us.
    expect(turn('```\nplain *not emphasis* text\n```\n', 'paragraph')).toBe(
      'plain \\*not emphasis\\* text\n',
    )
  })

  it('turns each line of code into its own block', () => {
    expect(turn('```\none\ntwo\n```\n', 'paragraph')).toBe('one\n\ntwo\n')
  })

  it('keeps the language out of the text when leaving a code block', () => {
    expect(turn('```python\nx = 1\n```\n', 'paragraph')).toBe('x = 1\n')
  })
})

describe('what the menu shows as current', () => {
  it.each<[string, TransformTarget]>([
    ['text\n', 'paragraph'],
    ['## t\n', 'heading2'],
    ['> q\n', 'blockquote'],
    [':::note\nb\n:::\n', 'callout'],
    [':::toggle\nb\n:::\n', 'toggle'],
    ['```\nx\n```\n', 'codeBlock'],
  ])('%s is %s', (markdown, expected) => {
    const state = stateOf(markdown)
    expect(currentTarget(collectRows(state.doc)[0])).toBe(expected)
  })

  it('has nothing to show for a heading below level 3', () => {
    // §3.1 keeps 4–6 working and out of the menus, so there is no entry to highlight.
    expect(currentTarget(collectRows(stateOf('##### deep\n').doc)[0])).toBeNull()
  })

  it('reads a list item’s kind off the list it is in', () => {
    for (const [markdown, expected] of [
      ['- i\n', 'bulletList'],
      ['1. i\n', 'orderedList'],
      ['- [ ] i\n', 'taskList'],
    ] as [string, TransformTarget][]) {
      const state = stateOf(markdown)
      expect(currentListTarget(state, collectRows(state.doc)[0])).toBe(expected)
    }
  })
})

describe('copy as markdown (§4.5)', () => {
  it.each([
    ['a paragraph', 'A line with **bold**.\n', 'A line with **bold**.'],
    ['a heading', '## Rules\n', '## Rules'],
    ['a code block', '```python\nx = 1\n```\n', '```python\nx = 1\n```'],
    ['a callout', ':::warning[Blocked]\nBody.\n:::\n', ':::warning[Blocked]\nBody.\n:::'],
    ['a table', '| a | b |\n| --- | ---: |\n| 1 | 2 |\n', '| a | b |\n| --- | ---: |\n| 1 | 2 |'],
    ['a wiki link', 'See [[qa/plan|the plan]].\n', 'See [[qa/plan|the plan]].'],
  ])('%s comes out as its own source', (_name, markdown, expected) => {
    // §4.5: "the block's original markdown lands on the clipboard" — the same bytes a save
    // would have written for it, and nothing of the document around it.
    const state = stateOf(markdown)
    expect(markdownOfRow(state.doc, collectRows(state.doc)[0])).toBe(expected)
  })

  it('keeps a list item’s own marker, and only its own', () => {
    // A bare item has no Markdown form, so it is written back inside a list of one — which
    // is also what someone pasting it expects to get.
    const state = stateOf('- one\n- two\n')
    expect(markdownOfRow(state.doc, collectRows(state.doc)[1])).toBe('- two')

    const tasks = stateOf('- [x] done\n- [ ] todo\n')
    expect(markdownOfRow(tasks.doc, collectRows(tasks.doc)[0])).toBe('- [x] done')
  })

  it('takes a callout whole, contents included', () => {
    const state = stateOf(':::note[T]\n- one\n- two\n:::\n')
    expect(markdownOfRow(state.doc, collectRows(state.doc)[0])).toBe(':::note[T]\n- one\n- two\n:::')
  })
})

describe('the whole matrix round-trips', () => {
  const TARGETS: TransformTarget[] = [
    'paragraph',
    'heading1',
    'heading2',
    'heading3',
    'blockquote',
    'bulletList',
    'orderedList',
    'taskList',
    'callout',
    'toggle',
    'codeBlock',
  ]

  it.each(TARGETS)('every source turns into %s and produces valid Markdown', (target) => {
    const sources = [
      'text\n',
      '## heading\n',
      '> quote\n',
      '- item\n',
      '1. item\n',
      '- [ ] task\n',
      ':::note[T]\nbody\n:::\n',
      ':::toggle[T]\nbody\n:::\n',
      '```\ncode\n```\n',
    ]
    for (const source of sources) {
      const result = turn(source, target)
      expect(result).not.toBe('')
      // Whatever came out has to survive a save and a reload unchanged (§8.2).
      const state = stateOf(result)
      expect(markdownOf(state.doc)).toBe(result)
    }
  })
})
