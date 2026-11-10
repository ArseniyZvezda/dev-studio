/**
 * Turning a document's Markdown into the Yjs document a live room starts from (dev spec §16.4).
 *
 * Markdown is what is stored and what every Studio has on disk; Yjs is what a room edits. The
 * first editor to arrive builds the second from the first, here, with the same parser the
 * editor opens a file with — so a room's document is exactly the document a lone editor would
 * have been showing, and what the room's scribe writes back is exactly what that editor would
 * have saved.
 */

import { getSchema } from '@tiptap/core'
import type { Schema } from '@tiptap/pm/model'
import { prosemirrorJSONToYDoc } from '@tiptap/y-tiptap'
import * as Y from 'yjs'

import { createExtensions } from '@/lib/editor/extensions'
import { createSlashState } from '@/lib/editor/slash-menu'
import { parseDocument } from '@/lib/md/document'
import { mdastToProseMirror } from '@/lib/md/prosemirror'
import { CarriedState, COLLAB_FIELD, carry } from './carried'

let schema: Schema | null = null

/**
 * The schema of an editor that is in a room, built once.
 *
 * The lone editor's schema plus the `carried` attribute (`carried.ts`) — the same nodes and
 * marks the editor's own extension list gives it when it is handed a room.
 */
export function roomSchema(): Schema {
  schema ??= getSchema([...createExtensions({ slash: createSlashState() }), CarriedState])
  return schema
}

/** A Yjs document holding *markdown* as the editor would open it. */
export function seedDocument(markdown: string): Y.Doc {
  // `carry` first: what the binding would drop on the way in is put where it will not.
  const content = carry(mdastToProseMirror(parseDocument(markdown).root))
  return prosemirrorJSONToYDoc(roomSchema(), content, COLLAB_FIELD)
}

/**
 * The update that seeds an empty room with *markdown*.
 *
 * Built in a throwaway document with a client id of its own, never in the session's: if the
 * server turns the offer down — somebody else seeded first — nothing of it may be left in the
 * document the session goes on to edit.
 */
export function seedUpdate(markdown: string): Uint8Array {
  const doc = seedDocument(markdown)
  try {
    return Y.encodeStateAsUpdate(doc)
  } finally {
    doc.destroy()
  }
}
