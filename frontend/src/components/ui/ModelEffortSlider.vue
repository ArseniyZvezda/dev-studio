<script setup lang="ts">
/** §10.3: one fixed-size effort control for settings and read-only run metadata. */
import { computed } from 'vue'

import { t } from '@/i18n'

const props = withDefaults(defineProps<{
  modelValue: string | null
  levels?: readonly string[]
  readonly?: boolean
  label?: string
  /** Null means model default on a member, inheritance on a task. */
  defaultLabel?: string
  defaultTitle?: string
}>(), {
  levels: () => ['low', 'medium', 'high', 'xhigh', 'max'],
  readonly: false,
})
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()

// Keep stored values from newer catalogues visible without silently rewriting them.
const steps = computed<(string | null)[]>(() => {
  const levels = [...new Set(props.levels.filter(Boolean))]
  if (props.modelValue && !levels.includes(props.modelValue)) levels.push(props.modelValue)
  return [null, ...levels]
})
const index = computed(() => Math.max(0, steps.value.indexOf(props.modelValue)))
const fraction = computed(() => index.value / Math.max(1, steps.value.length - 1))
const valueLabel = computed(() => props.modelValue || props.defaultLabel || t('modelEffort.default'))
const label = computed(() => props.label || t('teams.editor.effort'))
const valueDescription = computed(() => props.modelValue || props.defaultTitle || t('teams.editor.effortDefault'))
const description = computed(() => `${label.value}: ${valueDescription.value}`)
const style = computed(() => ({
  '--effort-mix': `${fraction.value * 100}%`,
  // Match the native 12px thumb's travel inside the fixed 112px control.
  '--effort-fill': `${fraction.value * 100}%`,
  '--effort-thumb': `${6 + fraction.value * 100}px`,
}))

function update(event: Event): void {
  if (props.readonly) return
  const value = Number((event.target as HTMLInputElement).value)
  if (Number.isInteger(value) && value >= 0 && value < steps.value.length) {
    emit('update:modelValue', steps.value[value] ?? null)
  }
}
</script>

<template>
  <span
    class="model-effort"
    :class="{ 'model-effort--editable': !readonly }"
    :style="style"
    :title="description"
    :role="readonly ? 'img' : undefined"
    :aria-label="readonly ? description : undefined"
  >
    <span class="model-effort__value" aria-hidden="true">{{ valueLabel }}</span>
    <span class="model-effort__track" aria-hidden="true">
      <span class="model-effort__fill" />
      <span v-if="!readonly" class="model-effort__thumb" />
    </span>
    <input
      v-if="!readonly"
      class="model-effort__input"
      type="range"
      min="0"
      :max="steps.length - 1"
      step="1"
      :value="index"
      :aria-label="label"
      :aria-valuetext="valueDescription"
      @input="update"
    />
  </span>
</template>

<style scoped>
.model-effort {
  --effort-muted: #9589a4;
  --effort-bright: #a66aff;
  --effort-color: color-mix(in oklab, var(--effort-muted), var(--effort-bright) var(--effort-mix));
  position: relative;
  display: inline-flex;
  flex: 0 0 112px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  width: 112px;
  min-width: 112px;
  max-width: 112px;
  height: 32px;
  vertical-align: middle;
  color: var(--effort-color);
}

.model-effort__value {
  overflow: hidden;
  font-size: 11px;
  font-weight: 700;
  line-height: 13px;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-effort__track {
  position: relative;
  display: block;
  height: 7px;
  border-radius: 999px;
  background: color-mix(in oklab, var(--effort-muted) 19%, transparent);
}

.model-effort__fill {
  display: block;
  width: var(--effort-fill);
  height: 100%;
  border-radius: inherit;
  background: currentColor;
}

.model-effort__input {
  appearance: none;
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  opacity: 0;
  cursor: ew-resize;
}

.model-effort__input::-webkit-slider-thumb {
  appearance: none;
  width: 12px;
  height: 12px;
}

.model-effort__input::-moz-range-thumb {
  width: 12px;
  height: 12px;
  border: 0;
}

.model-effort__thumb {
  position: absolute;
  top: 50%;
  left: var(--effort-thumb);
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px #0004;
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.model-effort--editable:has(.model-effort__input:focus-visible) {
  outline: 2px solid var(--effort-color);
  outline-offset: 3px;
  border-radius: 4px;
}

.model-effort--editable:hover .model-effort__track {
  background: color-mix(in oklab, var(--effort-muted) 30%, transparent);
}

[data-theme='light'] .model-effort {
  --effort-muted: #776780;
  --effort-bright: #802de0;
}

@media (forced-colors: active) {
  .model-effort { color: CanvasText; }
  .model-effort__track { border: 1px solid CanvasText; }
  .model-effort__fill { background: Highlight; forced-color-adjust: none; }
}
</style>
