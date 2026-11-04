/**
 * "Turn into" — the matrix from block-editor spec §4.6.
 *
 * One idea makes the whole table fall out: **every convertible block has *content blocks*,
 * and every target knows how to hold them.** A paragraph's content is itself; a callout's is
 * everything inside it except its title; a code block's is its text, split into lines. So a
 * conversion is "take the content out of one shape, put it into another", and the awkward
 * cases in §4.6 stop being special:
 *
 * - A container turned into a simple block **unwraps** rather than deletes, because its
 *   contents were the content all along.
 * - `→ code` loses the markup because text is what a code block can hold, not because
 *   anything is thrown away deliberately.
 * - `code → text` comes back unescaped for the same reason, from the other side.
 *
 * The replacement goes through `replaceRange`, which fits the content to the schema — that
 * is what lets a list item become a paragraph without this module knowing how to lift one
 * out of a list.
 */

import { Fragment, Slice, type Node, type Schema } from '@tiptap/pm/model'
import type { EditorState, Transaction } from '@tiptap/pm/state'

import type { BlockRef } from './blocks'

export type TransformTarget =
  | 'paragraph'
  | 'heading1'
  | 'heading2'
  | 'heading3'
  | 'blockquote'
  | 'bulletList'
  | 'orderedList'
  | 'taskList'
  | 'callout'
  | 'toggle'
  | 'codeBlock'

/**
 * §4.6: the table and the divider do not take part, and neither does anything the editor
 * does not model — an unsupported block is held, not reshaped (§3.4).
 */
const CONVERTIBLE = new Set([
  'paragraph',
  'heading',
  'blockquote',
  'listItem',
  'taskItem',
  'callout',
  'toggle',
  'codeBlock',
])

export function canTransform(node: Node): boolean {
  return CONVERTIBLE.has(node.type.name)
}

/** Which target a row already *is*, so the menu can show it as current. */
export function currentTarget(row: BlockRef): TransformTarget | null {
  const node = row.node
  switch (node.type.name) {
    case 'paragraph':
      return 'paragraph'
    case 'heading': {
      const level = Number(node.attrs.level)
      return level <= 3 ? (`heading${level}` as TransformTarget) : null
    }
    case 'blockquote':
      return 'blockquote'
    case 'callout':
      return 'callout'
    case 'toggle':
      return 'toggle'
    case 'codeBlock':
      return 'codeBlock'
    case 'listItem':
    case 'taskItem':
      return null // decided by the parent list, filled in by `currentListTarget`
    default:
      return null
  }
}

/** For a list item, the kind of list it sits in. */
export function currentListTarget(state: EditorState, row: BlockRef): TransformTarget | null {
  if (row.node.type.name !== 'listItem' && row.node.type.name !== 'taskItem') return null
  const parent = state.doc.resolve(row.pos).parent.type.name
  if (parent === 'bulletList' || parent === 'orderedList' || parent === 'taskList') {
    return parent as TransformTarget
  }
  return null
}

// ------------------------------------------------------------------ content in

/** The blocks a row is carrying, whatever shape it is currently in. */
function contentBlocksOf(node: Node, schema: Schema): Node[] {
  const paragraph = schema.nodes.paragraph

  switch (node.type.name) {
    case 'paragraph':
    case 'heading':
      return [paragraph.create(null, node.content)]

    case 'codeBlock':
      // §4.6: "inserted as text, without escaping". A line becomes a paragraph; blank lines
      // have no paragraph to become, and Markdown has no way to write one anyway.
      return node.textContent
        .split('\n')
        .filter((line) => line.trim().length > 0)
        .map((line) => paragraph.create(null, schema.text(line)))

    case 'blockquote':
    case 'listItem':
    case 'taskItem':
      return node.content.content.slice()

    case 'callout':
    case 'toggle':
      // The title belongs to the container, not to the content (§3.3).
      return node.content.content.filter((child) => child.type.name !== 'directiveTitle')

    default:
      return [node]
  }
}

/** Inline content for a target that holds inline, flattening anything that does not. */
function inlineOf(block: Node, schema: Schema): Fragment {
  if (block.isTextblock) return block.content
  const text = block.textContent
  return text ? Fragment.from(schema.text(text)) : Fragment.empty
}

// ------------------------------------------------------------------ content out

function build(target: TransformTarget, blocks: Node[], schema: Schema): Node[] {
  const nodes = schema.nodes
  const content = blocks.length ? blocks : [nodes.paragraph.create()]

  switch (target) {
    case 'paragraph':
      return content.map((block) =>
        block.type.name === 'paragraph' ? block : nodes.paragraph.create(null, inlineOf(block, schema)),
      )

    case 'heading1':
    case 'heading2':
    case 'heading3': {
      const level = Number(target.slice(-1))
      return content.map((block) => nodes.heading.create({ level }, inlineOf(block, schema)))
    }

    case 'blockquote':
      return [nodes.blockquote.create(null, Fragment.from(content))]

    case 'bulletList':
      return [nodes.bulletList.create(null, nodes.listItem.create(null, Fragment.from(content)))]

    case 'orderedList':
      return [
        nodes.orderedList.create({ start: 1 }, nodes.listItem.create(null, Fragment.from(content))),
      ]

    case 'taskList':
      return [
        nodes.taskList.create(
          null,
          nodes.taskItem.create({ checked: false }, Fragment.from(content)),
        ),
      ]

    case 'callout':
      return [
        nodes.callout.create(
          { kind: 'note' },
          Fragment.from([nodes.directiveTitle.create(), ...content]),
        ),
      ]

    case 'toggle':
      return [
        nodes.toggle.create(
          { open: false },
          Fragment.from([nodes.directiveTitle.create(), ...content]),
        ),
      ]

    case 'codeBlock': {
      // §4.6: markup is lost here, and no warning is shown — one `Ctrl+Z` takes it back.
      const text = content.map((block) => block.textContent).join('\n')
      return [nodes.codeBlock.create(null, text ? schema.text(text) : null)]
    }
  }
}

// ------------------------------------------------------------------ the operation

/**
 * Turn a row into `target`, in one transaction.
 *
 * Returns `false` when the row does not take part (§4.6) — the caller shows nothing rather
 * than a disabled entry, because the menu is built from what *is* possible.
 */
export function transformRow(
  tr: Transaction,
  state: EditorState,
  row: BlockRef,
  target: TransformTarget,
): boolean {
  if (!canTransform(row.node)) return false

  const schema = state.schema
  const isListItem = row.node.type.name === 'listItem' || row.node.type.name === 'taskItem'
  const listTarget = target === 'bulletList' || target === 'orderedList' || target === 'taskList'

  // A list item becoming another kind of list is a change to the *list*, not to the item:
  // wrapping it again would nest one list inside another.
  if (isListItem && listTarget) {
    const $row = state.doc.resolve(row.pos)
    const listPos = $row.before()
    const list = $row.parent
    const itemType = target === 'taskList' ? schema.nodes.taskItem : schema.nodes.listItem
    const items = list.content.content.map((item) =>
      itemType.create(
        target === 'taskList' ? { checked: item.attrs.checked ?? false } : null,
        item.content,
      ),
    )
    tr.replaceWith(listPos, listPos + list.nodeSize, schema.nodes[target].create(list.attrs, items))
    return true
  }

  const replacement = build(target, contentBlocksOf(row.node, schema), schema)
  // `replaceRange`, not `replaceWith`: it opens and closes the surrounding nodes as needed,
  // which is what lifts a list item out of its list when it stops being one.
  tr.replaceRange(row.pos, row.end, new Slice(Fragment.from(replacement), 0, 0))
  return true
}
