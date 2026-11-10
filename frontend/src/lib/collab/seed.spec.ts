/**
 * A document must survive the trip through a live room (dev spec §16.4).
 *
 * A room edits a Yjs document, and the Markdown on disk is what one of its members
 * serialises from it. So everything the Markdown layer guarantees (block-editor spec §8) is
 * only as good as this: Markdown → editor document → Yjs → editor document has to give back
 * the same document, attribute for attribute. Checked on the same corpus the Markdown round
 * trip is, because the interesting cases are the ones that corpus was written to hold.
 *
 * The second editor is modelled the way it really works: the binding's own reader builds the
 * document from Yjs, and then the `carried` plugin repairs what that reader dropped
 * (`carried.ts`). Without the plugin's half these tests fail — which is the point of it.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { Node } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { ySyncPluginKey, yXmlFragmentToProseMirrorRootNode } from '@tiptap/y-tiptap'
import { describe, expect, it } from 'vitest'
import * as Y from 'yjs'

import { parseDocument, serializeDocument } from '@/lib/md/document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'
import {
  CARRIED_TYPES,
  COLLAB_FIELD,
  MARK_CARRIERS,
  NULL_CARRIERS,
  carriedPlugin,
  carry,
  reconcileCarried,
} from './carried'
import { roomSchema, seedUpdate } from './seed'

const FIXTURES = fileURLToPath(new URL('../md/__fixtures__', import.meta.url))
const FIXTURE_NAMES = readdirSync(FIXTURES).filter((name) => name.endsWith('.md'))

const schema = roomSchema()

/** The document as the binding alone reads it out of the room — before any repair. */
function asRendered(markdown: string): Node {
  const peer = new Y.Doc()
  Y.applyUpdate(peer, seedUpdate(markdown))
  return yXmlFragmentToProseMirrorRootNode(peer.getXmlFragment(COLLAB_FIELD), schema)
}

/** What a second editor holds after being handed the room's state. */
function throughRoom(markdown: string): PmNode {
  const state = EditorState.create({ schema, doc: asRendered(markdown) })
  const tr = state.tr
  reconcileCarried(tr, state.doc, schema, true)
  return state.apply(tr).doc.toJSON() as PmNode
}

/** What the first editor holds: the same file, opened without a room in the way. */
function direct(markdown: string): PmNode {
  const json = carry(mdastToProseMirror(parseDocument(markdown).root))
  return Node.fromJSON(schema, json).toJSON() as PmNode
}

function saved(document: PmNode, markdown: string): string {
  return serializeDocument(proseMirrorToMdast(document), parseDocument(markdown).envelope)
}

/**
 * The one thing a room does normalise. A colour span nested in another puts two marks of one
 * type on the same text; the editor's schema lets a colour replace a colour, and Yjs keeps
 * one value per mark type, so the inner colour survives and the outer one ends at its edge.
 * It renders the same — the inner colour always won — but the Markdown is written flat.
 */
function flattened(name: string, markdown: string): string {
  if (name !== 'colors.md') return markdown
  const nested = 'A [nested [emphasis]{.blue} span]{.gray}'
  expect(markdown).toContain(nested)
  return markdown.replace(nested, 'A [nested ]{.gray}[emphasis]{.blue}[ span]{.gray}')
}

describe('a document seeded into a room', () => {
  it.each(FIXTURE_NAMES)('%s is saved by the room as the Markdown it was opened from', (name) => {
    const markdown = readFileSync(join(FIXTURES, name), 'utf8')
    expect(saved(throughRoom(markdown), markdown)).toBe(flattened(name, markdown))
  })

  it.each(FIXTURE_NAMES.filter((name) => name !== 'colors.md'))(
    '%s comes back as the same document',
    (name) => {
      const markdown = readFileSync(join(FIXTURES, name), 'utf8')
      expect(throughRoom(markdown)).toEqual(direct(markdown))
    },
  )

  it('keeps marks on inline nodes that are not text', () => {
    const markdown = [
      'A **[[bold link]]** and *[[an italic one]]*.',
      '',
      'A [![badge](badge.png)](https://example.invalid/ci) that is a link.',
      '',
    ].join('\n')
    // The binding alone loses them: this is what would reach the file without `carried`.
    expect(saved(asRendered(markdown).toJSON() as PmNode, markdown)).not.toBe(markdown)
    expect(saved(throughRoom(markdown), markdown)).toBe(markdown)
  })

  it('keeps a plain item in a task list plain', () => {
    const markdown = '- [ ] to do\n- [x] done\n- just a note\n'
    const items = (document: PmNode) =>
      (document.content?.[0].content ?? []).map((item) => item.attrs?.checked)
    expect(items(asRendered(markdown).toJSON() as PmNode)).toEqual([false, true, false])
    expect(items(throughRoom(markdown))).toEqual([false, true, null])
    expect(saved(throughRoom(markdown), markdown)).toBe(markdown)
  })

  it('two editors seeding at once produce updates that must not both be applied', () => {
    // Why the server takes only the first seed: the two are separate insertions of the same
    // text, and a document holding both shows every block twice.
    const both = new Y.Doc()
    Y.applyUpdate(both, seedUpdate('# Title\n'))
    Y.applyUpdate(both, seedUpdate('# Title\n'))
    const doubled = yXmlFragmentToProseMirrorRootNode(both.getXmlFragment(COLLAB_FIELD), schema)
    expect(doubled.childCount).toBe(2)
  })
})

