/**
 * The toolbar that appears over a text selection (block-editor spec §4.4).
 *
 * Written here rather than taken from a bubble-menu extension, because the one rule that
 * matters is one no generic implementation knows: **it must not appear when whole blocks are
 * selected.** §4.4 draws that line deliberately — a selection of blocks is operated on by the
 * block menu (§4.5), and offering "bold" for four paragraphs and a table is offering
 * something that has no single meaning.
 *
 * Position is measured from the selection's own rectangles rather than from the caret, so a
 * selection spanning three lines gets a toolbar centred over it and not over its last
 * character.
 */

import type { Editor } from '@tiptap/core'
import { onBeforeUnmount, ref, watch, type Ref } from 'vue'

import type { TextColor } from '@/lib/md'
import { getBlockSelection } from './block-selection'

export interface ToolbarPosition {
  top: number
  left: number
  /** True when there was no room above and the toolbar sits under the selection instead. */
  below: boolean
}

const HEIGHT = 40
const GAP = 8

export function useSelectionToolbar(editor: Ref<Editor | undefined>) {
  const position = ref<ToolbarPosition | null>(null)
  /** The link editor takes over the toolbar rather than opening a dialog (§4.5's spirit). */
  const editingLink = ref(false)
  const linkDraft = ref('')
  /** So does the colour picker: eleven swatches are still narrower than the link field. */
  const pickingColor = ref(false)

  function refresh(): void {
    const instance = editor.value
    if (!instance || !instance.isFocused) return hide()

    const { state, view } = instance
    const { selection } = state

    // Whole blocks selected — §4.4 says this toolbar stays away.
    if (getBlockSelection(state)) return hide()
    if (selection.empty) return hide()
    // A selected atom (an image, a wiki link) is not a text range either.
    if (!state.doc.textBetween(selection.from, selection.to, ' ').trim()) return hide()
    if (state.doc.resolve(selection.from).parent.type.spec.code) return hide()

    const start = view.coordsAtPos(selection.from)
    const end = view.coordsAtPos(selection.to)
    const top = Math.min(start.top, end.top)
    const bottom = Math.max(start.bottom, end.bottom)
    const centre = (Math.min(start.left, end.left) + Math.max(start.right, end.right)) / 2

    const below = top - HEIGHT - GAP < 0
    position.value = {
      top: below ? bottom + GAP : top - HEIGHT - GAP,
      left: centre,
      below,
    }
  }

  function hide(): void {
    position.value = null
    editingLink.value = false
    pickingColor.value = false
  }

  function startColor(): void {
    pickingColor.value = true
  }

  /**
   * `null` removes the colour.
   *
   * The picker closes on a choice, because the swatches replace the formatting buttons and
   * leaving them up would hide bold behind a decision the user has already made.
   */
  function applyColor(color: TextColor | null): void {
    const instance = editor.value
    if (!instance) return
    const chain = instance.chain().focus()
    if (color) chain.setTextColor(color).run()
    else chain.unsetTextColor().run()
    pickingColor.value = false
  }

  function startLink(): void {
    const instance = editor.value
    if (!instance) return
    linkDraft.value = String(instance.getAttributes('link').href ?? '')
    editingLink.value = true
  }

  function applyLink(): void {
    const instance = editor.value
    if (!instance) return
    const href = linkDraft.value.trim()
    const chain = instance.chain().focus()
    if (href) chain.setLink({ href }).run()
    else chain.unsetLink().run()
    editingLink.value = false
  }

  watch(
    editor,
    (instance, _previous, onCleanup) => {
      if (!instance) return
      const update = () => refresh()
      instance.on('selectionUpdate', update)
      instance.on('transaction', update)
      instance.on('blur', hide)
      onCleanup(() => {
        instance.off('selectionUpdate', update)
        instance.off('transaction', update)
        instance.off('blur', hide)
      })
    },
    { immediate: true },
  )

  onBeforeUnmount(hide)

  return {
    position,
    editingLink,
    linkDraft,
    pickingColor,
    startLink,
    applyLink,
    startColor,
    applyColor,
    hide,
  }
}
