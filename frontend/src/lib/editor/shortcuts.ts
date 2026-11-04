/**
 * The keyboard shortcuts from §4.8 that Tiptap does not already provide.
 *
 * Everything here has to `preventDefault` by returning `true`, because each one collides with
 * something the browser would rather do: `Ctrl+B/I` are Tiptap's already, but `Ctrl+Shift+X`,
 * `Ctrl+Alt+1..3` and `Ctrl+Shift+7/8/9` are not, and nothing in the Studio itself claims any
 * of them — the app has no global shortcuts at all, only `Escape` inside two open dialogs.
 *
 * `Ctrl+S` is handled in the component rather than here: it is a *document* action, and the
 * editor is not the thing that knows how to save.
 */

import { Extension } from '@tiptap/core'

import { getBlockSelection } from './block-selection'

export const EditorShortcuts = Extension.create({
  name: 'editorShortcuts',

  addKeyboardShortcuts() {
    const heading = (level: 1 | 2 | 3) => () =>
      this.editor.chain().focus().toggleHeading({ level }).run()

    return {
      'Mod-Alt-1': heading(1),
      'Mod-Alt-2': heading(2),
      'Mod-Alt-3': heading(3),

      // The digits are the shifted characters on a US layout, which is how every editor of
      // this shape spells them: 8 is `*`, 7 is `&`… and 9 is the checklist by convention.
      'Mod-Shift-8': () => this.editor.chain().focus().toggleBulletList().run(),
      'Mod-Shift-7': () => this.editor.chain().focus().toggleOrderedList().run(),
      'Mod-Shift-9': () => this.editor.chain().focus().toggleTaskList().run(),

      'Mod-Shift-x': () => this.editor.chain().focus().toggleStrike().run(),
    }
  },
})

/**
 * `Tab` never leaves the editor while text is being edited (§4.8).
 *
 * Without this the browser takes an unhandled `Tab` and moves focus to the next control on
 * the page — in the middle of typing, which is not a thing anyone asked for. §4.8 gives Tab
 * two jobs, indenting a list item and indenting inside code, and both are handled by the
 * extensions that own those nodes. This one only catches what falls through: a paragraph, a
 * heading, the first item of a list that cannot sink any further.
 *
 * **Priority 10, far below the default 100**, so the list, table and code-block handlers all
 * get the key first. A guard that runs before them would swallow the behaviour it exists to
 * protect.
 *
 * The one way out is deliberate: with blocks *selected* rather than a caret in text, nothing
 * is being typed, so `Tab` is let through and a keyboard user can leave the editor with
 * `Escape` then `Tab`.
 */
export const TabGuard = Extension.create({
  name: 'tabGuard',
  priority: 10,

  addKeyboardShortcuts() {
    const trap = () => !getBlockSelection(this.editor.state)
    return { Tab: trap, 'Shift-Tab': trap }
  },
})
