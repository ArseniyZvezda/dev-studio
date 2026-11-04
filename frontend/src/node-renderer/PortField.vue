<script setup lang="ts">
/**
 * One form control, chosen by `PortSchema.type` (spec §8.1 / §9.3).
 *
 * This is what makes a plugin's node configurable without any plugin-supplied UI: the
 * schema the backend already publishes is enough to render and validate the field.
 *
 * The three path-ish types also get a Browse button, which is the only control here that is
 * not a plain HTML input. It has to exist because the alternative is asking the user to type
 * a project-relative path from memory — and because the picker only describes paths inside
 * the project, so it cannot produce one the backend will refuse.
 *
 * `color` is the other one that earns a control of its own: the swatch is the only way to
 * *see* the value, and the eyedropper is the only way to answer "that colour, there". It
 * samples the screen, so it reaches anything the browser is drawing — a node's own preview
 * included — which is why a plugin does not have to ship a picker to be aimable.
 */
import { FolderOpen, Pipette } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import ProjectPathPicker from '@/components/pipeline/ProjectPathPicker.vue'
import type { PortSchema } from '@/api/types'
import { t } from '@/i18n'

const props = defineProps<{ port: PortSchema; modelValue: unknown }>()
const emit = defineEmits<{ 'update:modelValue': [unknown] }>()

const inputClass =
  'w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent'

/** JSON is edited as text and only committed when it parses, so a half-typed object
 *  does not clobber the stored value. */
const jsonText = ref('')
const jsonError = ref<string | null>(null)

watch(
  () => props.modelValue,
  (value) => {
    if (props.port.type !== 'json') return
    const serialised = value === undefined || value === null ? '' : JSON.stringify(value, null, 2)
    if (serialised !== jsonText.value) jsonText.value = serialised
  },
  { immediate: true },
)

function commitJson(text: string) {
  jsonText.value = text
  if (!text.trim()) {
    jsonError.value = null
    emit('update:modelValue', null)
    return
  }
  try {
    emit('update:modelValue', JSON.parse(text))
    jsonError.value = null
  } catch (error) {
    jsonError.value = error instanceof Error ? error.message : t('pipelines.field.invalidJson')
  }
}

const numberValue = computed(() =>
  props.modelValue === undefined || props.modelValue === null ? '' : Number(props.modelValue),
)

function commitNumber(raw: string) {
  if (raw === '') {
    emit('update:modelValue', null)
    return
  }
  const parsed = props.port.type === 'integer' ? Number.parseInt(raw, 10) : Number.parseFloat(raw)
  if (!Number.isNaN(parsed)) emit('update:modelValue', parsed)
}

/**
 * The value as `<input type="color">` insists on having it: exactly `#rrggbb`.
 *
 * Anything else — a half-typed value, a shorthand, an empty field — makes the native
 * control silently show black, which would read as "the colour is black" rather than "this
 * is not a colour yet". The text input beside it keeps whatever the user actually typed.
 */
const swatchValue = computed(() => {
  const raw = String(props.modelValue ?? props.port.default ?? '')
    .trim()
    .toLowerCase()
  const digits = raw.startsWith('#') ? raw.slice(1) : raw
  const expanded =
    digits.length === 3
      ? digits
          .split('')
          .map((digit) => digit + digit)
          .join('')
      : digits.slice(0, 6)
  return /^[0-9a-f]{6}$/.test(expanded) ? `#${expanded}` : '#ffffff'
})

/** Chromium ships the screen picker; Firefox and Safari do not, and hiding it is honest. */
type ScreenPicker = { open(): Promise<{ sRGBHex: string }> }
const screenPicker = typeof window !== 'undefined' && 'EyeDropper' in window

async function pickFromScreen() {
  const Picker = (window as unknown as { EyeDropper: new () => ScreenPicker }).EyeDropper
  try {
    emit('update:modelValue', (await new Picker().open()).sRGBHex)
  } catch {
    // The user pressed Escape. There is nothing to report and nothing to change.
  }
}

/** `path` is "a file or a folder"; the other two narrow it. `core.path` is why it exists. */
const PICKER_MODE = { file: 'file', directory: 'directory', path: 'path' } as const

const pickerMode = computed(() => PICKER_MODE[props.port.type as keyof typeof PICKER_MODE])
const picking = ref(false)

