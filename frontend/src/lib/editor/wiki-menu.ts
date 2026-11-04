/**
 * The `[[` autocomplete (block-editor spec §5).
 *
 * The interesting decision is what to *write*, not what to show. A wiki link addresses a
 * document by name, and a name is the better thing to store: it survives the file moving to
 * another folder, which a path does not. So the bare stem is written whenever it is
 * unambiguous — and when two documents share a stem, the path is written instead, because a
 * name that resolves to two files resolves to neither.
 *
 * Ambiguity is decided over the *whole* set of documents, not over the matches shown: a name
 * is ambiguous because a second file exists, whether or not the query happened to find it.
 */

import { Extension } from '@tiptap/core'
import { PluginKey } from '@tiptap/pm/state'
import Suggestion, { type SuggestionOptions } from '@tiptap/suggestion'
import { reactive } from 'vue'

export interface DocumentChoice {
  path: string
  title: string
  /** What goes between the brackets — the stem, or the path when the stem is taken twice. */
  target: string
  /** Shown beside the title when the stem alone would not identify it. */
  hint: string | null
}

export interface WikiMenuState {
  open: boolean
  items: DocumentChoice[]
  index: number
  query: string
  rect: { top: number; bottom: number; left: number } | null
  /** Present when nothing matched: the offer to create the document (§5). */
  create: string | null
  select: (choice: DocumentChoice) => void
  /** Insert a link to a document that does not exist yet, and ask the page to create it. */
  selectNew: (name: string) => void
}

export function createWikiState(): WikiMenuState {
  return reactive<WikiMenuState>({
    open: false,
    items: [],
    index: 0,
    query: '',
    rect: null,
    create: null,
    select: () => {},
    selectNew: () => {},
  })
}

const SUFFIX = /\.(md|markdown|txt)$/i

function stemOf(path: string): string {
  return (path.split('/').pop() ?? path).replace(SUFFIX, '')
}

/** Path without its extension — what an ambiguous name is written as. */
function pathTargetOf(path: string): string {
  return path.replace(SUFFIX, '')
}

/**
 * Rank documents against a query, over name *and* path (§5).
 *
 * A hit on the name outranks a hit on the folder: someone typing `plan` means the document
 * called plan, not everything under `plans/`.
 */
export function searchDocuments(
  documents: { path: string; title: string }[],
  query: string,
): DocumentChoice[] {
  const stems = new Map<string, number>()
  for (const document of documents) {
    const stem = stemOf(document.path).toLowerCase()
    stems.set(stem, (stems.get(stem) ?? 0) + 1)
  }

  const needle = query.trim().toLowerCase()
  const scored: { choice: DocumentChoice; score: number }[] = []

  for (const document of documents) {
    const stem = stemOf(document.path)
    const ambiguous = (stems.get(stem.toLowerCase()) ?? 0) > 1
    const choice: DocumentChoice = {
      path: document.path,
      title: document.title,
      target: ambiguous ? pathTargetOf(document.path) : stem,
      // The path is shown whenever it is what disambiguates, and when the query was aimed
      // at a folder — in both cases the row would otherwise be indistinguishable.
      hint: ambiguous || needle.includes('/') ? document.path : null,
    }

    if (!needle) {
      scored.push({ choice, score: 0 })
      continue
    }

    const inStem = stem.toLowerCase().indexOf(needle)
    const inTitle = document.title.toLowerCase().indexOf(needle)
    const inPath = document.path.toLowerCase().indexOf(needle)
    if (inStem === -1 && inTitle === -1 && inPath === -1) continue

    let score = 0
    if (inStem === 0) score += 40
    else if (inStem > 0) score += 24
    if (inTitle === 0) score += 20
    else if (inTitle > 0) score += 12
    if (inPath >= 0) score += 6
    score -= document.path.length / 40
    scored.push({ choice, score })
  }

  scored.sort((one, other) => other.score - one.score)
  return scored.map((entry) => entry.choice)
}

export function createWikiMenu(
  state: WikiMenuState,
  documents: () => { path: string; title: string }[],
) {
  const suggestion: Omit<SuggestionOptions<DocumentChoice>, 'editor'> = {
    // Distinct from the `/` menu's — see the note there.
    pluginKey: new PluginKey('voxarium_studioWikiSuggestion'),
    char: '[[',
    // A name may contain spaces (`[[a document with spaces]]`), so they cannot end the query.
    allowSpaces: true,
    allowedPrefixes: null,
    allow: ({ state: editorState, range }) =>
      !editorState.doc.resolve(range.from).parent.type.spec.code,

    items: ({ query }) => searchDocuments(documents(), query).slice(0, 8),

    command: ({ editor, range, props }) => {
      editor
        .chain()
        .focus()
        // The link alone, with nothing after it. A trailing space looks harmless and is
        // not: at the end of a block the serializer has to write it as `&#x20;` to stop it
        // being swallowed, and that entity lands in the file.
        .insertContentAt(range, {
          type: 'wikiLink',
          attrs: { value: props.target, target: props.target, label: null },
        })
        .run()
    },

    render: () => {
      let dismissed = false
      let latest: { command: (choice: DocumentChoice) => void } | null = null

      const show = (props: {
        query: string
        clientRect?: (() => DOMRect | null) | null
        command: (choice: DocumentChoice) => void
      }) => {
        if (dismissed) {
          state.open = false
          return
        }
        latest = props
        // Recomputed rather than read from the plugin's staged `items` — it renders once
        // with an empty list before the resolved one, and trusting that frame makes the
        // menu flicker and mis-decide whether anything matched.
        const items = searchDocuments(documents(), props.query).slice(0, 8)
        const rect = props.clientRect?.()

        state.items = items
        state.query = props.query
        state.create = items.length ? null : props.query.trim() || null
        state.rect = rect ? { top: rect.top, bottom: rect.bottom, left: rect.left } : null
        state.select = props.command
        state.open = items.length > 0 || state.create !== null
      }

      return {
        onStart: (props) => {
          dismissed = false
          state.index = 0
          show(props)
        },
        onUpdate: (props) => {
          state.index = 0
          show(props)
        },
        onKeyDown: ({ event }) => {
          if (!state.open) return false
          if (event.key === 'Escape') {
            dismissed = true
            state.open = false
            return true
          }
          if (event.key === 'ArrowDown' && state.items.length) {
            state.index = (state.index + 1) % state.items.length
            return true
          }
          if (event.key === 'ArrowUp' && state.items.length) {
            state.index = (state.index - 1 + state.items.length) % state.items.length
            return true
          }
          if (event.key === 'Enter' || event.key === 'Tab') {
            const choice = state.items[state.index]
            if (choice) latest?.command(choice)
            else if (state.create) state.selectNew(state.create)
            return true
          }
          return false
        },
        onExit: () => {
          dismissed = false
          state.open = false
          state.items = []
          state.create = null
          state.rect = null
        },
      }
    },
  }

  return Extension.create({
    name: 'wikiMenu',
    addProseMirrorPlugins() {
      return [Suggestion({ ...suggestion, editor: this.editor })]
    },
  })
}
