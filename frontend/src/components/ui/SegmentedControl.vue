<script setup lang="ts" generic="T extends string">
/**
 * A row of mutually exclusive choices, for the settings that are one of three or four
 * words (spec §7's knobs). A `<select>` hides the alternatives behind a click; when the
 * whole set fits on one line, showing it is the better answer.
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue'

import type { Choice } from '@/lib/appearance'

const props = defineProps<{
  options: readonly Choice<T>[]
  modelValue: T
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [T] }>()

const group = ref<HTMLElement | null>(null)

/**
 * Where the checked option sits, as four custom properties on the group — so the stylesheet
 * can draw one thumb *under* the options and slide it between them (`base.css`, the motion
 * section): the switcher is one raised piece that moves, not two buttons swapping colour.
 * Level 3 of the styling rules, a subtree scoped by an inline property, because the options
 * are as wide as their words, so the geometry can only be measured, and this is the one
 * component that knows which option is checked. The group is `relative`, which makes
 * `offsetLeft` its own coordinate. `data-thumb` arrives with the first measurement, and the
 * thumb is not drawn before it, so nothing slides in from zero on mount.
 *
 * Re-measured when the value changes (after the DOM has moved `aria-checked`) and whenever
 * the group's size does — which is any option's width changing, a font arriving included.
 */
const thumb = ref<CSSProperties | null>(null)

function measure(): void {
  const checked = group.value?.querySelector<HTMLElement>('[aria-checked="true"]')
  if (!checked) {
    thumb.value = null
    return
  }
  thumb.value = {
    '--vxs-thumb-x': `${checked.offsetLeft}px`,
    '--vxs-thumb-y': `${checked.offsetTop}px`,
    '--vxs-thumb-w': `${checked.offsetWidth}px`,
    '--vxs-thumb-h': `${checked.offsetHeight}px`,
  }
}

watch(
  () => props.modelValue,
  () => void nextTick(measure),
)

let observer: ResizeObserver | null = null

onMounted(() => {
  measure()
  if (typeof ResizeObserver === 'undefined' || !group.value) return
  observer = new ResizeObserver(measure)
  observer.observe(group.value)
})

onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <!-- `vxs-segmented` draws the rest (`base.css`): the well of the canvas grey, the raised
       checked option, and the thumb that carries the raise from one option to the next. -->
  <div
    ref="group"
    class="vxs-segmented relative inline-flex gap-0.5 border border-card p-0.5"
    role="radiogroup"
    :style="thumb ?? undefined"
    :data-thumb="thumb ? '' : undefined"
  >
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      role="radio"
      :aria-checked="option.value === modelValue"
      class="px-2.5 py-0.5 whitespace-nowrap disabled:opacity-50"
      :class="option.value === modelValue ? 'text-primary' : 'text-muted hover:text-primary'"
      :title="option.hint"
      :disabled="disabled"
      @click="emit('update:modelValue', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
