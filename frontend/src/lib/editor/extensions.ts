/**
 * The editor's schema, assembled (block-editor spec §3).
 *
 * Tiptap ships most of §3.1 already; what is configured here is where its defaults and
 * Markdown disagree. Each override below is one of those disagreements, and removing one
 * changes what lands on disk — this is a schema, not a preferences file.
 */

import type { Attributes } from '@tiptap/core'
import { Collaboration } from '@tiptap/extension-collaboration'
import { CollaborationCaret } from '@tiptap/extension-collaboration-caret'
import { Placeholder } from '@tiptap/extensions'
import CodeBlock from '@tiptap/extension-code-block'
import { TaskItem, TaskList } from '@tiptap/extension-list'
import { Image } from '@tiptap/extension-image'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import StarterKit from '@tiptap/starter-kit'
import { VueNodeViewRenderer } from '@tiptap/vue-3'

import ImageView from '@/components/docs/nodes/ImageView.vue'
import CodeBlockView from '@/components/docs/nodes/CodeBlockView.vue'
import { CarriedState, COLLAB_FIELD } from '@/lib/collab/carried'
import { renderCaret, renderSelection } from '@/lib/collab/caret'
import type { CollabSession } from '@/lib/collab/session'
import { t } from '@/i18n'
import { isImageWidth } from '@/lib/md/image-width'
import { BlockSelection } from './block-selection'
import { DocumentContext } from './document-context'
import { createImageDrop, type ImageDropOptions } from './image-drop'
import {
  Callout,
  DirectiveTitle,
  Toggle,
  UnsupportedBlock,
  UnsupportedInline,
  WikiLink,
} from './nodes'
import { EditorShortcuts, TabGuard } from './shortcuts'
import { createSlashMenu, type SlashMenuState } from './slash-menu'
import { TextColor } from './text-color'
import { createWikiMenu, type WikiMenuState } from './wiki-menu'

/**
 * GFM aligns a *column*, not a cell, so the alignment lives on the table and there is one
 * entry per column. §4.5 edits it; the schema only has to hold it.
 */
const MarkdownTable = Table.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      align: { default: [] as (string | null)[], rendered: false },
    }
  },
})

/**
 * A picture, drawn by `ImageView` (§3.4).
 *
 * Inline, because an image sits inside a paragraph in Markdown, and an atom — the reference
 * belongs to the file and is edited by editing the file, not by putting a caret in it. The
 * node view is what makes it *appear*: `src` is relative to the document's folder, which the
 * browser would resolve against the SPA's own URL and not find.
 *
 * **The reference is rendered as `data-src`, never as `src`**, and that is not cosmetic.
 * ProseMirror builds the node's `toDOM` spec beside the node view, so an `<img src>` here is
 * created and fetched even though nothing ever shows it — measured in the browser, one request
 * per picture: the relative ones landed on the SPA's catch-all and came back as `index.html`,
 * and a `![](https://…)` reference reached the *host*, which is the one thing §3.4 forbids.
 * An `<img>` with no `src` loads nothing, and `parseHTML` reads the attribute back from either
 * spelling, so an image survives being copied and pasted inside the editor.
 *
 * What the file holds is untouched by any of this: the serializer reads `attrs.src`, so a
 * resolved `/api/…` URL can never reach the Markdown.
 */
const DocImage = Image.extend({
  addAttributes() {
    const parent: Attributes = this.parent?.() ?? {}
    return {
      ...parent,
      src: {
        ...(parent.src ?? {}),
        parseHTML: (element: HTMLElement) =>
          element.getAttribute('data-src') ?? element.getAttribute('src'),
        renderHTML: (attributes: Record<string, unknown>) =>
          attributes.src ? { 'data-src': attributes.src } : {},
      },
      // Whole pixels or nothing, whatever the element said: the slider does arithmetic on it
      // and the file holds only the widths `image-width.ts` reads back (§3.4).
      width: {
        ...(parent.width ?? {}),
        default: null,
        parseHTML: (element: HTMLElement) => {
          const value = Number(element.getAttribute('data-width') ?? element.getAttribute('width'))
          return isImageWidth(value) ? value : null
        },
        renderHTML: (attributes: Record<string, unknown>) =>
          isImageWidth(attributes.width) ? { 'data-width': String(attributes.width) } : {},
      },
    }
  },

  parseHTML() {
    // The parent's `img[src]` still matches a picture pasted in from a browser.
    return [...(this.parent?.() ?? []), { tag: 'img[data-src]' }]
  },

  addNodeView() {
    return VueNodeViewRenderer(ImageView)
  },
})

/**
 * `checked` gains a third value: `null`, for a plain item inside a task list.
 *
 * GFM allows the mix, and forcing such an item to `false` would put an empty checkbox in
 * the file next to a line that never had one. With no `data-checked` attribute the CSS
 * hides the checkbox, so it reads as the plain item it is.
 */
