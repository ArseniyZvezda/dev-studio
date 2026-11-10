// @vitest-environment jsdom
/**
 * Two real editors in one room (dev spec §16.4).
 *
 * The other specs here argue about the pieces — the seed, the session, the `carried`
 * attribute. This one puts them together the way the app does: two Tiptap editors built
 * from the app's own extension list, each bound to its own Yjs document, with updates
 * passed between the two documents as the server would pass them. What is asserted is what
 * a person would see and what would reach their disk.
 *
 * The one file in the suite that needs a DOM, and only because an editor does; everything
 * about the *document* is still checked without one, next door.
 */

import { Editor } from '@tiptap/core'
import { TextSelection } from '@tiptap/pm/state'
import { afterEach, describe, expect, it } from 'vitest'
import { Awareness, applyAwarenessUpdate, encodeAwarenessUpdate } from 'y-protocols/awareness'
import * as Y from 'yjs'

import { createExtensions } from '@/lib/editor/extensions'
import { createSlashState } from '@/lib/editor/slash-menu'
import { parseDocument, serializeDocument } from '@/lib/md/document'
import { proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'
import { seedUpdate } from './seed'
import type { CollabSession } from './session'

const RELAY = Symbol('relay')

interface Member {
  name: string
  doc: Y.Doc
  awareness: Awareness
  editor: Editor
  element: HTMLElement
  /** Updates this member's document produced itself — what it would send to the room. */
  sent: number
}

let members: Member[] = []

afterEach(() => {
  for (const member of members) {
    member.editor.destroy()
    member.element.remove()
    member.awareness.destroy()
    member.doc.destroy()
  }
  members = []
})

const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms))

/** A room holding *markdown*, with one editor per name, all drawn and in step. */
async function room(markdown: string, ...names: string[]): Promise<Member[]> {
  const seed = seedUpdate(markdown)
  const docs = names.map(() => new Y.Doc())
  // The server: every update a document produces itself is applied to all the others.
  docs.forEach((doc, index) => {
    doc.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin === RELAY) return
      members[index].sent += 1
      docs.forEach((other) => {
        if (other !== doc) Y.applyUpdate(other, update, RELAY)
      })
    })
  })

  const awarenesses = docs.map((doc) => new Awareness(doc))
  // Cursors and names travel the same way, relayed without being read.
  awarenesses.forEach((awareness) => {
    awareness.on(
      'update',
      (changes: { added: number[]; updated: number[]; removed: number[] }, origin: unknown) => {
        if (origin === RELAY) return
        const update = encodeAwarenessUpdate(awareness, [
          ...changes.added,
          ...changes.updated,
          ...changes.removed,
        ])
        awarenesses.forEach((other) => {
          if (other !== awareness) applyAwarenessUpdate(other, update, RELAY)
        })
      },
    )
  })

  members = names.map((name, index) => {
    const doc = docs[index]
    Y.applyUpdate(doc, seed, RELAY)
    const awareness = awarenesses[index]
    const session = {
      doc,
      awareness,
      user: { id: name, name, color: '#3e63dd' },
    } as unknown as CollabSession
    // In the page, as an editor is: a cursor is only published by one that can have focus.
    const element = document.body.appendChild(document.createElement('div'))
    const editor = new Editor({
      element,
      extensions: createExtensions({
        slash: createSlashState(),
        collab: { session, onFirstRender: () => {} },
      }),
    })
    return { name, doc, awareness, editor, element, sent: 0 }
  })
  // The binding draws the room's document a tick after the editor is built.
  await settle()
  return members
}

function markdownOf(member: Member, source: string): string {
  return serializeDocument(
    proseMirrorToMdast(member.editor.getJSON() as PmNode),
    parseDocument(source).envelope,
  )
}

/** Where *text* starts in the member's document. */
function find(member: Member, text: string): number {
  let at = -1
  member.editor.state.doc.descendants((node, pos) => {
    if (at < 0 && node.isText && node.text?.includes(text)) at = pos + node.text.indexOf(text)
  })
  if (at < 0) throw new Error(`"${text}" is not in ${member.name}'s document`)
  return at
}

