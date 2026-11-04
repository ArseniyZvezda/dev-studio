/**
 * The colour mark, against the editor's **real** schema.
 *
 * `color.spec.ts` next door proves the Markdown round trip on plain JSON, which is where
 * the format lives. What it cannot see is whether the schema Tiptap builds actually accepts
 * that JSON: a mark named `textColor` in `prosemirror.ts` and `textColour` in the extension
 * would pass every test there and throw on the first document that used one.
 */

import { getSchema } from '@tiptap/core'
import { readFileSync } from 'node:fs'
import { Node } from '@tiptap/pm/model'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { parseDocument, serializeDocument } from '@/lib/md'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'
import { createExtensions } from './extensions'
import { createSlashState } from './slash-menu'

const schema = getSchema(createExtensions({ slash: createSlashState() }))

const FIXTURE = fileURLToPath(new URL('../md/__fixtures__/colors.md', import.meta.url))

describe('the schema knows the mark', () => {
  it('registers it', () => {
    expect(schema.marks.textColor).toBeDefined()
  })

  it('replaces itself, so two colours cannot land on one character', () => {
    // The ProseMirror default — a mark type excludes its own type — and the reason the
    // toolbar can `setMark` a new colour without unsetting the old one first.
    const green = schema.marks.textColor.create({ color: 'green' })
    const red = schema.marks.textColor.create({ color: 'red' })
    expect(red.addToSet([green])).toEqual([red])
  })

  it('renders the colour as both a class and an attribute', () => {
    const spec = schema.marks.textColor.spec.toDOM?.(
      schema.marks.textColor.create({ color: 'green' }),
      true,
    )
    expect(spec).toEqual(['span', { class: 'green', 'data-text-color': 'green' }, 0])
  })

  it('renders nothing for a colour it does not know', () => {
    const spec = schema.marks.textColor.spec.toDOM?.(
      schema.marks.textColor.create({ color: 'brand' }),
      true,
    )
    expect(spec).toEqual(['span', {}, 0])
  })
})

describe('a document of coloured prose', () => {
  const source = readFileSync(FIXTURE, 'utf8')

  it('is accepted by the schema exactly as the bridge builds it', () => {
    // `Node.fromJSON` is what the editor does on mount, and it throws on a mark or a node
    // the schema has never heard of — which is the failure this whole file exists to catch.
    const json = mdastToProseMirror(parseDocument(source).root)
    expect(() => Node.fromJSON(schema, json)).not.toThrow()
  })

  it('comes back off the schema as the same bytes', () => {
    const { root, envelope } = parseDocument(source)
    const document = Node.fromJSON(schema, mdastToProseMirror(root))
    expect(serializeDocument(proseMirrorToMdast(document.toJSON() as PmNode), envelope)).toBe(source)
  })
})
