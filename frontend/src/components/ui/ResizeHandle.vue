<script setup lang="ts">
/**
 * The draggable divider between two panes.
 *
 * How wide a rail is belongs to the *page*, not to the panel inside it, because it is the
 * other pane that has to give the width up. So this component owns the gesture and
 * nothing else, reporting a new width through `v-model`.
 *
 * `side` is which edge of the sized panel the handle sits on, and that is what decides the
 * sign of the drag: a rail on the right grows as the pointer moves *left*.
 *
 * The seam is drawn here rather than as a border on the panel next to it, so the 1px line
 * and the pointer target are one element — a hairline is not something anyone can hit, and
 * two of them side by side is a double border. `seam: false` is for the case where the panel
 * beside it already draws that line through `border-pane` (§7's chrome tokens).
 */
import { computed, ref } from 'vue'

import { t } from '@/i18n'
import { clampPaneWidth } from '@/lib/pane-width'

const props = withDefaults(
  defineProps<{
    /** Current width of the panel this handle sizes, in pixels. */
    modelValue: number
    /** Which edge of that panel the handle sits on. */
    side: 'left' | 'right'
    min?: number
    max?: number
    /**
     * Whether the line is drawn at rest. Off where the panel beside the handle already draws
     * its own edge through `border-pane` — that token is the one thing that knows how chrome
     * meets content, and a second line here would double the pane's own. The five pixels of
     * target and the hover feedback are unchanged; only the resting line goes.
     */
    seam?: boolean
    /** Width a double-click goes back to. Unset, a double-click does nothing. */
    resetTo?: number
    /**
     * How much width there is to divide, if the caller knows better than the window does.
     *
     * `window.innerWidth` is the fallback and is deliberately coarse: it counts the rail and
     * every gutter as space this pane could have, so the remainder it reserves for the centre
     * is not the remainder the centre gets. A caller that can measure the row the two panes
     * actually share should pass that instead.
     */
    available?: number
  }>(),
  // `resetTo` is deliberately absent: `undefined` is its meaningful value, not a default.
  { min: 200, max: 720, seam: true },
)

const emit = defineEmits<{ 'update:modelValue': [number] }>()

/** One arrow key press. Coarse on purpose: the keyboard path is for nudging a rail into
 *  place, and a 1px step would need two hundred presses to cross the panel. */
const STEP = 16

const dragging = ref(false)

/** The rule itself is `lib/pane-width.ts`, because a pane that *remembers* its width has to
 *  apply the same ceiling on the way back out of storage — and two copies of a clamp is two
 *  clamps that disagree the first time one of them moves. */
function clamp(width: number): number {
  return clampPaneWidth(width, props.min, props.max, props.available ?? window.innerWidth)
}

function onPointerDown(event: PointerEvent) {
  if (event.button !== 0) return
  const handle = event.currentTarget as HTMLElement
  const origin = event.clientX
  const start = props.modelValue
  const direction = props.side === 'left' ? -1 : 1

  // The pointer leaves this 5px strip on the first frame of any real drag; capturing it is
  // what keeps the move events arriving here instead of at the canvas underneath, which
  // would otherwise start panning.
  handle.setPointerCapture(event.pointerId)
  // Without this the press begins a text selection that then follows the pointer across
  // every pane it crosses.
  event.preventDefault()
  dragging.value = true

  const onMove = (move: PointerEvent) => {
    emit('update:modelValue', clamp(start + direction * (move.clientX - origin)))
  }
  const onUp = () => {
    dragging.value = false
    handle.removeEventListener('pointermove', onMove)
    handle.removeEventListener('pointerup', onUp)
    handle.removeEventListener('pointercancel', onUp)
  }

  handle.addEventListener('pointermove', onMove)
  handle.addEventListener('pointerup', onUp)
  handle.addEventListener('pointercancel', onUp)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  const towards = event.key === 'ArrowLeft' ? -1 : 1
  const direction = props.side === 'left' ? -1 : 1
  event.preventDefault()
  emit('update:modelValue', clamp(props.modelValue + direction * towards * STEP))
}

/** Say what the gesture is, and only claim the reset where there is one to go back to. */
const hint = computed(() =>
  props.resetTo === undefined ? t('ui.resize.drag') : t('ui.resize.dragOrReset'),
)

/**
 * Back to the width the page opens at. Clamped like any other, so a default that no longer
 * fits this window is still a width the pane can be.
 *
 * `onPointerDown`'s `preventDefault` suppresses text selection, not the synthesized
 * `dblclick`, and the capture is released on each `pointerup` — so the two presses each
 * start a drag that moves nothing, and then the reset lands.
 */
function onDoubleClick() {
  if (props.resetTo !== undefined) emit('update:modelValue', clamp(props.resetTo))
}
</script>

<template>
  <div
    class="resize-handle"
    :class="{ 'resize-handle-dragging': dragging, 'resize-handle-flush': !seam }"
    role="separator"
    aria-orientation="vertical"
    :aria-valuenow="Math.round(modelValue)"
    :aria-valuemin="min"
    :aria-valuemax="max"
    tabindex="0"
    :title="hint"
    @pointerdown="onPointerDown"
    @dblclick="onDoubleClick"
    @keydown="onKeydown"
  />
</template>

<style scoped>
/**
 * Five pixels of target around one pixel of seam: the transparent borders are what widen
 * the hit area without widening the line, since `background-clip: content-box` keeps the
 * paint off them.
 */
.resize-handle {
  flex: none;
  width: 5px;
  border-left: 2px solid transparent;
  border-right: 2px solid transparent;
  background: var(--vxs-border);
  background-clip: content-box;
  cursor: col-resize;
  touch-action: none;
}

/* `seam: false` — the panel beside the handle draws the edge, so at rest this is a hit
   target and nothing else. It still lights up on hover, focus and drag: a hairline is not
   something anyone can hit, and an invisible one even less so. Declared *before* the state
   group below, which it ties on specificity and would otherwise beat on source order. */
.resize-handle-flush {
  background: transparent;
}

.resize-handle:hover,
.resize-handle:focus-visible,
.resize-handle-dragging {
  background: var(--vxs-accent);
  background-clip: content-box;
}
</style>
