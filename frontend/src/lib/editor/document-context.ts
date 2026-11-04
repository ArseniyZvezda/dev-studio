/**
 * Which document an editor shows, for node views that resolve something against it — a
 * picture's reference is relative to its document's folder (block-editor spec §3.4).
 *
 * In Documents that is the store's open document, and a node view reads it there. The graph
 * page's reader shows a document that is *not* open (block-editor spec §5.1.1), so it names its
 * own. A node view is mounted outside the page's component tree and cannot take a prop; the
 * editor is the one thing it is handed, so the path rides on an extension's options.
 */
import { Extension, type Editor } from '@tiptap/core'

interface DocumentContextOptions {
  path: () => string | null
}

export const DocumentContext = Extension.create<DocumentContextOptions>({
  name: 'documentContext',
  addOptions() {
    return { path: () => null }
  },
})

/** The document an editor was told it shows, or `null` when it was not told. */
export function documentPathOf(editor: Editor): string | null {
  const context = editor.extensionManager.extensions.find(
    (extension) => extension.name === DocumentContext.name,
  )
  return context ? (context.options as DocumentContextOptions).path() : null
}
