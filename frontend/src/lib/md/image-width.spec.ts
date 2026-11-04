/**
 * `![shot](shot.png){width=300}` as a property of the file (block-editor spec §3.4, §8).
 *
 * The negative cases outnumber the positive ones on purpose, as they do for the colour spans:
 * a width that is not read is a picture at its own size, but braces that stop coming back
 * as they were written are an edit nobody made.
 */

import { describe, expect, it } from 'vitest'

import { normalise, parseDocument, serializeDocument, type DocumentEnvelope } from './document'
import { MAX_IMAGE_WIDTH } from './image-width'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from './prosemirror'

const ENVELOPE: DocumentEnvelope = { frontmatter: '', eol: '\n', finalNewline: true }

/** The inline nodes of the document's first paragraph. */
function inline(source: string) {
  const first = parseDocument(source).root.children[0]
  return first?.type === 'paragraph' ? first.children : []
}

/** Whether any node in the parsed document ended up with a width. */
function hasWidth(source: string): boolean {
  return JSON.stringify(parseDocument(source).root).includes('"width"')
}

/** What the editor would save after opening the document and touching nothing. */
function roundTrip(source: string): string {
  const { root, envelope } = parseDocument(source)
  return serializeDocument(proseMirrorToMdast(mdastToProseMirror(root)), envelope)
}

/** A one-picture document as the editor holds it, with the width the slider left. */
function picture(width: unknown, after?: string): PmNode {
  const content: PmNode[] = [{ type: 'image', attrs: { src: 'a.png', alt: 'a', title: null, width } }]
  if (after) content.push({ type: 'text', text: after })
  return { type: 'doc', content: [{ type: 'paragraph', content }] }
}

describe('a width is read', () => {
  it('moves `{width=…}` into the picture it follows', () => {
    const nodes = inline('![shot](shot.png){width=300}\n')
    expect(nodes).toHaveLength(1)
    expect(nodes[0]).toMatchObject({ type: 'image', url: 'shot.png', data: { width: 300 } })
  })

  it('leaves the text after it where it was', () => {
    const nodes = inline('Look: ![a](a.png){width=120} and more.\n')
    expect(nodes.map((node) => node.type)).toEqual(['text', 'image', 'text'])
    expect(nodes[2]).toMatchObject({ type: 'text', value: ' and more.' })
  })

  it.each([
    ['alone on its line', '![shot](shot.png){width=300}\n'],
    ['with a title', '![shot](shot.png "Frame sheet"){width=640}\n'],
    ['mid-sentence', 'Before ![a](a.png){width=48} after.\n'],
    ['two in a row', '![a](a.png){width=1}![b](b.png){width=10000}\n'],
    ['inside a link', '[![a](a.png){width=90}](big.png)\n'],
    ['inside emphasis', '**![a](a.png){width=90}**\n'],
    ['inside a colour span', '[see ![a](a.png){width=90}]{.green}\n'],
    ['inside a list item', '- ![a](a.png){width=200}\n'],
  ])('and written back byte for byte: %s', (_case, source) => {
    expect(hasWidth(source)).toBe(true)
    expect(normalise(source)).toBe(source)
    expect(roundTrip(source)).toBe(source)
  })
})

describe('everything that only looks like one stays text', () => {
  it.each([
    ['a space before it', '![a](a.png) {width=300}\n'],
    ['an escaped brace', '![a](a.png)\\{width=300}\n'],
    ['a unit', '![a](a.png){width=300px}\n'],
    ['a percentage', '![a](a.png){width=50%}\n'],
    ['zero', '![a](a.png){width=0}\n'],
    ['a leading zero', '![a](a.png){width=007}\n'],
    ['past the ceiling', `![a](a.png){width=${MAX_IMAGE_WIDTH + 1}}\n`],
    ['a class rather than a key', '![a](a.png){.widght=300}\n'],
    ['another key', '![a](a.png){height=200}\n'],
    ['two keys', '![a](a.png){width=300 height=200}\n'],
    ['no picture before it', 'Text{width=300}\n'],
    ['a link rather than a picture', '[a](a.png){width=300}\n'],
    ['inline code', '`![a](a.png){width=300}`\n'],
  ])('%s', (_case, source) => {
    expect(hasWidth(source)).toBe(false)
    expect(normalise(source)).toBe(source)
  })
})

describe('the editor', () => {
  it('hands the width to the node and gets it back', () => {
    const source = '![shot](shot.png){width=300}\n'
    const node = mdastToProseMirror(parseDocument(source).root).content?.[0]?.content?.[0]
    expect(node).toMatchObject({ type: 'image', attrs: { src: 'shot.png', width: 300 } })
  })

  it('gives a picture without one no width attribute at all', () => {
    const node = mdastToProseMirror(parseDocument('![shot](shot.png)\n').root).content?.[0]?.content?.[0]
    expect(node?.attrs).toEqual({ src: 'shot.png', alt: 'shot', title: null })
  })

  it('writes the width the slider set, and nothing once it is taken off', () => {
    expect(serializeDocument(proseMirrorToMdast(picture(240)), ENVELOPE)).toBe('![a](a.png){width=240}\n')
    expect(serializeDocument(proseMirrorToMdast(picture(null)), ENVELOPE)).toBe('![a](a.png)\n')
  })

  it.each([0, -5, 12.5, MAX_IMAGE_WIDTH + 1, '300'])('never writes a width it would not read: %s', (width) => {
    expect(serializeDocument(proseMirrorToMdast(picture(width)), ENVELOPE)).toBe('![a](a.png)\n')
  })

  it('escapes typed text that would come back as the width of the picture before it', () => {
    const markdown = serializeDocument(proseMirrorToMdast(picture(null, '{width=5} is the plan')), ENVELOPE)
    expect(markdown).toBe('![a](a.png)\\{width=5} is the plan\n')
    expect(hasWidth(markdown)).toBe(false)
    expect(normalise(markdown)).toBe(markdown)
  })

  it('does not escape braces the parser would not read anyway', () => {
    for (const text of ['{width=0}', '{width=50%}', '{height=5}']) {
      expect(serializeDocument(proseMirrorToMdast(picture(null, text)), ENVELOPE)).toBe(`![a](a.png)${text}\n`)
    }
  })

  it('writes a picture that has a width and then text that looks like one, both intact', () => {
    const markdown = serializeDocument(proseMirrorToMdast(picture(64, '{width=5}')), ENVELOPE)
    expect(markdown).toBe('![a](a.png){width=64}{width=5}\n')
    expect(inline(markdown)).toMatchObject([
      { type: 'image', data: { width: 64 } },
      { type: 'text', value: '{width=5}' },
    ])
    expect(normalise(markdown)).toBe(markdown)
  })
})