const MarkdownTaskItem = TaskItem.extend({
  addAttributes() {
    return {
      checked: {
        default: false,
        keepOnSplit: false,
        parseHTML: (element) => {
          const value = element.getAttribute('data-checked')
          // `'null'` is what Tiptap's own node view writes for a null attribute — it
          // stringifies rather than omitting — so pasted content has to read it back.
          if (value === null || value === 'null') return null
          return value === '' || value === 'true'
        },
        renderHTML: (attributes) =>
          attributes.checked === null ? {} : { 'data-checked': String(attributes.checked) },
      },
    }
  },
})

/** The document's code stays a normal Tiptap code block; only its view gains a copy control. */
const CopyableCodeBlock = CodeBlock.extend({
  addNodeView() {
    return VueNodeViewRenderer(CodeBlockView)
  },
})

export interface EditorOptions {
  slash: SlashMenuState
  wiki?: WikiMenuState
  /** The documents `[[` can link to. A function, because the list changes while typing. */
  documents?: () => { path: string; title: string }[]
  /** How a dropped picture is put beside the document. Omitted, a drop is not taken. */
  images?: ImageDropOptions
  /**
   * Which document this editor shows, when it is not the store's open one — the graph page's
   * reader. Omitted, node views that resolve against a document use `docs.open`.
   */
  documentPath?: () => string | null
  /**
   * The live room this editor is a member of (dev spec §16.4). Given one, the document is
   * the room's — bound to its Yjs document instead of loaded from `content` — and other
   * members' cursors are drawn in it.
   */
  collab?: {
    session: CollabSession
    /** The room's document has been drawn for the first time; before this the editor is empty. */
    onFirstRender: () => void
  }
}

export function createExtensions(options: EditorOptions) {
  return [
    createSlashMenu(options.slash),
    ...(options.documentPath ? [DocumentContext.configure({ path: options.documentPath })] : []),
    ...(options.images ? [createImageDrop(options.images)] : []),
    ...(options.wiki && options.documents
      ? [createWikiMenu(options.wiki, options.documents)]
      : []),
    EditorShortcuts,
    TabGuard,
    // The room, when there is one. `CarriedState` first: its attribute has to be part of
    // the schema the binding is built over (see `carried.ts` for what it carries and why).
    ...(options.collab
      ? [
          CarriedState,
          Collaboration.configure({
            document: options.collab.session.doc,
            field: COLLAB_FIELD,
            onFirstRender: options.collab.onFirstRender,
          }),
          CollaborationCaret.configure({
            provider: { awareness: options.collab.session.awareness },
            user: options.collab.session.user,
            render: renderCaret,
            selectionRender: renderSelection,
          }),
        ]
      : []),
    StarterKit.configure({
      // §3.2: there is no underline in Markdown, and adding one through HTML is forbidden.
      underline: false,
      // 4–6 open and save correctly even though nothing offers them (§3.1).
      heading: { levels: [1, 2, 3, 4, 5, 6] },
      link: {
        openOnClick: false,
        // Both would rewrite the file behind the user: a typed URL is a typed URL until
        // they ask for a link (§4.9 lists the substitutions, and this is not one).
        autolink: false,
        linkOnPaste: true,
      },
      // §7 asks for undo grouped by pauses in typing rather than by keystroke. In a room the
      // history is the binding's own — one that undoes this person's edits and nobody
      // else's — and two histories over one document would each undo the other's steps.
      undoRedo: options.collab ? false : { newGroupDelay: 500 },
      // The empty paragraph StarterKit keeps after a final table or list is an insertion
      // like any other. In a room it would be an edit made by opening the document — sent
      // to everyone, and a reason for the scribe to write the file (dev spec §16.4). The gap
      // cursor still puts a caret after a final table.
      ...(options.collab ? { trailingNode: false as const } : {}),
      codeBlock: false,
    }),
    // block-editor spec §3.1: the view adds Copy without changing the Markdown content.
    // §4.8: Tab still indents inside code, two spaces as before.
    CopyableCodeBlock.configure({ enableTabIndentation: true, tabSize: 2 }),
    // After StarterKit, so the colour has the higher mark rank and its `<span>` is drawn
    // *inside* `<strong>` and `<em>` rather than around them.
    TextColor,
    TaskList,
    MarkdownTaskItem.configure({ nested: true }),
    MarkdownTable.configure({ resizable: false }),
    TableRow,
    TableHeader,
    TableCell,
    DocImage.configure({ inline: true, allowBase64: true }),
    WikiLink,
    DirectiveTitle,
    Callout,
    Toggle,
    UnsupportedBlock,
    UnsupportedInline,
    // After the nodes: its keymap has to see the schema it operates on, and its Escape and
    // Mod-a must win over the defaults (§4.7).
    BlockSelection,
    Placeholder.configure({
      // Only the block the caret is in, and only when the document is otherwise empty of
      // guidance — a placeholder on every empty block is noise (§4.10).
      showOnlyCurrent: true,
      placeholder: ({ node }) =>
        node.type.name === 'heading'
          ? t('editor.placeholder.heading')
          : node.type.name === 'directiveTitle'
            ? t('editor.placeholder.title')
            : t('editor.placeholder.text'),
    }),
  ]
}