const DOCUMENT = [
  '# Dash ability',
  '',
  'Owned by **[[marina]]**, see *the plan* first.',
  '',
  '- [ ] write the spec',
  '- [x] agree the name',
  '- a note, not a task',
  '',
  '| Stat | Value |',
  '| :--- | ---: |',
  '| Range | 4 |',
  '',
].join('\n')

describe('two editors in one room', () => {
  it('open the document without writing anything', async () => {
    const [alice, bob] = await room(DOCUMENT, 'alice', 'bob')
    // What each would save is the file as it was opened…
    expect(markdownOf(alice, DOCUMENT)).toBe(DOCUMENT)
    expect(markdownOf(bob, DOCUMENT)).toBe(DOCUMENT)
    // …and neither has produced a single update. An update here would be the room changing
    // by itself on open, which the scribe would dutifully save: a file rewritten because
    // somebody looked at it (block-editor spec §8.2).
    await settle(50)
    expect([alice.sent, bob.sent]).toEqual([0, 0])
  })

  it('see each other type, and would save the same Markdown', async () => {
    const [alice, bob] = await room(DOCUMENT, 'alice', 'bob')
    alice.editor.commands.insertContentAt(find(alice, 'plan'), 'real ')
    bob.editor.commands.insertContentAt(find(bob, 'write the spec'), 'first ')
    await settle()

    const expected = DOCUMENT.replace('*the plan*', '*the real plan*').replace(
      'write the spec',
      'first write the spec',
    )
    expect(markdownOf(alice, DOCUMENT)).toBe(expected)
    expect(markdownOf(bob, DOCUMENT)).toBe(expected)
    // Editing around them took nothing from the nodes the binding cannot carry by itself:
    // the bold on the wiki link, and the item that has no checkbox.
    expect(expected).toContain('**[[marina]]**')
    expect(expected).toContain('- a note, not a task')
  })

  it('carry a mark put on a wiki link to the other editor', async () => {
    const source = 'Ask [[marina]] today.\n'
    const [alice, bob] = await room(source, 'alice', 'bob')
    // Alice makes the whole sentence bold — the link in the middle included.
    alice.editor
      .chain()
      .setTextSelection({ from: 1, to: alice.editor.state.doc.content.size - 1 })
      .toggleBold()
      .run()
    await settle()
    expect(markdownOf(alice, source)).toBe('**Ask [[marina]] today.**\n')
    expect(markdownOf(bob, source)).toBe('**Ask [[marina]] today.**\n')

    // And Bob takes it off again.
    bob.editor
      .chain()
      .setTextSelection({ from: 1, to: bob.editor.state.doc.content.size - 1 })
      .toggleBold()
      .run()
    await settle()
    expect(markdownOf(alice, source)).toBe(source)
    expect(markdownOf(bob, source)).toBe(source)
  })

  it('undo their own edits and nobody else’s', async () => {
    const source = 'One.\n\nTwo.\n'
    const [alice, bob] = await room(source, 'alice', 'bob')
    alice.editor.commands.insertContentAt(find(alice, 'One'), 'A ')
    // Past the binding's capture window, so the two edits are not one undo step.
    await settle(600)
    bob.editor.commands.insertContentAt(find(bob, 'Two'), 'B ')
    await settle()
    expect(markdownOf(alice, source)).toBe('A One.\n\nB Two.\n')

    alice.editor.commands.undo()
    await settle()
    expect(markdownOf(alice, source)).toBe('One.\n\nB Two.\n')
    expect(markdownOf(bob, source)).toBe('One.\n\nB Two.\n')
  })

  it('show each other’s cursors without putting them in the document', async () => {
    const source = 'Somewhere in here.\n'
    const [alice, bob] = await room(source, 'alice', 'bob')
    // Focus and a selection, set the way a click does but without the scroll into view
    // that follows a command — there is no layout here to scroll.
    alice.editor.view.focus()
    alice.editor.view.dispatch(
      alice.editor.state.tr.setSelection(
        TextSelection.create(alice.editor.state.doc, find(alice, 'in here')),
      ),
    )
    await settle()

    const caret = bob.editor.view.dom.querySelector('.collab-caret')
    expect(caret?.querySelector('.collab-caret-label')?.textContent).toBe('alice')
    expect(markdownOf(bob, source)).toBe(source)
    expect([alice.sent, bob.sent]).toEqual([0, 0])
  })
})
