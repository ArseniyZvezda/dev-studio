/**
 * The block's context menu, as data (block-editor spec §4.5).
 *
 * The menu is *built from the row*, not rendered by a component that knows every block type:
 * `menuFor` returns the actions and the properties that apply, and the popup renders whatever
 * it is handed. That is what keeps §4.5's rule enforceable — **only what Markdown can express
 * is a block property.** Line wrapping in a code block and the font size are settings of the
 * editor, and there is nowhere in this file to put them, which is the point.
 *
 * Properties are edited in place (a select, a field, a switch) and never open a dialog,
 * because every one of them is a single value and a dialog for a single value is a ceremony.
 */

import type { Editor } from '@tiptap/core'
import type { Node } from '@tiptap/pm/model'

import { t, type MessageKey } from '@/i18n'
import { rangeOf, rowAt, type BlockRef } from './blocks'
import { setBlockSelection } from './block-selection'
import { serializeDocument } from '@/lib/md/document'
import { proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'
import { CODE_LANGUAGES } from './languages'
import { canTransform, currentListTarget, currentTarget, transformRow, type TransformTarget } from './transform'

export interface MenuAction {
  id: string
  label: string
  shortcut?: string
  danger?: boolean
  run: () => void
}

export type MenuProperty =
  | { kind: 'select'; id: string; label: string; value: string; options: { value: string; label: string }[]; set: (value: string) => void }
  | { kind: 'search'; id: string; label: string; value: string; options: { value: string; label: string }[]; set: (value: string) => void }
  | { kind: 'number'; id: string; label: string; value: number; min: number; set: (value: number) => void }
  | { kind: 'text'; id: string; label: string; value: string; placeholder: string; set: (value: string) => void }
  | { kind: 'switch'; id: string; label: string; value: boolean; set: (value: boolean) => void }
  | { kind: 'buttons'; id: string; label: string; buttons: { id: string; label: string; run: () => void }[] }

export interface BlockMenuModel {
  row: BlockRef
  title: string
  /** Empty when the block does not take part in §4.6 — an unsupported block, a table. */
  transforms: { target: TransformTarget; label: string; current: boolean }[]
  actions: MenuAction[]
  properties: MenuProperty[]
  /** Copy / Cut / Paste, present only with a live text selection inside the block (§4.5). */
  clipboard: MenuAction[]
}

/**
 * Message keys, not strings: the menu is built each time it opens, and `t` is called then, so
 * it speaks the language on screen rather than the one this module was loaded in.
 */
export const TRANSFORM_LABELS: Record<TransformTarget, MessageKey> = {
  paragraph: 'editor.blocks.paragraph',
  heading1: 'editor.blocks.heading1',
  heading2: 'editor.blocks.heading2',
  heading3: 'editor.blocks.heading3',
  blockquote: 'editor.blocks.blockquote',
  bulletList: 'editor.blocks.bulletList',
  orderedList: 'editor.blocks.orderedList',
  taskList: 'editor.blocks.taskList',
  callout: 'editor.blocks.callout',
  toggle: 'editor.blocks.toggle',
  codeBlock: 'editor.blocks.codeBlock',
}

const BLOCK_TITLES: Record<string, MessageKey> = {
  paragraph: 'editor.blocks.paragraph',
  heading: 'editor.blocks.heading',
  blockquote: 'editor.blocks.blockquote',
  listItem: 'editor.blocks.listItem',
  taskItem: 'editor.blocks.taskItem',
  codeBlock: 'editor.blocks.codeBlock',
  table: 'editor.blocks.table',
  horizontalRule: 'editor.blocks.horizontalRule',
  callout: 'editor.blocks.callout',
  toggle: 'editor.blocks.toggle',
  unsupportedBlock: 'editor.blocks.unsupported',
}

/**
 * The row's own Markdown — what §4.5's "Copy as markdown" puts on the clipboard.
 *
 * Takes the document rather than the editor, because it has nothing to do with one: it is
 * the same serialization a save performs, over a slice, and that makes it checkable without
 * a browser.
 */
export function markdownOfRow(doc: Node, row: BlockRef): string {
  const slice = doc.slice(row.pos, row.end)

  // A list item on its own is not a document. Markdown has no way to write one without the
  // list around it, and the converter — correctly — has no case for a bare item, so it would
  // serialize to nothing at all. Put it back in a list of its own kind.
  const name = row.node.type.name
  const content =
    name === 'listItem' || name === 'taskItem'
      ? [
          {
            type: doc.resolve(row.pos).parent.type.name,
            content: slice.content.toJSON() as PmNode[],
          } as PmNode,
        ]
      : (slice.content.toJSON() as PmNode[])

  const document: PmNode = { type: 'doc', content }
  return serializeDocument(proseMirrorToMdast(document), {
    frontmatter: '',
    eol: '\n',
    finalNewline: false,
  })
}

function updateAttrs(editor: Editor, pos: number, attrs: Record<string, unknown>): void {
  const { state, view } = editor
  const node = state.doc.nodeAt(pos)
  if (!node) return
  view.dispatch(state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, ...attrs }))
  editor.commands.focus()
}

