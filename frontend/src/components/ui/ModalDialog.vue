<script setup lang="ts">
/**
 * The centred panel every full dialog on the task board wears (§10.4).
 *
 * Both things the board opens — the composer and a task — are the same gesture: the page
 * dims, one panel takes the middle, and `Escape` or a click outside puts it away. That was
 * written twice before this existed, so the two drifted on the parts nobody looks at twice
 * — the overlay's opacity, whether `Escape` reached a handler at all.
 *
 * It owns the frame and nothing else. The panel's size is the caller's (`panelClass`), and
 * so is everything inside it: a dialog that hugs its content and one that fills the window
 * are the same component with different classes, which is what lets the task dialog grow to
 * the whole window without this file knowing what a task is.
 *
 * `Escape` closes it, but the content gets the key first: anything inside can register an
 * `EscapeConsumer` and cancel its own layer — an open form, a confirmation — before the
 * dialog closes over the top of it.
 *
 * A click outside closes it, and "outside" means **both halves of the click**: see
 * `onOverlayUp`.
 */
import { nextTick, onMounted, onScopeDispose, provide, ref } from 'vue'

import { dialogEscape, type EscapeConsumer } from '@/lib/dialog'
import { useLeave } from '@/lib/leave'

const props = defineProps<{
  /** Named for assistive technology, which has no header to read from a slot. */
  label: string
  /** Size and shape of the panel — everything else about it is fixed here. */
  panelClass?: string
  /**
   * Neither arrives nor leaves: there, then gone. For a panel the keyboard opens many times a
   * day — the search palette — where an entrance is a delay under the first keystroke (§7).
   */
  instant?: boolean
}>()
const emit = defineEmits<{ close: [] }>()

const overlay = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)

// The dialog goes out the way it came in. The caller's `v-if` still unmounts it at once;
// what fades for a beat afterwards is the overlay Vue has already let go of (`lib/leave.ts`).
useLeave(
  () => overlay.value,
  () => !props.instant,
)

/**
 * Layers of the content that want `Escape` for themselves, innermost first.
 *
 * Registered rather than sniffed out of the DOM, because the key is heard on the panel and
 * the panel is nobody's descendant — see `lib/dialog.ts`. Each consumer unregisters with
 * the component that added it, so a form that has gone away cannot claim the key.
 */
const consumers = new Set<EscapeConsumer>()

provide(dialogEscape, (consumer: EscapeConsumer) => {
  consumers.add(consumer)
  onScopeDispose(() => consumers.delete(consumer))
})

function onEscape(): void {
  for (const consumer of consumers) if (consumer()) return
  emit('close')
}

// The panel takes focus only when the content did not claim it: a form focuses the field it
// was opened for, and stealing that back would put the caret nowhere. What this covers is
// the other case — a dialog with nothing to type into still has to hear `Escape`.
onMounted(async () => {
  await nextTick()
  if (!panel.value?.contains(document.activeElement)) panel.value?.focus({ preventScroll: true })
})

/**
 * Focus that leaves for nowhere comes back to the panel.
 *
 * `Escape` is heard on the panel, so it only works while focus is inside it — and a button
 * that removes itself takes focus with it. Pressing Delete swaps the button row for a
 * confirmation, the browser parks focus on `<body>`, and from there the key reaches nothing
 * at all: the dialog stops closing until the user happens to click back inside it. A null
 * `relatedTarget` is exactly that case (the control went away, or the click landed on
 * something unfocusable), and it is the one worth catching — focus moving to another real
 * control is somebody's deliberate `Tab`.
 */
function onFocusOut(event: FocusEvent): void {
  if (event.relatedTarget) return
  void nextTick(() => {
    // `preventScroll`, because this fires on an ordinary click into the transcript as well:
    // focusing the panel must never jump the panel under the reader.
    if (!panel.value?.contains(document.activeElement)) panel.value?.focus({ preventScroll: true })
  })
}

/**
 * Where the press that is about to become a click started.
 *
 * Not a `ref`: nothing renders from it, and a re-render between the two halves of a click
 * would be work done for nobody.
 */
let pressedOverlay = false

function onOverlayDown(event: PointerEvent): void {
  pressedOverlay = event.target === event.currentTarget
}

/**
 * A click outside, and not merely a button coming up out there.
 *
 * `@click.self` was the whole guard here, and `click` fires on the **common ancestor** of
 * where the button went down and where it came up — which for a press inside the panel and
 * a release over the overlay is the overlay itself. So selecting the text of a brief and
 * letting go a few pixels past the panel closed the dialog and threw the edit away. Both
 * halves have to land on the overlay, which is what a person means by "clicking outside".
 */
function onOverlayUp(event: PointerEvent): void {
  const outside = pressedOverlay && event.target === event.currentTarget
  pressedOverlay = false
  if (outside) emit('close')
}
</script>

<template>
  <Teleport to="body">
    <!-- `vxs-veil` is how the motion section of `base.css` fades the veil in as the dialog
         arrives, and out again as it leaves; `data-instant` switches both off. -->
    <div
      ref="overlay"
      class="vxs-veil fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-6"
      :data-instant="instant ? '' : undefined"
      @pointerdown="onOverlayDown"
      @pointerup="onOverlayUp"
    >
      <!-- Flex and not `grid place-items-center`, which reads as the same thing and is not:
           a grid row sizes itself to its content, so the panel's own `max-h-full` resolves
           against a row as tall as the panel and constrains nothing. A short window then had
           the footer buttons off the bottom of the screen. A flex item's percentage resolves
           against the container, which is the whole point of the cap. -->
      <div
        ref="panel"
        class="vxs-card flex max-h-full max-w-full flex-col overflow-hidden rounded-xl border border-card bg-surface shadow-lg outline-none"
        :class="panelClass"
        role="dialog"
        aria-modal="true"
        :aria-label="label"
        tabindex="-1"
        @keydown.esc.prevent="onEscape"
        @focusout="onFocusOut"
      >
        <slot />
      </div>
    </div>
  </Teleport>
</template>