describe('the carried attribute', () => {
  it('is on every node type the binding would lose marks from', () => {
    const inlineNonText = Object.values(schema.nodes)
      .filter((type) => type.isInline && !type.isText)
      .map((type) => type.name)
    expect([...MARK_CARRIERS].sort()).toEqual(inlineNonText.sort())
    for (const name of CARRIED_TYPES) {
      expect(schema.nodes[name].spec.attrs).toHaveProperty('carried')
    }
  })

  it('covers every attribute a room would read back as its default', () => {
    // Every attribute whose default is not null: if the Markdown layer can set one to null,
    // the binding drops it and a room reads the default back. Each is either listed in
    // NULL_CARRIERS or named here with the reason it cannot be null — so a new attribute
    // fails this test until somebody has decided which it is.
    const neverNull: Record<string, string> = {
      'heading.level': 'always 1–6',
      'orderedList.start': 'always a number',
      'orderedList.type': 'set by the list, not by the file',
      'table.align': 'an array, possibly of nulls, never null itself',
      'tableCell.colspan': 'GFM has no spans',
      'tableCell.rowspan': 'GFM has no spans',
      'tableHeader.colspan': 'GFM has no spans',
      'tableHeader.rowspan': 'GFM has no spans',
      'wikiLink.value': 'the link as written, always a string',
      'wikiLink.target': 'always a string',
      'callout.kind': 'the directive name, always one of the four',
      'toggle.open': 'always a boolean',
    }
    const listed = Object.entries(NULL_CARRIERS).flatMap(([type, attrs]) =>
      attrs.map((attr) => `${type}.${attr}`),
    )
    const nonNullDefaults = Object.values(schema.nodes).flatMap((type) =>
      Object.entries(type.spec.attrs ?? {})
        .filter(([, spec]) => spec.default !== null && spec.default !== undefined)
        .map(([attr]) => `${type.name}.${attr}`),
    )
    const undecided = nonNullDefaults.filter(
      (attr) => !listed.includes(attr) && !(attr in neverNull),
    )
    expect(undecided).toEqual([])
  })

  it('follows a local change and repairs a change from the room', () => {
    const markdown = 'See **[[plan]]** today.\n'
    let state = EditorState.create({
      schema,
      doc: Node.fromJSON(schema, direct(markdown)),
      plugins: [carriedPlugin()],
    })
    const find = (): { node: Node; pos: number } => {
      const found: { node: Node; pos: number }[] = []
      state.doc.descendants((node, pos) => {
        if (node.type.name === 'wikiLink') found.push({ node, pos })
      })
      return found[0]
    }
    expect(find().node.attrs.carried).toEqual({ marks: [{ type: 'bold' }] })

    // Here: the bold is taken off the whole paragraph, so the attribute must follow.
    state = state.apply(state.tr.removeMark(0, state.doc.content.size, schema.marks.bold))
    expect(find().node.marks).toHaveLength(0)
    expect(find().node.attrs.carried).toBeNull()

    // From the room: the binding renders the node bare, with the attribute saying italic.
    const fromRoom = state.tr
      .setNodeAttribute(find().pos, 'carried', { marks: [{ type: 'italic' }] })
      .setMeta(ySyncPluginKey, { isChangeOrigin: true })
    state = state.apply(fromRoom)
    expect(find().node.marks.map((mark) => mark.type.name)).toEqual(['italic'])
    expect(saved(state.doc.toJSON() as PmNode, markdown)).toBe('See *[[plan]]* today.\n')
  })
})
