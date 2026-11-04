/**
 * The outline, off the editor's live document (block-editor spec §6).
 *
 * Not off the file: §6 asks for it to follow what is being typed, and the file is only
 * written every second or so. Positions rather than slugs identify a heading, because two
 * headings with the same text are ordinary in a spec and a slug would collide.
 */

import type { Node } from '@tiptap/pm/model'

export interface Heading {
  level: number
  text: string
  /** Document position of the heading node — stable for as long as the document is open. */
  pos: number
}

export function outlineOf(doc: Node): Heading[] {
  const headings: Heading[] = []
  doc.descendants((node, pos) => {
    if (node.type.name !== 'heading') return
    headings.push({ level: Number(node.attrs.level) || 1, text: node.textContent, pos })
    // Nothing inside a heading is a heading.
    return false
  })
  return headings
}
