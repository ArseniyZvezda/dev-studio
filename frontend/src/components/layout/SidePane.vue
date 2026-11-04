<script setup lang="ts">
/**
 * The side pane Explorer and Documents share (§6.2, §10.4).
 *
 * One container for both, because they are the same object seen twice: a titled panel with
 * a row of icon buttons, fixed strips under it, one scroller, and notes along the bottom.
 * It owns the width as well, which is the reason it is a component rather than a copied
 * `<aside>` — a pane the user drags has to remember how wide they left it, and two views
 * each doing that is two places to get the clamp and the corrupt-value guard right.
 *
 * `ResizeHandle`'s docstring puts a width on the *page* rather than on the panel, because
 * whoever owns it has to arrange both sides of the seam — a panel that sizes itself from the
 * inside makes its neighbour pay. This component is that thing: it renders the pane *and*
 * the handle, and the `<section class="min-w-0 flex-1">` the view puts beside it takes
 * whatever is left, so the centre reflows without a number crossing a component boundary at
 * all. Hence no `emit` for the width, and no layout store.
 *
 * Two root nodes on purpose: the pane and the seam are flex siblings in the view's own row, so
 * the width set on the `<aside class="vxs-pane">` is the width that row lays out, with no
 * wrapper to size in between.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ResizeHandle from '@/components/ui/ResizeHandle.vue'
import { clampPaneWidth, paneWidth } from '@/lib/pane-width'

// `inheritAttrs` is deliberately *left alone*. A two-root component cannot auto-inherit
// attributes, and Vue warns about that in development — but only while `inheritAttrs` is
// true, because setting it false is how a component says "I forward `$attrs` myself".
// This one does not, so declaring the option would buy silence rather than the loud failure
// a `class` added at a call site deserves.

const props = withDefaults(
  defineProps<{
    /** What the pane calls itself, top left. */
    title: string
    /**
     * Where this pane's width is remembered — the whole `localStorage` key, so a grep for
     * `pane-width` finds every pane that has one. Per pane and per browser; `lib/pane-width.ts`
     * says why it is not in the project's config.
     */
    storageKey: string
    /** Width on a first visit, and what a double-click on the seam goes back to. */
    defaultWidth: number
    min?: number
    max?: number
  }>(),
  { min: 200, max: 560 },
)

/**
 * The width the user *picked* and the width that *fits* are two different numbers, and
 * keeping them apart is what makes a remembered width safe.
 *
 * `chosen` is the gesture, and the only thing written to storage. `width` is what the pane
 * renders at: the same number with the viewport ceiling applied, so a pane dragged to 560px
 * on a maximised 2560px monitor and reopened in an 800px window does not leave the centre
 * forty pixels wide with the seam that would fix it off the useful part of the screen.
 *
 * Folding the two together would mean the narrowest window the pane was ever opened in
 * filed the stored value down permanently — the user's choice quietly rewritten by a window
 * they resized once. The handle's own clamp cannot cover this either: it runs during a
 * gesture, and neither restoring a pane nor resizing the window is one.
 */
const chosen = ref(
  paneWidth(window.localStorage.getItem(props.storageKey), props.defaultWidth, props.min, props.max),
)

/**
 * The width there is to divide, measured off the row this pane and the centre share rather
 * than taken from `window.innerWidth`.
 *
 * The window is the wrong ruler by exactly the rail: 192px of a 760px window is never
 * available to either of them, so a ceiling of `innerWidth - 320` leaves the centre about
 * ninety pixels while claiming to have reserved three hundred and twenty. Measuring the row
 * makes the reserve mean what it says. A `ResizeObserver` rather than a `resize` listener,
 * because the rail is itself a variable width — `data-sidebar="icons"` takes it to 48px
 * without the window moving at all.
 */
const pane = ref<HTMLElement | null>(null)
const row = ref(window.innerWidth)
let observer: ResizeObserver | undefined

onMounted(() => {
  const parent = pane.value?.parentElement
  if (!parent) return
  observer = new ResizeObserver(([entry]) => (row.value = entry.contentRect.width))
  observer.observe(parent)
})
onBeforeUnmount(() => observer?.disconnect())

const width = computed(() => clampPaneWidth(chosen.value, props.min, props.max, row.value))

/**
 * A drag emits a width per frame and `localStorage.setItem` is synchronous, so the write
 * trails the gesture rather than riding it: the number is worth remembering, not worth
 * writing sixty times a second. The timer is deliberately not cleared on unmount — a
 * pending write is exactly what still has to happen when someone drags the pane and then
 * navigates away from the page.
 */
let pending: ReturnType<typeof setTimeout> | undefined
watch(chosen, (value) => {
  clearTimeout(pending)
  pending = setTimeout(() => window.localStorage.setItem(props.storageKey, String(value)), 250)
})

const style = computed(() => ({ width: `${width.value}px` }))
</script>

<template>
  <!-- `overflow-hidden` so that nothing but the scroller in the default slot can overflow the
       pane — it is what actually scrolls. -->
  <aside
    ref="pane"
    class="vxs-pane flex shrink-0 flex-col overflow-hidden border-r border-pane bg-surface"
    :style="style"
  >
    <!-- Not `vxs-pane`: on a `<header>` that class names a *page* header. This one is the
         pane's own, on the page header's ground, with its title set as a caption — the tree
         under it is what the pane is about. -->
    <header class="pane-head flex items-center gap-1 border-b border-pane px-3 py-2">
      <span class="flex-1 truncate text-small tracking-wide text-muted uppercase">{{ title }}</span>
      <slot name="actions" />
    </header>

    <!-- Fixed strips between the header and the scroller — an error, a create form, a
         search field. Each stays the caller's own element, so it keeps its own `v-if` and
         its own border; an empty slot renders a comment node and no flex item. -->
    <slot name="top" />

    <!-- The scroller belongs to the caller, not here, and that is the load-bearing part of
         this contract. The Explorer's scroller carries the tree's `ref`, its `tabindex`, the
         project root's right-click target and every keyboard shortcut; slot content is
         compiled in the caller's scope, so all of that keeps working with nothing forwarded
         through this component. A wrapper with `overflow-auto` here would take the scroll off
         the element those handlers are bound to — do not add one. -->
    <slot />

    <slot name="footer" />
  </aside>

  <!-- The handle drags from what is on screen and writes back to what was chosen: `width` is
       a computed, and the difference between the two is only ever the viewport ceiling, which
       the handle applies to its own emission anyway. -->
  <ResizeHandle
    :model-value="width"
    side="right"
    :min="min"
    :max="max"
    :seam="false"
    :reset-to="defaultWidth"
    :available="row"
    @update:model-value="chosen = $event"
  />
</template>

<style scoped>
.pane-head {
  background: var(--vxs-header-fill);
}
</style>