function pick(value: string) {
  picking.value = false
  emit('update:modelValue', value)
}

const placeholder = computed(() => {
  if (props.port.default !== null && props.port.default !== undefined) {
    return t('pipelines.field.defaultValue', { value: String(props.port.default) })
  }
  return props.port.accept.length ? props.port.accept.join(', ') : ''
})
</script>

<template>
  <label class="block">
    <span class="mb-1 flex items-baseline gap-1">
      <span class="font-medium">{{ port.label || port.id }}</span>
      <span v-if="port.required" class="text-danger" :title="t('pipelines.field.required')">*</span>
      <span class="ml-auto font-mono text-[10px] text-faint">{{ port.type }}</span>
      <!-- Whoever owns the node instance puts the pin toggle here: the port's *type* is
           this component's business, and whether it is pinned is the instance's. -->
      <slot name="actions" />
    </span>

    <textarea
      v-if="port.type === 'text'"
      :value="(modelValue as string) ?? ''"
      rows="4"
      :placeholder="placeholder"
      :class="[inputClass, 'resize-y font-mono']"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />

    <template v-else-if="port.type === 'json'">
      <textarea
        :value="jsonText"
        rows="4"
        :class="[inputClass, 'resize-y font-mono']"
        @input="commitJson(($event.target as HTMLTextAreaElement).value)"
      />
      <span v-if="jsonError" class="mt-1 block text-small text-danger">{{ jsonError }}</span>
    </template>

    <select
      v-else-if="port.type === 'enum'"
      :value="(modelValue as string) ?? ''"
      :class="inputClass"
      @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
    >
      <option value="">—</option>
      <option v-for="option in port.options" :key="option" :value="option">{{ option }}</option>
    </select>

    <label
      v-else-if="port.type === 'boolean'"
      class="flex cursor-pointer items-center gap-2 py-1"
    >
      <input
        type="checkbox"
        :checked="Boolean(modelValue)"
        class="accent-[var(--vxs-accent)]"
        @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)"
      />
      <span class="text-muted">
        {{ modelValue ? t('pipelines.field.enabled') : t('pipelines.field.disabled') }}
      </span>
    </label>

    <input
      v-else-if="port.type === 'number' || port.type === 'integer'"
      type="number"
      :value="numberValue"
      :min="port.minimum ?? undefined"
      :max="port.maximum ?? undefined"
      :step="port.step ?? (port.type === 'integer' ? 1 : 'any')"
      :placeholder="placeholder"
      :class="inputClass"
      @input="commitNumber(($event.target as HTMLInputElement).value)"
    />

    <div v-else-if="port.type === 'color'" class="flex gap-1">
      <input
        type="color"
        :value="swatchValue"
        class="h-[26px] w-9 shrink-0 cursor-pointer rounded-md border border-line bg-inset p-0.5"
        :title="swatchValue"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <input
        type="text"
        :value="(modelValue as string) ?? ''"
        :placeholder="placeholder"
        :class="[inputClass, 'font-mono']"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        v-if="screenPicker"
        type="button"
        class="flex shrink-0 items-center gap-1 rounded-md border border-line px-2 hover:bg-hover"
        :title="t('pipelines.field.pickColour')"
        @click.stop.prevent="pickFromScreen"
      >
        <Pipette class="size-3.5" />
      </button>
    </div>

    <div v-else-if="pickerMode" class="flex gap-1">
      <input
        type="text"
        :value="(modelValue as string) ?? ''"
        :placeholder="placeholder"
        :class="[inputClass, 'font-mono']"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        type="button"
        class="flex shrink-0 items-center gap-1 rounded-md border border-line px-2 hover:bg-hover"
        :title="t('pipelines.field.browse')"
        @click.stop.prevent="picking = true"
      >
        <FolderOpen class="size-3.5" />
      </button>
    </div>

    <input
      v-else
      type="text"
      :value="(modelValue as string) ?? ''"
      :placeholder="placeholder"
      :class="inputClass"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />

    <span v-if="port.description" class="mt-1 block text-small text-faint">
      {{ port.description }}
    </span>

    <ProjectPathPicker
      v-if="picking && pickerMode"
      :mode="pickerMode"
      :initial="((modelValue as string) ?? '').trim()"
      :accept="port.accept"
      @submit="pick"
      @cancel="picking = false"
    />
  </label>
</template>
