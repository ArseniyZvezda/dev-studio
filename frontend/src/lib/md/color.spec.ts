/**
 * `[text]{.green}` as a property of the file (block-editor spec §3.2, §8).
 *
 * Two questions, and the second is the one that can lose someone's work: does the syntax
 * mean what it says, and does everything that merely *looks* like it come back untouched.
 * The negative cases below outnumber the positive ones on purpose.
 */

import { describe, expect, it } from 'vitest'

import { maskColors, TEXT_COLORS } from './color'
import { normalise, parseDocument, serializeDocument } from './document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from './prosemirror'

/** The inline nodes of the document's first paragraph. */
function inline(source: string) {
  const first = parseDocument(source).root.children[0]
  return first?.type === 'paragraph' ? first.children : []
}

function firstSpan(source: string) {
  return inline(source).find((node) => node.type === 'colorSpan')
}

/** What the editor would save after opening the document and touching nothing. */
function roundTrip(source: string): string {
  const { root, envelope } = parseDocument(source)
  return serializeDocument(proseMirrorToMdast(mdastToProseMirror(root)), envelope)
}

/** The private-use blocks both masks draw their sentinels from. */
const SENTINEL = /[\uE000-\uE01F]/

describe('a span is recognised', () => {
  it('becomes a colour span with the word the file used', () => {
    const span = firstSpan('Build [ok]{.green}.\n')
    expect(span).toMatchObject({ type: 'colorSpan', color: 'green' })
  })

  it.each(TEXT_COLORS)('accepts %s', (color) => {
    expect(firstSpan(`A [word]{.${color}} here.\n`)).toMatchObject({ color })
  })

  it('keeps `grey` spelled as it was found', () => {
    expect(normalise('A [word]{.grey} here.\n')).toBe('A [word]{.grey} here.\n')
    expect(normalise('A [word]{.gray} here.\n')).toBe('A [word]{.gray} here.\n')
  })

  it('parses the markup inside it', () => {
    const span = firstSpan('[**bold** and `code`]{.red}\n')
    expect(span?.children.map((child) => child.type)).toEqual(['strong', 'text', 'inlineCode'])
  })

  it('holds a wiki link', () => {
    const span = firstSpan('[see [[test-plan]] first]{.orange}\n')
    expect(span?.children.some((child) => child.type === 'wikiLink')).toBe(true)
  })

  it('holds a link, brackets and all', () => {
    const span = firstSpan('[read [the plan](plan.md) now]{.blue}\n')
    expect(span?.children.some((child) => child.type === 'link')).toBe(true)
  })

  it('nests', () => {
    const outer = firstSpan('[outer [inner]{.red} tail]{.gray}\n')
    expect(outer).toMatchObject({ color: 'gray' })
    expect(outer?.children.find((child) => child.type === 'colorSpan')).toMatchObject({
      color: 'red',
    })
  })

  it('is not stolen by a link definition of the same name', () => {
    // The whole reason for masking before the parser runs: without it `[ok]` is a shortcut
    // link reference here and the `{.green}` is left dangling as prose.
    const source = 'A [ok]{.green} word.\n\n[ok]: https://example.invalid/a\n'
    expect(firstSpan(source)).toMatchObject({ color: 'green' })
  })
})

describe('what only looks like a span', () => {
  const PROSE: Record<string, string> = {
    'an unknown colour': 'A \\[word]{.brand} here.\n',
    'an escaped bracket': 'A \\[word]{.green} here.\n',
    'a gap before the attribute': 'A \\[word] {.green} here.\n',
    'an empty span': 'A \\[]{.green} here.\n',
    'an attribute on its own': 'A {.green} here.\n',
    'an attribute with more in it': 'A \\[word]{.green #id} here.\n',
    'a bracket with no attribute': 'A \\[word] here.\n',
    'an unclosed bracket': 'A \\[word{.green} here.\n',
    'a link with an attribute after it': 'A [word](plan.md){.green} here.\n',
  }

  it.each(Object.entries(PROSE))('%s stays prose', (_name, source) => {
    expect(firstSpan(source)).toBeUndefined()
    expect(normalise(source)).toBe(source)
  })

  it('is not a span across two lines', () => {
    // A paragraph is written as one line (§8.1), so a bracket that reaches the end of its
    // line has not found its partner — and refusing to look further is what keeps an
    // unmatched `[` from scanning a whole document.
    expect(maskColors('[start\nend]{.green}').spans).toHaveLength(0)
  })

  it('does not read a code span', () => {
    expect(maskColors('`[ok]{.green}`').spans).toHaveLength(0)
  })

  it('closes after a code span rather than inside one', () => {
    // CommonMark resolves backticks before brackets, so the `]` in the code span is a
    // character; a scanner that stopped there would end the colour mid-sentence.
    const span = firstSpan('[a `]` b]{.green}\n')
    expect(span?.color).toBe('green')
    expect(normalise('[a `]` b]{.green}\n')).toBe('[a `]` b]{.green}\n')
  })

  it('leaves a fenced code block alone', () => {
    const source = '```md\n[not a span]{.green}\n```\n'
    expect(normalise(source)).toBe(source)
    expect(parseDocument(source).root.children[0]).toMatchObject({
      type: 'code',
      value: '[not a span]{.green}',
    })
  })

  it('leaves the text of an inline code span alone', () => {
    const source = 'Write `[ok]{.green}` for that.\n'
    expect(normalise(source)).toBe(source)
    expect(inline(source).some((node) => node.type === 'colorSpan')).toBe(false)
  })
})