// ------------------------------------------------------------------ properties

function propertiesFor(editor: Editor, row: BlockRef): MenuProperty[] {
  const node = row.node
  const { state } = editor

  switch (node.type.name) {
    case 'heading':
      return [
        {
          kind: 'select',
          id: 'level',
          label: t('editor.blockMenu.level'),
          value: String(node.attrs.level),
          // All six, unlike the insert menu: §3.1 hides 4–6 from *choosing* a block, but a
          // document that already has one needs a way to change it.
          options: [1, 2, 3, 4, 5, 6].map((level) => ({
            value: String(level),
            label: t('editor.blockMenu.headingLevel', { level }),
          })),
          set: (value) => updateAttrs(editor, row.pos, { level: Number(value) }),
        },
      ]

    case 'listItem':
    case 'taskItem': {
      const kind = currentListTarget(state, row)
      if (!kind) return []
      return [
        {
          kind: 'select',
          id: 'listKind',
          label: t('editor.blockMenu.list'),
          value: kind,
          options: (['bulletList', 'orderedList', 'taskList'] as TransformTarget[]).map((target) => ({
            value: target,
            label: t(TRANSFORM_LABELS[target]),
          })),
          set: (value) => applyTransform(editor, row, value as TransformTarget),
        },
        ...(kind === 'orderedList' ? [startNumberProperty(editor, row)] : []),
      ]
    }

    case 'codeBlock':
      return [
        {
          kind: 'search',
          id: 'language',
          label: t('editor.blockMenu.language'),
          value: String(node.attrs.language ?? ''),
          options: CODE_LANGUAGES,
          set: (value) => updateAttrs(editor, row.pos, { language: value || null }),
        },
      ]

    case 'callout':
      return [
        {
          kind: 'select',
          id: 'kind',
          label: t('editor.blockMenu.calloutType'),
          value: String(node.attrs.kind ?? 'note'),
          options: [
            { value: 'note', label: t('editor.callouts.note') },
            { value: 'tip', label: t('editor.callouts.tip') },
            { value: 'warning', label: t('editor.callouts.warning') },
            { value: 'danger', label: t('editor.callouts.danger') },
          ],
          set: (value) => updateAttrs(editor, row.pos, { kind: value }),
        },
      ]

    case 'toggle':
      return [
        {
          kind: 'text',
          id: 'title',
          label: t('editor.blockMenu.title'),
          value: node.firstChild?.type.name === 'directiveTitle' ? node.firstChild.textContent : '',
          placeholder: t('editor.placeholder.title'),
          set: (value) => setDirectiveTitle(editor, row, value),
        },
        {
          kind: 'switch',
          id: 'open',
          label: t('editor.blockMenu.openByDefault'),
          value: node.attrs.open === true,
          // §3.3: this is the *file's* default. Clicking a toggle open changes nothing here.
          set: (value) => updateAttrs(editor, row.pos, { open: value }),
        },
      ]

    case 'table':
      return tableProperties(editor, row)

    default:
      return []
  }
}

