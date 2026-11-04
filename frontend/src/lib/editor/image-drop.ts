/**
 * Dropping a picture into a document (block-editor spec §3.4).
 *
 * The file is copied beside the `.md` and a relative `![…](…)` reference is inserted on its own
 * line under the row it was dropped on (`insertionFor`) — so the two halves of a document, its
 * text and its pictures, stay in one folder that can be moved, committed and reviewed together.
 * Uploading is the *page's* job, injected as `upload`, which keeps this module free of the API
 * surface and of which document happens to be open.
 *
 * **The insertion point is a decoration, not a remembered number.** An upload is a round trip,
 * and a position captured before it means nothing afterwards if anything was typed meanwhile —
 * text above the drop moves it, and the picture lands somewhere the user did not drop it. A
 * widget decoration is mapped through every transaction by ProseMirror itself, so the position
 * found when the bytes land is the same *place*, whatever happened to the document. It doubles
 * as the only feedback a 20 MiB drop over a slow disk gives.
 */

import { Extension } from '@tiptap/core'
import type { Node } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet, type EditorView } from '@tiptap/pm/view'

import { t } from '@/i18n'
import { acceptsBlocks, rowAt } from './blocks'
import { isImageFile, IMAGE_SUFFIXES } from './images'

/** What the page answers with once the file is beside the document. */
export interface UploadedImage {
  /** The reference to write into the Markdown, exactly as it should appear. */
  src: string
  alt: string
}

export type ImageUploader = (file: File) => Promise<UploadedImage>

export interface ImageDropOptions {
  /** Null while no document is open, which is also "do not take the drop". */
  upload: () => ImageUploader | null
  /** Shown to the user; the page owns the banner. */
  onError: (message: string) => void
}

const placeholderKey = new PluginKey<DecorationSet>('imageDropPlaceholder')

type PlaceholderAction =
  | { add: { id: object; name: string; pos: number; side: number } }
  | { remove: object }

/** True when a drag carries files rather than text — the only kind this module takes. */
export function dragCarriesFiles(event: DragEvent): boolean {
  return [...(event.dataTransfer?.types ?? [])].includes('Files')
}

/** Where a pending upload's decoration has been mapped to, or null if it is gone. */
function placeholderPos(view: EditorView, id: object): number | null {
  const set = placeholderKey.getState(view.state)
  const found = set?.find(undefined, undefined, (spec) => spec.id === id)
  return found?.length ? found[0].from : null
}

/**
 * Where a picture dropped at *pos* goes: **its own line under the row it was dropped on**.
 *
 * `posAtCoords` answers with a text position, so dropping on a paragraph would otherwise put
 * the picture inside the sentence, wherever between two letters the pointer happened to be —
 * `First paragraph.![shot](shot.png)` in the file. A picture is a thing of its own here, and a
 * row is what the user aimed at, which is why this goes through `rowAt`.
 *
 * Two positions rather than one, and the second is not a refinement — it is the difference
 * between working and visibly breaking the document. A row that **takes blocks** (a list item,
 * a callout, a toggle) is a place to go *into*: `row.end` for a bullet is the position between
 * two items, and a paragraph inserted there splits the list in half. `row.end - 1` is the end
 * of the item's own content, which makes the picture a second paragraph of that bullet — the
 * shape the exported documents already use.
 *
 * An empty paragraph is the third case: it is filled rather than followed, or dropping into an
 * empty document leaves a blank line above the picture.
 */
export function insertionFor(doc: Node, pos: number): number {
  const row = rowAt(doc, pos)
  if (!row) return doc.content.size
  if (row.node.type.name === 'paragraph' && row.node.content.size === 0) return row.pos + 1
  return acceptsBlocks(row.node) ? row.end - 1 : row.end
}

/**
 * The node to insert at *pos*: the picture itself, or a paragraph holding it.
 *
 * An image is **inline**, so it can only be inserted where inline content is allowed, and
 * `insertionFor` deliberately answers with a position *between* blocks. Inserting a bare
 * inline node there produces a document the schema forbids — ProseMirror throws, and the
 * picture is lost along with the upload that fetched it.
 */
