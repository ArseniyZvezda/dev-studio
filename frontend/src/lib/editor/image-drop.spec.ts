/**
 * Where a dropped picture lands (block-editor spec §3.4).
 *
 * Against the real schema and real documents, like `blocks.spec.ts`, and asserted as the
 * **Markdown that comes out**: the question this function answers is what the file will say,
 * and a position number says nothing about whether the list survived.
 */

import { getSchema } from '@tiptap/core'
import { Node as PmDoc } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'

import { parseDocument, serializeDocument } from '@/lib/md/document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'

import { createExtensions } from './extensions'
import { insertionFor } from './image-drop'
import { createSlashState } from './slash-menu'

const schema = getSchema(createExtensions({ slash: createSlashState() }))

function docOf(markdown: string): PmDoc {
  return PmDoc.fromJSON(schema, mdastToProseMirror(parseDocument(markdown).root) as never)
}

/** The position of the first character of `text`, which is where the drop is aimed. */
function inside(doc: PmDoc, text: string): number {
  let found = -1
  doc.descendants((node, pos) => {
    if (found < 0 && node.isText && node.text?.includes(text)) found = pos + 1
    return found < 0
  })
  if (found < 0) throw new Error(`No text matching ${text} in the document`)
  return found
}

/**
 * Drop a picture where `text` sits and read back the file it makes.
 *
 * The insert mirrors `receiveImageDrop`'s own — same position rule, same wrap-or-not — so a
 * change to either shows up here as different Markdown.
 */
function dropOn(markdown: string, text: string): string {
  const doc = docOf(markdown)
  const at = insertionFor(doc, text ? inside(doc, text) : 1)
  const image = schema.nodes.image.create({ src: 'shot.png', alt: 'shot', title: null })
  const content = doc.resolve(at).parent.inlineContent
    ? image
    : schema.nodes.paragraph.create(null, image)

  const after = EditorState.create({ doc }).tr.insert(at, content).doc
  return serializeDocument(proseMirrorToMdast(after.toJSON() as PmNode), {
    frontmatter: '',
    eol: '\n',
    finalNewline: true,
  })
}

describe('where a dropped picture lands', () => {
  it('goes on its own line under the paragraph, never inside the sentence', () => {
    expect(dropOn('# Title\n\nFirst paragraph.\n\nSecond paragraph.\n', 'First')).toBe(
      '# Title\n\nFirst paragraph.\n\n![shot](shot.png)\n\nSecond paragraph.\n',
    )
  })

  it('goes inside a bullet rather than between two of them', () => {
    // `row.end` for a list item is the position *between* items, and a paragraph inserted
    // there splits the list in half — which is exactly what this did before `acceptsBlocks`.
    expect(dropOn('- A bullet\n- Another bullet\n', 'A bullet')).toBe(
      '- A bullet\n\n  ![shot](shot.png)\n\n- Another bullet\n',
    )
  })

  it('fills an empty document rather than leaving a blank line above the picture', () => {
    expect(dropOn('', '')).toBe('![shot](shot.png)\n')
  })
})