function startNumberProperty(editor: Editor, row: BlockRef): MenuProperty {
  const listPos = editor.state.doc.resolve(row.pos).before()
  const list = editor.state.doc.nodeAt(listPos)
  return {
    kind: 'number',
    id: 'start',
    label: t('editor.blockMenu.startsAt'),
    value: Number(list?.attrs.start ?? 1),
    min: 0,
    set: (value) => updateAttrs(editor, listPos, { start: Math.max(0, Math.trunc(value)) }),
  }
}

function setDirectiveTitle(editor: Editor, row: BlockRef, value: string): void {
  const { state, view } = editor
  const node = state.doc.nodeAt(row.pos)
  if (!node || node.firstChild?.type.name !== 'directiveTitle') return
  const from = row.pos + 1
  const to = from + node.firstChild.nodeSize
  const title = state.schema.nodes.directiveTitle.create(
    null,
    value ? state.schema.text(value) : null,
  )
  view.dispatch(state.tr.replaceWith(from, to - 1, title))
}

/**
 * §3.1 for a table: column alignment, and adding or removing rows and columns.
 *
 * Alignment belongs to the *column* in GFM, so it is stored on the table and edited for
 * whichever column the caret is in — there is no per-cell answer to give.
 */
function tableProperties(editor: Editor, row: BlockRef): MenuProperty[] {
  const column = columnAt(editor, row)
  const align = (row.node.attrs.align as (string | null)[]) ?? []

  const properties: MenuProperty[] = []

  if (column !== null) {
    properties.push({
      kind: 'select',
      id: 'align',
      label: t('editor.blockMenu.table.column', { number: column + 1 }),
      value: align[column] ?? '',
      options: [
        { value: '', label: t('editor.blockMenu.table.alignDefault') },
        { value: 'left', label: t('editor.blockMenu.table.alignLeft') },
        { value: 'center', label: t('editor.blockMenu.table.alignCenter') },
        { value: 'right', label: t('editor.blockMenu.table.alignRight') },
      ],
      set: (value) => {
        const next = [...align]
        while (next.length <= column) next.push(null)
        next[column] = value || null
        updateAttrs(editor, row.pos, { align: next })
      },
    })
  }

  properties.push(
    {
      kind: 'buttons',
      id: 'rows',
      label: t('editor.blockMenu.table.row'),
      buttons: [
        { id: 'above', label: t('editor.blockMenu.table.above'), run: () => editor.chain().focus().addRowBefore().run() },
        { id: 'below', label: t('editor.blockMenu.table.below'), run: () => editor.chain().focus().addRowAfter().run() },
        { id: 'delete', label: t('common.delete'), run: () => editor.chain().focus().deleteRow().run() },
      ],
    },
    {
      kind: 'buttons',
      id: 'columns',
      label: t('editor.blockMenu.table.columns'),
      buttons: [
        { id: 'before', label: t('editor.blockMenu.table.before'), run: () => editor.chain().focus().addColumnBefore().run() },
        { id: 'after', label: t('editor.blockMenu.table.after'), run: () => editor.chain().focus().addColumnAfter().run() },
        { id: 'delete', label: t('common.delete'), run: () => editor.chain().focus().deleteColumn().run() },
      ],
    },
  )

  return properties
}

/** Which column the caret is in, or null when it is not in this table. */
function columnAt(editor: Editor, row: BlockRef): number | null {
  const { selection, doc } = editor.state
  if (selection.from < row.pos || selection.from > row.end) return null
  const $pos = doc.resolve(selection.from)
  for (let depth = $pos.depth; depth > 0; depth -= 1) {
    if ($pos.node(depth).type.name === 'tableRow') return $pos.index(depth)
  }
  return null
}

// ------------------------------------------------------------------ actions

function applyTransform(editor: Editor, row: BlockRef, target: TransformTarget): void {
  const { state, view } = editor
  // The row is re-read from the live document: the menu may have been open while something
  // else changed it, and a stale position would rewrite the wrong block.
  const current = rowAt(state.doc, row.pos)
  if (!current) return
  const tr = state.tr
  if (transformRow(tr, state, current, target)) view.dispatch(tr)
  editor.commands.focus()
}

