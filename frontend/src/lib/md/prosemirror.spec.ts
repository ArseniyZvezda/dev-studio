/**
 * The bridge must not cost anything §8 already bought.
 *
 * Every check here is the round trip *through* ProseMirror compared against the round trip
 * without it: if a document survives `parse → serialize` but not `parse → pm → mdast →
 * serialize`, the loss is in the mapping, and this is where it shows up rather than in a
 * browser three PRs later.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { normalise, parseDocument, serializeDocument } from './document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from './prosemirror'

const FIXTURES = fileURLToPath(new URL('./__fixtures__', import.meta.url))
const FIXTURE_NAMES = readdirSync(FIXTURES).filter((name) => name.endsWith('.md'))

/** What the editor would save after opening a document and touching nothing. */
function throughEditor(source: string): string {
  const { root, envelope } = parseDocument(source)
  const document = mdastToProseMirror(root)
  return serializeDocument(proseMirrorToMdast(document), envelope)
}

function docOf(source: string): PmNode {
  return mdastToProseMirror(parseDocument(source).root)
}

function firstBlock(source: string): PmNode {
  return docOf(source).content?.[0] as PmNode
}

describe('the corpus survives the bridge', () => {
  it.each(FIXTURE_NAMES)('%s is unchanged by a trip through ProseMirror', (name) => {
    const source = readFileSync(join(FIXTURES, name), 'utf8')
    expect(throughEditor(source)).toBe(normalise(source))
  })

  it.each(FIXTURE_NAMES)('%s is stable when the trip is taken twice', (name) => {
    const once = throughEditor(readFileSync(join(FIXTURES, name), 'utf8'))
    expect(throughEditor(once)).toBe(once)
  })
})

describe('blocks', () => {
  const BLOCKS: Record<string, [markdown: string, type: string]> = {
    paragraph: ['Text.\n', 'paragraph'],
    heading: ['## Rules\n', 'heading'],
    'bulleted list': ['- one\n', 'bulletList'],
    'numbered list': ['1. one\n', 'orderedList'],
    'task list': ['- [ ] one\n', 'taskList'],
    quote: ['> quoted\n', 'blockquote'],
    code: ['```ts\nconst x = 1\n```\n', 'codeBlock'],
    table: ['| a |\n| --- |\n| 1 |\n', 'table'],
    divider: ['---\n', 'horizontalRule'],
    callout: [':::note[Title]\nBody.\n:::\n', 'callout'],
    toggle: [':::toggle[Title]{open}\nBody.\n:::\n', 'toggle'],
    'html block': ['<div>raw</div>\n', 'unsupportedBlock'],
    'footnote definition': ['[^a]: note\n', 'unsupportedBlock'],
    'link definition': ['[a]: https://example.invalid/a\n', 'unsupportedBlock'],
    'unknown directive': [':::mystery\nBody.\n:::\n', 'unsupportedBlock'],
  }

  it.each(Object.entries(BLOCKS))('%s becomes the right node', (_name, [markdown, type]) => {
    expect(firstBlock(markdown).type).toBe(type)
  })

  it.each(Object.entries(BLOCKS))('%s comes back as it was', (_name, [markdown]) => {
    expect(throughEditor(markdown)).toBe(markdown)
  })

  it('keeps the heading level', () => {
    expect(firstBlock('#### Deep\n').attrs?.level).toBe(4)
    expect(throughEditor('#### Deep\n')).toBe('#### Deep\n')
  })

  it('keeps the code language', () => {
    expect(firstBlock('```python\nx = 1\n```\n').attrs?.language).toBe('python')
  })

  it('keeps the start of a numbered list', () => {
    expect(firstBlock('5. five\n6. six\n').attrs?.start).toBe(5)
    expect(throughEditor('5. five\n6. six\n')).toBe('5. five\n6. six\n')
  })

  it('keeps a checked task and an unchecked one apart', () => {
    const items = firstBlock('- [x] done\n- [ ] todo\n').content ?? []
    expect(items.map((item) => item.attrs?.checked)).toEqual([true, false])
    expect(throughEditor('- [x] done\n- [ ] todo\n')).toBe('- [x] done\n- [ ] todo\n')
  })

  it('keeps a plain item inside a task list plain', () => {
    // GFM allows the mix, and turning the third item into an unticked box would be an edit
    // the user never made.
    const source = '- [x] done\n- [ ] todo\n- just an item\n'
    const items = firstBlock(source).content ?? []
    expect(items.map((item) => item.attrs?.checked)).toEqual([true, false, null])
    expect(throughEditor(source)).toBe(source)
  })

  it('keeps table alignment on the table', () => {
    const source = '| a | b | c |\n| :--- | :---: | ---: |\n| 1 | 2 | 3 |\n'
    expect(firstBlock(source).attrs?.align).toEqual(['left', 'center', 'right'])
    expect(throughEditor(source)).toBe(source)
  })

  it('marks the first table row as headers', () => {
    const rows = firstBlock('| a |\n| --- |\n| 1 |\n').content ?? []
    expect(rows[0].content?.[0].type).toBe('tableHeader')
    expect(rows[1].content?.[0].type).toBe('tableCell')
  })

  it('keeps the callout kind', () => {
    expect(firstBlock(':::danger[Stop]\nBody.\n:::\n').attrs?.kind).toBe('danger')
  })

  it('keeps whether a toggle is open', () => {
    expect(firstBlock(':::toggle[A]{open}\nB.\n:::\n').attrs?.open).toBe(true)
    expect(firstBlock(':::toggle[A]\nB.\n:::\n').attrs?.open).toBe(false)
  })

  it('gives a directive its title as an editable node', () => {
    const callout = firstBlock(':::note[The **title**]\nBody.\n:::\n')
    const title = callout.content?.[0]
    expect(title?.type).toBe('directiveTitle')
    // The label carries inline markup, so it cannot be a plain string attribute.
    expect(title?.content?.[1].marks?.[0].type).toBe('bold')
  })

  it('gives an untitled directive an empty title node, and writes no label back', () => {
    // Always first, titled or not — a collapsed toggle hides "everything after the title",
    // which needs the title to be somewhere fixed.
    const title = firstBlock(':::note\nBody.\n:::\n').content?.[0]
    expect(title?.type).toBe('directiveTitle')
    expect(title?.content).toBeUndefined()
    expect(throughEditor(':::note\nBody.\n:::\n')).toBe(':::note\nBody.\n:::\n')
  })

  it('nests directives', () => {
    const source = '::::note[Outer]\n:::tip[Inner]\nBody.\n:::\n::::\n'
    const outer = firstBlock(source)
    expect(outer.content?.[1].type).toBe('callout')
    expect(throughEditor(source)).toBe(source)
  })

  it('drops an empty trailing paragraph rather than writing a blank line', () => {
    const document: PmNode = {
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Body.' }] }, { type: 'paragraph' }],
    }
    const { envelope } = parseDocument('Body.\n')
    expect(serializeDocument(proseMirrorToMdast(document), envelope)).toBe('Body.\n')
  })

  it('turns an empty document into one empty paragraph', () => {
    expect(docOf('')).toEqual({ type: 'doc', content: [{ type: 'paragraph' }] })
  })
})

