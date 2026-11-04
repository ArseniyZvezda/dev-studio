/**
 * The gutter and block dragging (block-editor spec §4.1, §4.2).
 *
 * A Vue composable rather than a ProseMirror plugin, because almost all of it is geometry:
 * where the pointer is, which row that is beside, and which of the possible drop positions
 * the *horizontal* position picks out. ProseMirror is asked structural questions
 * (`lib/editor/blocks.ts`) and given one transaction at the end.
 *
 * Two details do most of the UX work:
 *
 * - **The row under the pointer is probed at the content column's left edge, not at the
 *   pointer's own x.** The gutter lives outside the column (§4.1), so probing at the
 *   pointer would miss the document entirely as soon as the cursor left the text — which
 *   is exactly the "cursor loses the gutter on a diagonal" problem §4.1 calls out.
 * - **Nesting comes from the pointer's x against real DOM edges**, measured from the node
 *   each candidate would line up with, not from a pixel constant. A callout indents by a
 *   different amount than a list, and the indicator has to land where the block will.
 */

import type { Editor } from '@tiptap/core'
import { onBeforeUnmount, ref, type Ref } from 'vue'

import {
  collectRows,
  insertionsAfter,
  insertionsBefore,
  moveRange,
  rangeBetween,
  rangeOf,
  rowAt,
  type BlockRange,
  type BlockRef,
  type Insertion,
} from './blocks'
import { getBlockSelection, setBlockSelection, setDragRange } from './block-selection'

export interface GutterPosition {
  top: number
  height: number
  /**
   * The row's box in the scroller's own coordinates — where the hover outline is drawn, and
   * what the gutter hangs off the left of.
   *
   * Measured rather than assumed. The gutter used to place itself in CSS from the column's
   * half-width written out as a constant, which silently became wrong the moment the column
   * changed width: the constant said 24rem against a 72rem column, so on any window wide
   * enough for the column to reach its maximum the gutter landed ~108px *inside* the text.
   */
  left: number
  width: number
  /** Position of the row the gutter belongs to. */
  pos: number
}

export interface DropIndicator {
  top: number
  left: number
  width: number
}

/** §4.2: a translucent preview follows the cursor while the original stays put, dimmed. */
export interface DragPreview {
  x: number
  y: number
  text: string
  count: number
}

/** How close to an edge the pointer has to get before the document scrolls itself (§4.2). */
const AUTOSCROLL_MARGIN = 60
const AUTOSCROLL_SPEED = 12
/** Ignore a press that never really became a drag, so a click on the handle stays a click. */
const DRAG_THRESHOLD = 4