describe('the file survives', () => {
  const CASES: Record<string, string> = {
    plain: 'Build [ok]{.green}.\n',
    'with markup': '[**bold** and *italic*]{.red}\n',
    'with a bracket inside': '[a \\[b\\] c]{.purple}\n',
    'with a wiki link': '[see [[test-plan]]]{.orange}\n',
    'with a link': '[read [the plan](plan.md)]{.blue}\n',
    nested: '[outer [inner]{.red} tail]{.gray}\n',
    'in a heading': '## A [warning]{.yellow}\n',
    'in a list': '- [done]{.green}\n',
    'in a quote': '> [careful]{.red}\n',
    'in a table': '| a |\n| --- |\n| [ok]{.green} |\n',
    'in a callout': ':::warning[Title]\n[on hold]{.red}\n:::\n',
    'twice on one line': '[a]{.red} and [b]{.blue}\n',
    'the whole paragraph': '[everything]{.cyan}\n',
  }

  it.each(Object.entries(CASES))('%s is written back unchanged', (_name, source) => {
    expect(normalise(source)).toBe(source)
  })

  it.each(Object.entries(CASES))('%s survives the trip through ProseMirror', (_name, source) => {
    expect(roundTrip(source)).toBe(source)
  })

  it.each(Object.entries(CASES))('%s leaks no masking sentinel', (_name, source) => {
    expect(SENTINEL.test(normalise(source))).toBe(false)
  })

  it('is stable from the second save on', () => {
    const messy = 'A [ok]{.green} and [[wiki]] and *em*.\n'
    const once = normalise(messy)
    expect(normalise(once)).toBe(once)
  })
})

describe('the ProseMirror mark', () => {
  function marksOf(source: string): PmNode[] {
    const document = mdastToProseMirror(parseDocument(source).root)
    return document.content?.[0]?.content ?? []
  }

  it('is carried on the text, not around it', () => {
    expect(marksOf('[ok]{.green}\n')[0]).toMatchObject({
      type: 'text',
      text: 'ok',
      marks: [{ type: 'textColor', attrs: { color: 'green' } }],
    })
  })

  it('combines with the other marks', () => {
    const [node] = marksOf('[**ok**]{.green}\n')
    expect(node.marks?.map((mark) => mark.type)).toEqual(['textColor', 'bold'])
  })

  it('reaches a wiki link, which is an atom', () => {
    const nodes = marksOf('[[[test-plan]]]{.green}\n')
    const link = nodes.find((node) => node.type === 'wikiLink')
    expect(link?.marks).toEqual([{ type: 'textColor', attrs: { color: 'green' } }])
  })

  it('does not merge two colours into one span', () => {
    expect(roundTrip('[a]{.red}[b]{.blue}\n')).toBe('[a]{.red}[b]{.blue}\n')
  })

  it('is dropped rather than written as an empty class', () => {
    // Nothing in the UI can produce this; the serializer still has to answer for it,
    // because `[x]{.}` is a span no parser would read back.
    const document: PmNode = {
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [{ type: 'text', text: 'x', marks: [{ type: 'textColor', attrs: {} }] }],
        },
      ],
    }
    const envelope = { frontmatter: '', eol: '\n' as const, finalNewline: true }
    expect(serializeDocument(proseMirrorToMdast(document), envelope)).toBe('x\n')
  })
})
