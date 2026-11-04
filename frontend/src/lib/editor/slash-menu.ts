/**
 * The `/` menu (block-editor spec §4.3).
 *
 * Tiptap's `Suggestion` plugin does the hard part — noticing the trigger, tracking the query
 * as it is typed, and keeping the range to replace — so what is here is the two decisions it
 * does not make.
 *
 * **When the menu goes away.** §4.3 asks for three ways out, and only one is the plugin's:
 * `Escape`, a space straight after the `/`, and "three characters in with nothing matching".
 * The last two are guesses about intent — someone typing `/ ` meant a slash, and someone
 * typing `/xyz` is writing a path, not looking for a block — so once dismissed the menu
 * stays down until the suggestion itself ends. Reopening under the user's fingers is worse
 * than not opening.
 *
 * **What "insert" means.** Into an empty block it changes that block; into a block with text
 * in it, it starts a new one below (§4.3). The alternative — turning the paragraph someone is
 * halfway through into a table — is not a reading of "insert" anyone wants.
 */

import { Extension, type Editor, type Range } from '@tiptap/core'
import { PluginKey } from '@tiptap/pm/state'
import Suggestion, { type SuggestionOptions } from '@tiptap/suggestion'
import { reactive } from 'vue'

import { searchSlashItems, type SlashItem } from './slash-items'

/** Below this many characters, "nothing matched" is still probably a work in progress. */
const GIVE_UP_AFTER = 3

export interface SlashMenuState {
  open: boolean
  items: SlashItem[]
  index: number
  query: string
  /** Caret rectangle in viewport coordinates, for placing the popup (§4.3). */
  rect: { top: number; bottom: number; left: number } | null
  select: (item: SlashItem) => void
}

export function createSlashState(): SlashMenuState {
  return reactive<SlashMenuState>({
    open: false,
    items: [],
    index: 0,
    query: '',
    rect: null,
    select: () => {},
  })
}

/**
 * Run an item, deciding between "change this block" and "start a new one".
 *
 * The split is done *before* the item runs, so the item only ever sees an empty block and
 * does not have to know which case it is in.
 */
function insert(editor: Editor, range: Range, item: SlashItem): void {
  const $from = editor.state.doc.resolve(range.from)
  const start = $from.start()
  const before = $from.parent.textBetween(0, range.from - start)
  const after = $from.parent.textBetween(range.to - start, $from.parent.content.size)

  if (`${before}${after}`.trim() === '') {
    item.run(editor, range)
    return
  }

  editor.chain().focus().deleteRange(range).run()
  const end = editor.state.doc.resolve(editor.state.selection.from).end()
  editor.chain().focus().setTextSelection(end).splitBlock().run()
  const caret = editor.state.selection.from
  item.run(editor, { from: caret, to: caret })
}

export function createSlashMenu(state: SlashMenuState) {
  const suggestion: Omit<SuggestionOptions<SlashItem>, 'editor'> = {
    // Its own key. `Suggestion` defaults to a shared one, and a second suggestion plugin —
    // the `[[` autocomplete — then fails the editor outright with "adding different
    // instances of a keyed plugin".
    pluginKey: new PluginKey('voxarium_studioSlashSuggestion'),
    char: '/',
    // Start of a block, or after a space — never in the middle of a word, where a slash is
    // punctuation (§4.3).
    allowedPrefixes: [' '],
    allowSpaces: false,
    // A slash inside code is a slash.
    allow: ({ state: editorState, range }) =>
      !editorState.doc.resolve(range.from).parent.type.spec.code,

    items: ({ query }) => searchSlashItems(query),

    command: ({ editor, range, props }) => insert(editor, range, props),

    render: () => {
      let dismissed = false

      const show = (props: {
        query: string
        clientRect?: (() => DOMRect | null) | null
        command: (item: SlashItem) => void
      }) => {
        // The matches are recomputed here rather than read from `props.items`, and that is
        // the fix for a real bug: the suggestion plugin resolves items asynchronously and
        // renders *twice* per keystroke — once with an empty list, then with the answer. The
        // "no matches, give up" rule below fired on that transient empty frame and closed the
        // menu on every query of three characters or more, including ones that matched.
        const items = searchSlashItems(props.query)
        if (!items.length && props.query.length >= GIVE_UP_AFTER) dismissed = true
        if (dismissed) {
          state.open = false
          return
        }
        const rect = props.clientRect?.()
        state.items = items
        state.query = props.query
        state.rect = rect ? { top: rect.top, bottom: rect.bottom, left: rect.left } : null
        state.select = props.command
        state.open = items.length > 0
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
          if (event.key === 'ArrowDown') {
            state.index = (state.index + 1) % state.items.length
            return true
          }
          if (event.key === 'ArrowUp') {
            state.index = (state.index - 1 + state.items.length) % state.items.length
            return true
          }
          if (event.key === 'Enter' || event.key === 'Tab') {
            const item = state.items[state.index]
            if (item) state.select(item)
            return true
          }
          return false
        },
        onExit: () => {
          dismissed = false
          state.open = false
          state.items = []
          state.rect = null
        },
      }
    },
  }

  return Extension.create({
    name: 'slashMenu',
    addProseMirrorPlugins() {
      return [Suggestion({ ...suggestion, editor: this.editor })]
    },
  })
}