export function useBlockDrag(editor: Ref<Editor | undefined>, scroller: Ref<HTMLElement | null>) {
  const gutter = ref<GutterPosition | null>(null)
  const indicator = ref<DropIndicator | null>(null)
  const preview = ref<DragPreview | null>(null)
  const marquee = ref<{ top: number; left: number; width: number; height: number } | null>(null)
  const dragging = ref(false)

  let dragRange: BlockRange | null = null
  let target: Insertion | null = null
  let started = false
  let origin = { x: 0, y: 0 }
  let autoscroll: number | null = null
  let marqueeOrigin: { x: number; y: number } | null = null

  function view() {
    return editor.value?.view
  }

  /** The rectangle of a row, relative to the scroller. */
  function rectOf(pos: number): DOMRect | null {
    const dom = view()?.nodeDOM(pos)
    return dom instanceof HTMLElement ? dom.getBoundingClientRect() : null
  }

  function columnBounds(): { left: number; right: number } | null {
    const content = view()?.dom
    if (!(content instanceof HTMLElement)) return null
    const rect = content.getBoundingClientRect()
    return { left: rect.left, right: rect.right }
  }

  // ---------------------------------------------------------------- hover

  function onPointerMove(event: PointerEvent): void {
    if (dragging.value) return
    const instance = view()
    const bounds = columnBounds()
    if (!instance || !bounds) return

    const row = rowUnder(instance, bounds, event)
    if (!row) {
      gutter.value = null
      return
    }
    const rect = rectOf(row.pos)
    const host = scroller.value?.getBoundingClientRect()
    if (!rect || !host) {
      gutter.value = null
      return
    }
    // The gutter is absolutely positioned inside the scroller, so its offset is measured
    // from the *content* origin — which is `scrollTop` above the visible top edge. Without
    // that term the gutter is drawn one screenful too high the moment the document is
    // scrolled, i.e. it stops appearing at all on any document longer than the window.
    gutter.value = {
      top: rect.top - host.top + (scroller.value?.scrollTop ?? 0),
      height: rect.height,
      // The column, not the row: a short paragraph and a full-width table have to outline
      // to the same edges, or the highlight reads as the block being a different size.
      left: bounds.left - host.left,
      width: bounds.right - bounds.left,
      pos: row.pos,
    }
  }

  /**
   * The row under the pointer, probed twice.
   *
   * The column's left edge first — probing at the pointer's own x loses the row the instant
   * the cursor leaves the text, which is exactly the "diagonal reach" problem §4.1 calls out
   * and why this probe is where it is.
   *
   * But that edge sits in the *margin* of anything indented. A list item, a callout and a
   * blockquote all begin further right, so at the column's edge there is nothing to hit and
   * the gutter never appeared beside any of them — on a document that is mostly bullets,
   * that is most of the document. The fallback is the pointer's own column, clamped inside
   * the text, which is over the block by construction.
   */
  function rowUnder(
    instance: NonNullable<ReturnType<typeof view>>,
    bounds: { left: number; right: number },
    event: PointerEvent,
  ): BlockRef | null {
    const edge = bounds.left + 4
    const own = Math.min(Math.max(event.clientX, edge), bounds.right - 4)
    for (const left of edge === own ? [edge] : [edge, own]) {
      const found = instance.posAtCoords({ left, top: event.clientY })
      if (!found) continue
      const row = topRowAt(instance.state.doc, found.inside >= 0 ? found.inside : found.pos, event.clientY)
      if (row) return row
    }
    return null
  }

  /**
   * The row the gutter belongs to at a given height.
   *
   * `posAtCoords` answers with the innermost position, which inside a list item is the
   * paragraph that *is* the item. `rowAt` maps that back to the row the handle should move.
   */
  function topRowAt(doc: Parameters<typeof rowAt>[0], pos: number, clientY: number): BlockRef | null {
    const row = rowAt(doc, pos)
    if (!row) return null
    // A row whose box does not actually contain this height is a stale answer from a
    // collapsed toggle; fall back to whatever does.
    const rect = rectOf(row.pos)
    if (rect && clientY >= rect.top - 2 && clientY <= rect.bottom + 2) return row
    return row
  }

  function onPointerLeave(): void {
    if (!dragging.value) gutter.value = null
  }

  // ---------------------------------------------------------------- drag

  function startDrag(event: PointerEvent, pos: number): void {
    const instance = view()
    if (!instance) return
    const row = rowAt(instance.state.doc, pos)
    if (!row) return

    // Dragging a block that is part of a multi-block selection drags the selection (§4.2).
    const selection = getBlockSelection(instance.state)
    dragRange =
      selection && row.pos >= selection.from && row.end <= selection.to ? selection : rangeOf(row)

    origin = { x: event.clientX, y: event.clientY }
    started = false
    window.addEventListener('pointermove', onDragMove)
    window.addEventListener('pointerup', onDragEnd)
    window.addEventListener('keydown', onDragKey, true)
  }

  function onDragMove(event: PointerEvent): void {
    if (!dragRange) return
    if (!started) {
      const moved = Math.abs(event.clientX - origin.x) + Math.abs(event.clientY - origin.y)
      if (moved < DRAG_THRESHOLD) return
      started = true
      dragging.value = true
      document.body.classList.add('is-dragging-block')
      markSource(true)
    }
    const instance = view()
    if (instance && dragRange) {
      const slice = instance.state.doc.slice(dragRange.from, dragRange.to)
      preview.value = {
        x: event.clientX,
        y: event.clientY,
        text: slice.content.textBetween(0, slice.content.size, ' ').slice(0, 80) || '…',
        count: slice.content.childCount,
      }
    }
    updateTarget(event.clientX, event.clientY)
    runAutoscroll(event.clientY)
  }

  /**
   * Dim the blocks being moved, so the preview under the cursor is the live one (§4.2).
   *
   * Through a decoration, not by touching the DOM: ProseMirror rewrites a node's attributes
   * when it next syncs it, and a class set by hand is gone by the following frame.
   */
  function markSource(on: boolean): void {
    const instance = view()
    if (!instance) return
    instance.dispatch(setDragRange(instance.state.tr, on ? dragRange : null))
  }

  function updateTarget(x: number, y: number): void {
    const instance = view()
    const bounds = columnBounds()
    const host = scroller.value?.getBoundingClientRect()
    if (!instance || !bounds || !host || !dragRange) return

    const doc = instance.state.doc
    const rows = collectRows(doc).filter((row) => {
      const rect = rectOf(row.pos)
      return rect !== null && rect.height > 0
    })
    if (!rows.length) return

    // The gap: after the last row whose middle the pointer has passed.
    let above: BlockRef | null = null
    for (const row of rows) {
      const rect = rectOf(row.pos)
      if (!rect) continue
      if (y >= rect.top + rect.height / 2) above = row
      else break
    }

    const candidates = (above ? insertionsAfter(doc, above) : insertionsBefore(rows[0])).filter(
      // §4.2: a container cannot be dropped inside itself, and no indicator is shown for it.
      (entry) => !(entry.pos > dragRange!.from && entry.pos < dragRange!.to),
    )
    if (!candidates.length) {
      target = null
      indicator.value = null
      return
    }

    const measured = candidates
      .map((entry) => ({ entry, left: leftOf(entry, bounds.left) }))
      .sort((one, other) => one.left - other.left)

    // Deepest candidate the pointer has reached; the shallowest when it is left of them all.
    let chosen = measured[0]
    for (const option of measured) if (x >= option.left) chosen = option

    target = chosen.entry
    const top = above ? (rectOf(above.pos)?.bottom ?? 0) : (rectOf(rows[0].pos)?.top ?? 0)
    indicator.value = {
      top: top - host.top + (scroller.value?.scrollTop ?? 0),
      left: chosen.left - host.left,
      width: bounds.right - chosen.left,
    }
  }

  /** Where a dropped block's left edge would be, measured off the node it lines up with. */
  function leftOf(entry: Insertion, columnLeft: number): number {
    const rect = rectOf(entry.reference)
    if (!rect) return columnLeft
    if (!entry.inside) return rect.left
    // Inside a container: line up with what is already in it, and fall back to one step.
    const instance = view()
    if (instance) {
      const inner = collectRows(instance.state.doc).find(
        (row) => row.pos > entry.reference && row.end <= entry.pos + 1,
      )
      const innerRect = inner ? rectOf(inner.pos) : null
      if (innerRect && innerRect.left > rect.left) return innerRect.left
    }
    return rect.left + 24
  }

  function runAutoscroll(y: number): void {
    const host = scroller.value
    const rect = host?.getBoundingClientRect()
    if (!host || !rect) return
    stopAutoscroll()

    const above = y - rect.top
    const below = rect.bottom - y
    if (above > AUTOSCROLL_MARGIN && below > AUTOSCROLL_MARGIN) return

    const delta = above <= AUTOSCROLL_MARGIN ? -AUTOSCROLL_SPEED : AUTOSCROLL_SPEED
    autoscroll = window.setInterval(() => host.scrollBy({ top: delta }), 16)
  }

  function stopAutoscroll(): void {
    if (autoscroll !== null) {
      window.clearInterval(autoscroll)
      autoscroll = null
    }
  }

  /** §4.2: `Esc` during a drag cancels it and changes nothing. */
  function onDragKey(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return
    event.preventDefault()
    event.stopPropagation()
    target = null
    finishDrag()
  }

  function onDragEnd(): void {
    const instance = view()
    if (started && instance && dragRange && target) {
      const tr = instance.state.tr
      if (moveRange(tr, dragRange, target.pos)) {
        setBlockSelection(tr, { range: null, anchor: null })
        instance.dispatch(tr.scrollIntoView())
      }
    }
    finishDrag()
  }

  function finishDrag(): void {
    window.removeEventListener('pointermove', onDragMove)
    window.removeEventListener('pointerup', onDragEnd)
    window.removeEventListener('keydown', onDragKey, true)
    stopAutoscroll()
    markSource(false)
    document.body.classList.remove('is-dragging-block')
    dragging.value = false
    indicator.value = null
    preview.value = null
    dragRange = null
    target = null
    started = false
  }

  // ---------------------------------------------------------------- marquee (§4.7)

/**
 * Dragging over the empty space beside the text selects the blocks it sweeps.
 *
 * Only from *outside* the content, which is what keeps it from stealing the ordinary
 * drag-to-select-text gesture: a press that lands on the editable area is ProseMirror's.
 */
function startMarquee(event: PointerEvent): void {
  const host = scroller.value
  const content = view()?.dom
  if (!host || !(content instanceof HTMLElement)) return
  if (content.contains(event.target as globalThis.Node)) return
  if ((event.target as HTMLElement).closest('.block-gutter')) return

  marqueeOrigin = { x: event.clientX, y: event.clientY }
  window.addEventListener('pointermove', onMarqueeMove)
  window.addEventListener('pointerup', endMarquee)
}

function onMarqueeMove(event: PointerEvent): void {
  const instance = view()
  const host = scroller.value?.getBoundingClientRect()
  if (!marqueeOrigin || !instance || !host) return

  const top = Math.min(marqueeOrigin.y, event.clientY)
  const bottom = Math.max(marqueeOrigin.y, event.clientY)
  if (bottom - top < DRAG_THRESHOLD) return

  marquee.value = {
    top: top - host.top + (scroller.value?.scrollTop ?? 0),
    left: Math.min(marqueeOrigin.x, event.clientX) - host.left,
    width: Math.abs(event.clientX - marqueeOrigin.x),
    height: bottom - top,
  }

  // Only the outermost rows the sweep touches: selecting a callout *and* its children would
  // be two overlapping answers to "what did I just select".
  const swept = collectRows(instance.state.doc).filter((row) => {
    const rect = rectOf(row.pos)
    return rect !== null && rect.bottom > top && rect.top < bottom
  })
  const outer = swept.filter((row) => !swept.some((other) => other.pos < row.pos && other.end >= row.end))
  if (!outer.length) return

  const range = rangeBetween(instance.state.doc, outer[0], outer[outer.length - 1])
  instance.dispatch(setBlockSelection(instance.state.tr, { range, anchor: outer[0].pos }))
}

function endMarquee(): void {
  window.removeEventListener('pointermove', onMarqueeMove)
  window.removeEventListener('pointerup', endMarquee)
  marqueeOrigin = null
  marquee.value = null
}

/** §4.1: `+` puts an empty block below and leaves the caret in it. */
  function insertBelow(pos: number): void {
    const instance = view()
    if (!instance) return
    const row = rowAt(instance.state.doc, pos)
    if (!row) return
    editor.value
      ?.chain()
      .focus()
      .insertContentAt(row.end, { type: 'paragraph' })
      .setTextSelection(row.end + 1)
      // §4.1 wants the `/` menu open straight away. It is opened by *typing* the slash: the
      // suggestion plugin watches the document, so any other way of showing it would be a
      // second entry point to keep in step with the first.
      .insertContent('/')
      .run()
  }

  onBeforeUnmount(() => {
    finishDrag()
    endMarquee()
  })

  return {
    gutter,
    indicator,
    preview,
    marquee,
    dragging,
    onPointerMove,
    onPointerLeave,
    startMarquee,
    startDrag,
    insertBelow,
  }
}