describe('inline markup', () => {
  const INLINE = [
    '**bold**',
    '*italic*',
    '~~struck~~',
    '`code`',
    '[a link](https://example.invalid/a)',
    '[[wiki-link]]',
    '[[wiki|labelled]]',
    '**bold with *italic* inside**',
    '*[[a link inside emphasis]]*',
    '[**bold** inside a link](https://example.invalid/a)',
    '`code` next to **bold**',
    '![an image](https://example.invalid/i.png)',
    'text with <kbd>html</kbd> in it',
    'a footnote[^a]',
  ]

  // Against `normalise`, not against the source: the bridge has to cost nothing *beyond*
  // what §8 already allows the first save to do. `[^a]` with no definition is prose, and
  // prose containing a bracket gets escaped — by the serializer, not by this mapping.
  it.each(INLINE)('%s costs nothing extra on the way through', (markdown) => {
    const source = `Before ${markdown} after.\n`
    expect(throughEditor(source)).toBe(normalise(source))
  })

  it('carries a real footnote reference and its definition', () => {
    const source = 'A claim[^a]\n\n[^a]: The evidence.\n'
    expect(throughEditor(source)).toBe(source)
  })

  it('flattens nesting into marks and rebuilds it', () => {
    const paragraph = firstBlock('***both***\n')
    const text = paragraph.content?.[0]
    expect(text?.marks?.map((mark) => mark.type).sort()).toEqual(['bold', 'italic'])
  })

  it('keeps a link target and title on the mark', () => {
    const paragraph = firstBlock('[a](https://example.invalid/a "T")\n')
    const mark = paragraph.content?.[0].marks?.find((entry) => entry.type === 'link')
    expect(mark?.attrs).toEqual({ href: 'https://example.invalid/a', title: 'T' })
  })

  it('carries a wiki link whole, with its raw text', () => {
    const paragraph = firstBlock('See [[qa/plan|the plan]].\n')
    const link = paragraph.content?.find((child) => child.type === 'wikiLink')
    expect(link?.attrs).toEqual({ value: 'qa/plan|the plan', target: 'qa/plan', label: 'the plan' })
  })

  it('does not merge two adjacent links into one', () => {
    const source = '[a](https://example.invalid/a)[b](https://example.invalid/b)\n'
    expect(throughEditor(source)).toBe(source)
  })

  it('keeps a soft break', () => {
    expect(throughEditor('first\\\nsecond\n')).toBe('first\\\nsecond\n')
  })

  it('drops whitespace left hanging off the end of a block', () => {
    // A space at the end of a paragraph means nothing in Markdown, and writing it out means
    // writing `&#x20;` — an entity in the file, for a space somebody left while thinking.
    const document: PmNode = {
      type: 'doc',
      content: [
        { type: 'paragraph', content: [{ type: 'text', text: 'text with a space ' }] },
        { type: 'heading', attrs: { level: 2 }, content: [{ type: 'text', text: 'heading  ' }] },
      ],
    }
    const { envelope } = parseDocument('')
    expect(serializeDocument(proseMirrorToMdast(document), { ...envelope, finalNewline: true })).toBe(
      'text with a space\n\n## heading\n',
    )
  })

  it('leaves a space that is not at the end alone', () => {
    expect(throughEditor('one **two** three\n')).toBe('one **two** three\n')
  })
})

describe('the §8.3 rule', () => {
  it('carries an unmodelled block through as its own mdast node', () => {
    const block = firstBlock('<figure>\n  <img src="a.png">\n</figure>\n')
    expect(block.type).toBe('unsupportedBlock')
    expect((block.attrs?.mdast as { type: string }).type).toBe('html')
  })

  it('never shares a node with the tree it came from', () => {
    // The editor may hand these attrs around; mutating one must not reach back into the
    // parsed document.
    const { root } = parseDocument('<div>raw</div>\n')
    const block = mdastToProseMirror(root).content?.[0]
    expect(block?.attrs?.mdast).not.toBe(root.children[0])
    expect(block?.attrs?.mdast).toEqual(root.children[0])
  })
})
