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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Awareness, applyAwarenessUpdate, encodeAwarenessUpdate } from 'y-protocols/awareness'
import * as Y from 'yjs'

import { createApiClient } from '@/api/client'
import type { DocumentFence, DocumentSaveReceipt, ProjectRemoteEditorMaterialization } from '@/api/types'
import { createExtensions } from '@/lib/editor/extensions'
import { createSlashState } from '@/lib/editor/slash-menu'
import { parseDocument, serializeDocument } from '@/lib/md/document'
import { proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'
import { ProjectSession } from '@/session/project-session'
import type { RemoteBindingTicket } from '@/stores/sync'
import { seedUpdate } from './seed'
import { fromBase64, openSession, toBase64, type CollabSession, type SocketLike } from './session'

const RELAY = Symbol('relay')

interface Member {
  name: string
  doc: Y.Doc
  awareness: Awareness
  editor: Editor
  element: HTMLElement
  /** Updates this member's document produced itself — what it would send to the room. */
  sent: number
  session?: CollabSession
}

let members: Member[] = []
let relayDocs: Y.Doc[] = []

/** Web Storage written by the code under test when jsdom's own is shadowed. */
const memoryStorage = new Map<string, string>()

beforeEach(() => {
  // Node >= 25 defines its own global localStorage, undefined without
  // --localstorage-file, and it shadows jsdom's here; the API client reads its token
  // from it. Give it an in-memory one only when the runtime has none.
  memoryStorage.clear()
  if (typeof window.localStorage === 'undefined') {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => memoryStorage.get(key) ?? null,
      setItem: (key: string, value: string) => { memoryStorage.set(key, String(value)) },
      removeItem: (key: string) => { memoryStorage.delete(key) },
      clear: () => memoryStorage.clear(),
      key: (index: number) => [...memoryStorage.keys()][index] ?? null,
      get length() { return memoryStorage.size },
    })
  }
})