function transformsFor(editor: Editor, row: BlockRef): BlockMenuModel['transforms'] {
  if (!canTransform(row.node)) return []
  const current = currentTarget(row) ?? currentListTarget(editor.state, row)
  return (Object.keys(TRANSFORM_LABELS) as TransformTarget[]).map((target) => ({
    target,
    label: t(TRANSFORM_LABELS[target]),
    current: target === current,
  }))
}

function actionsFor(editor: Editor, row: BlockRef, copied: (text: string) => void): MenuAction[] {
  const range = rangeOf(row)

  return [
    {
      id: 'duplicate',
      label: t('editor.blockMenu.duplicate'),
      shortcut: 'Ctrl+D',
      run: () => {
        const { state, view } = editor
        const slice = state.doc.slice(range.from, range.to)
        view.dispatch(state.tr.insert(range.to, slice.content).scrollIntoView())
        editor.commands.focus()
      },
    },
    {
      id: 'moveUp',
      label: t('editor.blockMenu.moveUp'),
      shortcut: 'Ctrl+Shift+↑',
      run: () => {
        selectRow(editor, row)
        editor.commands.keyboardShortcut('Mod-Shift-ArrowUp')
      },
    },
    {
      id: 'moveDown',
      label: t('editor.blockMenu.moveDown'),
      shortcut: 'Ctrl+Shift+↓',
      run: () => {
        selectRow(editor, row)
        editor.commands.keyboardShortcut('Mod-Shift-ArrowDown')
      },
    },
    {
      id: 'copyMarkdown',
      label: t('editor.blockMenu.copyMarkdown'),
      run: () => copied(markdownOfRow(editor.state.doc, row)),
    },
    {
      id: 'select',
      label: t('editor.blockMenu.selectBlock'),
      shortcut: 'Esc',
      run: () => selectRow(editor, row),
    },
    {
      id: 'delete',
      label: t('common.delete'),
      shortcut: 'Delete',
      danger: true,
      run: () => {
        const { state, view } = editor
        view.dispatch(state.tr.delete(range.from, range.to))
        editor.commands.focus()
      },
    },
  ]
}

function selectRow(editor: Editor, row: BlockRef): void {
  const { state, view } = editor
  view.dispatch(setBlockSelection(state.tr, { range: rangeOf(row), anchor: row.pos }))
  editor.commands.focus()
}

function clipboardFor(editor: Editor, copied: (text: string) => void): MenuAction[] {
  const { selection, doc } = editor.state
  if (selection.empty) return []
  const text = doc.textBetween(selection.from, selection.to, '\n', ' ')
  if (!text) return []

  return [
    { id: 'copy', label: t('common.copy'), shortcut: 'Ctrl+C', run: () => copied(text) },
    {
      id: 'cut',
      label: t('editor.blockMenu.cut'),
      shortcut: 'Ctrl+X',
      run: () => {
        copied(text)
        editor.chain().focus().deleteSelection().run()
      },
    },
    {
      id: 'paste',
      label: t('editor.blockMenu.paste'),
      shortcut: 'Ctrl+V',
      run: () => {
        // The browser decides whether this is allowed; a refusal is silent and the keyboard
        // shortcut always works, so there is nothing useful to report here.
        void navigator.clipboard
          .readText()
          .then((value) => value && editor.chain().focus().insertContent(value).run())
          .catch(() => {})
      },
    },
  ]
}

export function menuFor(editor: Editor, row: BlockRef, copied: (text: string) => void): BlockMenuModel {
  return {
    row,
    title: titleOf(row.node),
    transforms: transformsFor(editor, row),
    actions: actionsFor(editor, row, copied),
    properties: propertiesFor(editor, row),
    clipboard: clipboardFor(editor, copied),
  }
}

function titleOf(node: Node): string {
  const key = BLOCK_TITLES[node.type.name]
  return key ? t(key) : node.type.name
}

export { applyTransform }
