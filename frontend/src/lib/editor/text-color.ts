/**
 * Colour, as an inline mark (block-editor spec §3.2, amended 2026-09-09).
 *
 * A mark rather than a node, because colour has to coexist with everything else inline:
 * half a bold phrase can be red, and a coloured run can hold a link and a wiki link. That
 * also means it needs no work anywhere else — "Copy as markdown" and every save already go
 * through `proseMirrorToMdast`, which knows the mark, so the colour rides along.
 *
 * The attribute is the source of truth and `class` is derived from it. The DOM carries
 * both: `data-text-color` is what `parseHTML` reads back — so a phrase copied from one
 * document into another keeps its colour — and `class="green"` is what the stylesheet hangs
 * on, matching the class name that is in the file. Only the words in `TEXT_COLORS` are
 * accepted, at parse time as well as at render time; a document written by an agent is not
 * allowed to name a class the app never defined.
 *
 * The three behavioural options are the ProseMirror defaults and are written out anyway,
 * because they are the whole feel of the thing: typing at the end of a coloured run
 * continues it, `Enter` carries it into the new block, and a second colour *replaces* the
 * first instead of stacking with it (the last is `excludes` defaulting to the mark itself).
 */

import { Mark, mergeAttributes } from '@tiptap/core'

import { t } from '@/i18n'
import { isTextColor, type TextColor as TextColorName } from '@/lib/md'

/**
 * What a colour is called on screen, in the interface language. Only a title: the word in the
 * file is the class name and never translates. `grey` is `gray` spelled the other way, and
 * shares its name.
 */
export function textColorLabel(color: TextColorName): string {
  const name: Exclude<TextColorName, 'grey'> = color === 'grey' ? 'gray' : color
  return t(`editor.colors.${name}`)
}

export interface TextColorOptions {
  /** Merged into every rendered `<span>`, the convention every Tiptap mark follows. */
  HTMLAttributes: Record<string, unknown>
}

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    textColor: {
      /** Colour the selection, replacing whatever colour it already has. */
      setTextColor: (color: TextColorName) => ReturnType
      /** Drop the colour. From a bare caret, the whole coloured run. */
      unsetTextColor: () => ReturnType
    }
  }
}

export const TextColor = Mark.create<TextColorOptions>({
  name: 'textColor',

  addOptions() {
    return { HTMLAttributes: {} }
  },

  inclusive: true,
  keepOnSplit: true,
  spanning: true,

  addAttributes() {
    return {
      color: {
        default: null,
        parseHTML: (element) => {
          const value = element.getAttribute('data-text-color')
          return isTextColor(value) ? value : null
        },
        renderHTML: (attributes) =>
          isTextColor(attributes.color)
            ? { 'data-text-color': attributes.color, class: attributes.color }
            : {},
      },
    }
  },

  parseHTML() {
    return [
      {
        tag: 'span[data-text-color]',
        // A colour outside the list is not this mark. `false` declines the rule, so the
        // span arrives as plain text rather than as a mark no stylesheet can honour.
        getAttrs: (element: HTMLElement) =>
          isTextColor(element.getAttribute('data-text-color')) ? null : false,
      },
    ]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(this.options.HTMLAttributes, HTMLAttributes), 0]
  },

  addCommands() {
    return {
      // `setMark`, never `toggleMark`: the toolbar's swatches are a picker with its own
      // "no colour" button, and a swatch that removed the colour when clicked twice would
      // make re-applying the colour already there do the opposite of what it says.
      setTextColor:
        (color) =>
        ({ commands }) =>
          commands.setMark(this.name, { color }),

      unsetTextColor:
        () =>
        ({ commands }) =>
          commands.unsetMark(this.name, { extendEmptyMarkRange: true }),
    }
  },
})