afterEach(() => {
  for (const member of members) {
    member.editor.destroy()
    member.element.remove()
    if (member.session) member.session.close()
    else {
      member.awareness.destroy()
      member.doc.destroy()
    }
  }
  members = []
  for (const doc of relayDocs) doc.destroy()
  relayDocs = []
  vi.unstubAllGlobals()
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

describe('a clean open through real sessions (I16, dev spec §16.4)', () => {
  it('persists nothing at all: no update, no save request, no local Yjs store', async () => {
    // Markdown, saved by the backend, is the only persisted form of a room. A Yjs
    // persistence hook (IndexedDB, Web Storage) or a save on open/close would show here.
    const requests = vi.fn(async () => new Response(null, { status: 500 }))
    vi.stubGlobal('fetch', requests)
    const databases: string[] = []
    vi.stubGlobal('indexedDB', { open: (name: string) => { databases.push(name); throw new Error('no local Yjs store') } })
    const stored = vi.spyOn(Storage.prototype, 'setItem')
    try {
      const { frames, alice, bob } = await leasedRoom(DOCUMENT)
      await settle(50)
      expect(markdownOf(alice, DOCUMENT)).toBe(DOCUMENT)
      expect(markdownOf(bob, DOCUMENT)).toBe(DOCUMENT)
      for (const member of [alice, bob]) member.session!.close()
      await settle(50)
      expect(frames.filter(frame => frame.t === 'update')).toHaveLength(0)
      expect(requests).not.toHaveBeenCalled()
      expect(databases).toEqual([])
      expect(stored).not.toHaveBeenCalled()
      expect(memoryStorage.size).toBe(0)
    } finally {
      stored.mockRestore()
    }
  })
})

/** Local relay fixture only. It deliberately has no transition/ACK implementation:
 * the I5 Python fixtures own backend drain evidence, not this jsdom process. */
async function leasedRoom(source: string) {
  const binding = new ProjectSession('editor-project')
  binding.bind({ project_id: 'editor-project', epoch: 'runtime-epoch' })
  let generation = 7
  const sockets: SocketLike[] = []
  const frames: Array<Record<string, unknown>> = []
  const roomDoc = new Y.Doc()
  relayDocs.push(roomDoc)
  Y.applyUpdate(roomDoc, seedUpdate(source))
  for (const name of ['alice', 'bob']) {
    const ticket: RemoteBindingTicket = Object.freeze({
      runtime_scope: binding.scope!, backend_epoch: 'save-epoch', expected_binding_generation: generation,
      assertCurrent: () => { if (generation !== 7) throw new Error('stale remote binding') },
    })
    const fence: DocumentFence = Object.freeze({
      backend_epoch: ticket.backend_epoch, expected_binding_generation: ticket.expected_binding_generation,
      editor_id: `editor-${name}`, client_instance_id: name,
    })
    const socket: SocketLike = {
      onopen: null, onmessage: null, onclose: null,
      close: () => { const at = sockets.indexOf(socket); if (at >= 0) sockets.splice(at, 1) },
      send: (data) => {
        const frame = JSON.parse(data) as Record<string, unknown>
        frames.push(frame)
        if (frame.t === 'join') {
          socket.onmessage?.({ data: JSON.stringify({ ...binding.scope, t: 'joined',
            sid: `sid-${name}`, scribe: name === 'alice', seeded: true,
            state: toBase64(Y.encodeStateAsUpdate(roomDoc)), user: { id: name, name, color: '#3e63dd' } }) })
        } else if (frame.t === 'update' || frame.t === 'awareness') {
          if (frame.t === 'update') Y.applyUpdate(roomDoc, fromBase64(String(frame.update)))
          for (const peer of sockets) if (peer !== socket) {
            peer.onmessage?.({ data: JSON.stringify({ ...frame, ...binding.scope }) })
          }
        }
      },
    }
    sockets.push(socket)
    const opening = openSession({ path: 'same.md', session: binding, ticket, fence,
      seed: seedUpdate, socket: () => socket })
    socket.onopen!()
    const session = (await opening)!
    const element = document.body.appendChild(document.createElement('div'))
    const editor = new Editor({ element, extensions: createExtensions({
      slash: createSlashState(), collab: { session, onFirstRender: () => {} },
    }) })
    members.push({ name, session, doc: session.doc, awareness: session.awareness, editor, element, sent: 0 })
  }
  await settle()
  return { binding, frames, sockets, alice: members[0], bob: members[1],
    fence: () => { generation += 1 } }
}

describe('I8 real editors → captured final Markdown save', () => {
  it('issues each clean editor its own local materialization receipt with zero Markdown save writes', async () => {
    const { binding, frames, alice, bob } = await leasedRoom(DOCUMENT)
    const client = createApiClient(binding)
    const proofs = new Map<string, string>()
    const receipts = new Map<string, number>()
    const requests: Array<{ url: string; body: Record<string, unknown> }> = []
    let sequence = 200
    // Narrow synthetic local API model only. Python tests, not this fixture,
    // establish backend room/save anchors; this is not an interop claim.
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      url = new URL(url, 'http://localhost').pathname
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      requests.push({ url, body })
      expect(init.method).toBe('POST')
      const id = url.includes('/editor-alice/') ? 'editor-alice' : 'editor-bob'
      const lease = { editor_id: id, backend_epoch: 'save-epoch', binding_generation: 7,
        version: 0, revision: 2, document_path: 'same.md', client_instance_id: id.slice(7),
        lease_expires_at: Date.now() / 1000 + 30, dirty: false, confirmed: false, last_saved_version: null }
      if (url.endsWith('/materialization')) {
        expect(body).toEqual({ backend_epoch: 'save-epoch', expected_binding_generation: 7,
          document_path: 'same.md', client_instance_id: lease.client_instance_id,
          sid: `sid-${lease.client_instance_id}`, version: 0, revision: 2 })
        const proof_id = `proof-${id}`; proofs.set(id, proof_id)
        return new Response(JSON.stringify({ ...lease, proof_id }), { status: 200 })
      }
      if (url.endsWith('/materialization/confirm')) {
        expect(body.content).toBe(DOCUMENT)
        expect(body.proof_id).toBe(proofs.get(id)); proofs.delete(id)
        receipts.set(id, ++sequence)
        return new Response(JSON.stringify({ ...lease, confirmed: true,
          last_saved_version: sequence }), { status: 200 })
      }
      expect(url).toContain('/editor-ack')
      expect(body.last_saved_version).toBe(receipts.get(String(body.editor_id)))
      return new Response(null, { status: 204 })
    }))
    for (const member of [alice, bob]) {
      const doc = member.editor.state.doc
      const dom = member.editor.view.dom
      member.editor.setEditable(false)
      const tuple = Object.freeze({ backend_epoch: 'save-epoch', expected_binding_generation: 7,
        document_path: 'same.md', client_instance_id: member.name,
        sid: member.session!.sid, version: 0, revision: 2 }) satisfies ProjectRemoteEditorMaterialization
      const ticket = await client.materializeRemoteEditor(`editor-${member.name}`, tuple)
      const own = await client.confirmRemoteEditorMaterialization(`editor-${member.name}`,
        { ...tuple, proof_id: ticket.proof_id, content: markdownOf(member, DOCUMENT) })
      await client.ackRemoteEditor('change-old', { editor_id: own.editor_id, old_generation: 7,
        last_saved_version: own.last_saved_version! })
      expect(member.editor.state.doc).toBe(doc); expect(member.editor.view.dom).toBe(dom)
    }
    expect([...receipts.values()]).toEqual([201, 202])
    expect(requests).toHaveLength(6)
    expect(frames.filter(frame => frame.t === 'update')).toHaveLength(0)
    expect(frames.filter(frame => frame.t === 'join')).toHaveLength(2)
  })

  it('saves the non-scribe edit through the final scribe with a completed receipt and exact editor-ack', async () => {
    const { binding, frames, alice, bob } = await leasedRoom(DOCUMENT)
    const client = createApiClient(binding)
    const puts: Array<Record<string, unknown>> = []
    const acks: Array<Record<string, unknown>> = []
    let completeSave: (() => void) | undefined
    let receipt: DocumentSaveReceipt | undefined
    let sequence = 101
    let disk = DOCUMENT
    const ownReceipts = new Map<string, number>()
    const proofIds = new Map<string, string>()
    // This HTTP fixture checks client wire ordering/DTOs, NOT backend drain or
    // Remote ACK correctness. Its sequence intentionally differs from local v=1.
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      url = new URL(url, 'http://localhost').pathname
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      if (init.method === 'PUT') {
        expect(url).toContain('/api/projects/editor-project/docs/same.md')
        puts.push(body)
        return await new Promise<Response>((resolve) => {
          completeSave = () => {
            disk = String(body.content)
            receipt = { editor_id: 'editor-alice', backend_epoch: 'save-epoch', binding_generation: 7,
              version: 1, last_saved_version: 101 }
            resolve(new Response(JSON.stringify({ ...receipt, path: 'same.md', content: body.content }), { status: 200 }))
          }
        })
      }
      if (url.endsWith('/materialization') || url.endsWith('/materialization/confirm')) {
        const name = url.includes('/editor-alice/') ? 'alice' : 'bob'
        const id = `editor-${name}`
        expect(body).toMatchObject({ backend_epoch: 'save-epoch', expected_binding_generation: 7,
          document_path: 'same.md', client_instance_id: name, sid: `sid-${name}`, version: 1, revision: 4 })
        expect(receipt).toBeDefined() // The writer's final save completed first.
        const lease = { editor_id: id, backend_epoch: 'save-epoch', binding_generation: 7,
          document_path: 'same.md', client_instance_id: name, version: 1, revision: 4,
          lease_expires_at: Date.now() / 1000 + 30, dirty: true, confirmed: false, last_saved_version: null }
        if (url.endsWith('/materialization')) {
          const proof_id = `own-${name}`; proofIds.set(id, proof_id)
          return new Response(JSON.stringify({ ...lease, proof_id }), { status: 200 })
        }
        expect(body.proof_id).toBe(proofIds.get(id)); proofIds.delete(id)
        expect(body.content).toBe(disk)
        ownReceipts.set(id, ++sequence)
        return new Response(JSON.stringify({ ...lease, dirty: false, confirmed: true,
          last_saved_version: sequence }), { status: 200 })
      }
      expect(url).toContain('/remote/changes/change-old/editor-ack')
      acks.push(body)
      return body.old_generation === 7
        && body.last_saved_version === ownReceipts.get(String(body.editor_id))
        ? new Response(null, { status: 204 })
        : new Response(JSON.stringify({ code: 'invalid_editor_ack', details: {} }), { status: 409 })
    }))
    expect(alice.session!.scribe).toBe(true); expect(bob.session!.scribe).toBe(false)
    expect(frames.filter(frame => frame.t === 'update')).toHaveLength(0)
    expect(puts).toEqual([]) // Clean opens / constructing editors write no Markdown.
    bob.editor.commands.insertContentAt(find(bob, 'write the spec'), 'non-scribe ')
    await settle()
    const finalText = DOCUMENT.replace('write the spec', 'non-scribe write the spec')
    expect(markdownOf(alice, DOCUMENT)).toBe(finalText)
    expect(markdownOf(bob, DOCUMENT)).toBe(finalText)
    expect(frames.filter(frame => frame.t === 'update')).toHaveLength(1)
    // The parent owns freeze/flush policy. Here both are real, still-mounted
    // editors; final extraction happens before HTTP and before any destruction.
    alice.editor.setEditable(false); bob.editor.setEditable(false)
    const destroyed = [vi.spyOn(alice.doc, 'destroy'), vi.spyOn(bob.doc, 'destroy')]
    const fence = Object.freeze({ ...alice.session!.fence!, version: 1 })
    const saving = client.saveDoc('same.md', markdownOf(alice, DOCUMENT), 'user', null, alice.session!.sid, fence)
    expect(puts[0]).toMatchObject({ content: finalText, ...fence, collab_session: 'sid-alice' })
    expect(acks).toEqual([]); destroyed.forEach(spy => expect(spy).not.toHaveBeenCalled())
    expect(markdownOf(bob, DOCUMENT)).toBe(finalText)
    // A local version / saved boolean is not an ACK receipt.
    await expect(client.ackRemoteEditor('change-old', { editor_id: 'editor-alice', old_generation: 7,
      last_saved_version: 1 })).rejects.toMatchObject({ code: 'invalid_editor_ack' })
    completeSave!()
    const saved = await saving
    expect(saved.last_saved_version).toBe(101)
    // Scribe room/save evidence is not a peer's receipt. Both frozen editors
    // materialize their own rendered tree; each obtains a distinct own receipt.
    for (const member of [alice, bob]) {
      const tuple = Object.freeze({ backend_epoch: 'save-epoch', expected_binding_generation: 7,
        document_path: 'same.md', client_instance_id: member.name,
        sid: member.session!.sid, version: 1, revision: 4 }) satisfies ProjectRemoteEditorMaterialization
      const ticket = await client.materializeRemoteEditor(`editor-${member.name}`, tuple)
      const own = await client.confirmRemoteEditorMaterialization(`editor-${member.name}`,
        { ...tuple, proof_id: ticket.proof_id, content: markdownOf(member, DOCUMENT) })
      await client.ackRemoteEditor('change-old', { editor_id: own.editor_id, old_generation: 7,
        last_saved_version: own.last_saved_version! })
    }
    expect(acks.slice(-2)).toEqual([
      { editor_id: 'editor-alice', old_generation: 7, last_saved_version: 102 },
      { editor_id: 'editor-bob', old_generation: 7, last_saved_version: 103 },
    ])
    expect(puts).toHaveLength(1); destroyed.forEach(spy => expect(spy).not.toHaveBeenCalled())
    expect(frames.filter(frame => frame.t === 'join')).toHaveLength(2)
    expect(markdownOf(alice, DOCUMENT)).toBe(finalText)
    expect(markdownOf(bob, DOCUMENT)).toBe(finalText)
  })

  it('preserves both rendered documents on a typed final-save refusal and fences late room callbacks without auto-joining', async () => {
    const { binding, frames, alice, bob, sockets, fence: advanceGeneration } = await leasedRoom('Keep this.\n')
    const client = createApiClient(binding)
    const fetch = vi.fn(async () => new Response(JSON.stringify({ code: 'stale_binding_generation', details: {} }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    bob.editor.commands.insertContentAt(find(bob, 'Keep'), 'Unsaved ')
    await settle()
    const screen = markdownOf(alice, 'Keep this.\n')
    const destroyed = [vi.spyOn(alice.doc, 'destroy'), vi.spyOn(bob.doc, 'destroy')]
    const pending = client.saveDoc('same.md', screen, 'user', null, alice.session!.sid,
      { ...alice.session!.fence!, version: 1 })
    advanceGeneration()
    await expect(pending).rejects.toMatchObject({ isFenceConflict: true, isDiskConflict: false })
    const sent = frames.length
    for (const socket of [...sockets]) socket.onmessage?.({ data: JSON.stringify({ t: 'scribe', ...binding.scope }) })
    expect(bob.session!.scribe).toBe(false)
    expect(markdownOf(alice, screen)).toBe(screen); expect(markdownOf(bob, screen)).toBe(screen)
    destroyed.forEach(spy => expect(spy).not.toHaveBeenCalled())
    expect(frames).toHaveLength(sent); expect(frames.filter(frame => frame.t === 'join')).toHaveLength(2)
    expect(fetch).toHaveBeenCalledOnce() // No overwrite, adopt, retry, or target lookup.
  })
})
