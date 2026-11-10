/**
 * What the Yjs binding would otherwise drop from a document (dev spec §16.4).
 *
 * A live room edits a Yjs copy of the document, and the binding between the editor and Yjs
 * (`@tiptap/y-tiptap`) does not carry everything the editor's document holds:
 *
 * - **Marks on inline nodes that are not text.** Yjs formatting belongs to text. A wiki link
 *   inside bold, a picture inside a link, a line break inside a coloured span — each is a
 *   node with marks, and comes back from the room without them. On disk that is
 *   `**[[plan]]**` turning into `[[plan]]`, and `[![badge](b.png)](https://…)` losing its
 *   link.
 * - **`null` where the schema's default is something else.** A `null` attribute is not
 *   written to Yjs at all, and is read back as the default. A plain item in a task list is
 *   `checked: null` (see `MarkdownTaskItem`), the default is `false`, and so it would come
 *   back with an empty checkbox the file never had.
 *
 * Both are things the Markdown layer goes out of its way to keep (block-editor spec §8), so a
 * room must not lose them. They ride through Yjs in one ordinary attribute, `carried`, on the
 * nodes concerned — an attribute is something the binding does keep — and a plugin holds the
 * attribute and the real thing in step: after a local change the attribute is written from
 * the node, after a change that came from the room the node is put right from the attribute.
 *
 * The attribute exists only in the schema of an editor that is in a room. A lone editor's
 * document never has it, and neither does anything on disk.
 */

import { Extension } from '@tiptap/core'
import type { Node as PmDocNode, Schema } from '@tiptap/pm/model'
import { Plugin, PluginKey, type Transaction } from '@tiptap/pm/state'
import { ySyncPluginKey } from '@tiptap/y-tiptap'

/** The name of the fragment inside the Yjs document — Tiptap's own default, stated once. */
export const COLLAB_FIELD = 'default'

/** Inline nodes that are not text: the ones whose marks Yjs has nowhere to put. */
export const MARK_CARRIERS = ['wikiLink', 'image', 'unsupportedInline', 'hardBreak'] as const

/**
 * Attributes where `null` is a value the Markdown layer writes and the schema's default is
 * not `null` — by node type. One entry today; the spec beside this file fails if the schema
 * grows another such attribute without it being listed here.
 */
export const NULL_CARRIERS: Readonly<Record<string, readonly string[]>> = {
  taskItem: ['checked'],
}

export const CARRIED_TYPES: readonly string[] = [...MARK_CARRIERS, ...Object.keys(NULL_CARRIERS)]

const MARK_CARRIER_SET: ReadonlySet<string> = new Set(MARK_CARRIERS)

export interface Carried {
  /** The node's marks, as ProseMirror serialises them. */
  marks?: unknown[]
  /** Names of the attributes that are `null`. */
  nulls?: string[]
}

interface JsonNode {
  type: string
  attrs?: Record<string, unknown>
  marks?: unknown[]
  content?: JsonNode[]
  text?: string
}

/** What a node of *type* with these attributes and marks has to carry — `null` for nothing. */
function carriedFor(
  type: string,
  attrs: Record<string, unknown> | undefined,
  marks: readonly unknown[] | undefined,
): Carried | null {
  const carried: Carried = {}
  if (MARK_CARRIER_SET.has(type) && marks?.length) carried.marks = [...marks]
  const nulls = (NULL_CARRIERS[type] ?? []).filter((name) => attrs?.[name] === null)
  if (nulls.length) carried.nulls = nulls
  return carried.marks || carried.nulls ? carried : null
}

/**
 * A document as JSON with every `carried` attribute written from the node it sits on — what
 * a document is passed through before it seeds a room.
 */
export function carry<T extends JsonNode>(node: T): T {
  const next: JsonNode = { ...node }
  if (node.content) next.content = node.content.map(carry)
  if (CARRIED_TYPES.includes(node.type)) {
    next.attrs = { ...node.attrs, carried: carriedFor(node.type, node.attrs, node.marks) }
  }
  return next as T
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a ?? null) === JSON.stringify(b ?? null)

/**
 * The steps that bring the nodes of *doc* and their `carried` attributes back in step.
 *
 * `fromRoom` says which of the two is right. A change that came from the room was rendered
 * by the binding, which has just dropped the marks and the `null`s — so the attribute is the
 * truth and the node is repaired from it. A change made here was made to the node — so the
 * node is the truth and the attribute is rewritten, which is what sends it to everyone else.
 *
 * Every step replaces a node with one of the same size, so positions read from *doc* stay
 * valid for the whole pass.
 */
export function reconcileCarried(tr: Transaction, doc: PmDocNode, schema: Schema, fromRoom: boolean): void {
  doc.descendants((node, pos) => {
    const type = node.type.name
    if (!CARRIED_TYPES.includes(type)) return
    const carried = (node.attrs.carried ?? null) as Carried | null

    if (!fromRoom) {
      const actual = carriedFor(type, node.attrs, node.marks.map((mark) => mark.toJSON()))
      if (!same(actual, carried)) tr.setNodeAttribute(pos, 'carried', actual)
      return
    }

    let attrs: Record<string, unknown> = node.attrs
    for (const name of carried?.nulls ?? []) {
      if ((NULL_CARRIERS[type] ?? []).includes(name) && attrs[name] !== null) {
        attrs = { ...attrs, [name]: null }
      }
    }
    let marks = node.marks
    if (MARK_CARRIER_SET.has(type)) {
      const wanted = carried?.marks ?? []
      if (!same(wanted, node.marks.map((mark) => mark.toJSON()))) {
        try {
          marks = wanted.map((mark) => schema.markFromJSON(mark))
        } catch {
          // A mark this build's schema does not have — written by a newer Studio in the
          // room. The node keeps what it has rather than the room losing an editor.
        }
      }
    }
    if (attrs !== node.attrs || marks !== node.marks) tr.setNodeMarkup(pos, null, attrs, marks)
  })
}

export const carriedPluginKey = new PluginKey('collabCarried')

/** Whether the binding dispatched this transaction to show a change made in the room. */
function cameFromRoom(transaction: Transaction): boolean {
  return Boolean((transaction.getMeta(ySyncPluginKey) as { isChangeOrigin?: boolean } | undefined)?.isChangeOrigin)
}

/**
 * The `carried` attribute, and the plugin that keeps it honest.
 *
 * Before `Collaboration` in the extension list, so its attribute is part of the schema the
 * binding is built over. The repairs are not undo steps of their own: they describe the
 * edit that caused them and are undone with it.
 */
export const CarriedState = Extension.create({
  name: 'collabCarried',

  addGlobalAttributes() {
    return [
      {
        types: [...CARRIED_TYPES],
        attributes: { carried: { default: null, rendered: false } },
      },
    ]
  },

  addProseMirrorPlugins() {
    return [carriedPlugin()]
  },
})

/** The plugin on its own — it needs a state, not an editor, which is how it is tested. */
export function carriedPlugin(): Plugin {
  return new Plugin({
    key: carriedPluginKey,
    appendTransaction: (transactions, _oldState, newState) => {
      const changed = transactions.filter((transaction) => transaction.docChanged)
      if (!changed.length) return null
      // Its own repairs are final — acting on them again would only undo them.
      if (changed.every((transaction) => transaction.getMeta(carriedPluginKey))) return null
      const tr = newState.tr
      reconcileCarried(tr, newState.doc, newState.schema, changed.some(cameFromRoom))
      if (!tr.steps.length) return null
      return tr.setMeta(carriedPluginKey, true).setMeta('addToHistory', false)
    },
  })
}