function imageAt(view: EditorView, pos: number, src: string, alt: string) {
  const { schema, doc } = view.state
  const image = schema.nodes.image.create({ src, alt, title: null })
  return doc.resolve(pos).parent.inlineContent ? image : schema.nodes.paragraph.create(null, image)
}

function decoration(name: string): HTMLElement {
  const element = document.createElement('span')
  element.className = 'doc-image-uploading'
  // `textContent`, never `innerHTML`: this is a file name off a drag from outside the app.
  element.textContent = t('editor.image.uploading', { name })
  return element
}

/**
 * Take a set of dropped files: upload each, insert each where its placeholder ends up.
 *
 * Exported because the editor's **margins** are not ProseMirror's DOM, so a drop into the
 * gutter never reaches the plugin — `BlockEditor` catches those and calls this with a
 * position probed inside the content column.
 */
export async function receiveImageDrop(
  view: EditorView,
  files: File[],
  pos: number,
  options: ImageDropOptions,
): Promise<void> {
  const upload = options.upload()
  if (!upload) return

  // Normalised once, here, so the placeholder marks the place the picture will actually land
  // rather than the letter the pointer was over.
  const at = insertionFor(view.state.doc, pos)

  const pictures = files.filter(isImageFile)
  if (pictures.length < files.length) {
    options.onError(t('editor.image.onlyPictures', { suffixes: IMAGE_SUFFIXES.join(', ') }))
  }

  // Every placeholder is created before the first upload finishes, so several files dropped
  // together keep the order they were dropped in — `side` is what orders widgets sharing a
  // position, and what biases each one's mapping when the picture before it is inserted.
  const pending = pictures.map((file, index) => {
    const id = {}
    view.dispatch(
      view.state.tr.setMeta(placeholderKey, {
        add: { id, name: file.name, pos: at, side: index },
      } satisfies PlaceholderAction),
    )
    return { id, file }
  })

  await Promise.all(
    pending.map(async ({ id, file }) => {
      try {
        const { src, alt } = await upload(file)
        const landing = placeholderPos(view, id)
        // Null means the user deleted the block the picture was going into while it uploaded.
        // The file is beside the document either way; nothing is inserted into a place that
        // no longer exists.
        if (landing !== null) {
          view.dispatch(
            view.state.tr
              .insert(landing, imageAt(view, landing, src, alt))
              .setMeta(placeholderKey, { remove: id } satisfies PlaceholderAction),
          )
          return
        }
      } catch (cause) {
        options.onError(
          `${file.name}: ${cause instanceof Error ? cause.message : String(cause)}`,
        )
      }
      view.dispatch(
        view.state.tr.setMeta(placeholderKey, { remove: id } satisfies PlaceholderAction),
      )
    }),
  )
}

export function createImageDrop(options: ImageDropOptions) {
  return Extension.create({
    name: 'imageDrop',

    addProseMirrorPlugins() {
      return [
        new Plugin({
          key: placeholderKey,
          state: {
            init: () => DecorationSet.empty,
            apply(tr, set) {
              set = set.map(tr.mapping, tr.doc)
              const action = tr.getMeta(placeholderKey) as PlaceholderAction | undefined
              if (action && 'add' in action) {
                const { id, name, pos, side } = action.add
                set = set.add(tr.doc, [
                  Decoration.widget(pos, decoration(name), { id, side }),
                ])
              }
              if (action && 'remove' in action) {
                set = set.remove(
                  set.find(undefined, undefined, (spec) => spec.id === action.remove),
                )
              }
              return set
            },
          },
          props: {
            decorations: (state) => placeholderKey.getState(state),

            handleDrop(view, event, _slice, moved) {
              // `moved` is ProseMirror carrying its own node across the document — a picture
              // being dragged from one paragraph to another is that, not a new file.
              if (moved) return false
              const files = [...(event.dataTransfer?.files ?? [])]
              if (!files.length) return false

              event.preventDefault()
              const at = view.posAtCoords({ left: event.clientX, top: event.clientY })
              void receiveImageDrop(view, files, at?.pos ?? view.state.selection.from, options)
              return true
            },
          },
        }),
      ]
    },
  })
}
