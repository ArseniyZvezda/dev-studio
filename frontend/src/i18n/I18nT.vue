<script setup lang="ts">
/**
 * A message with markup inside it — a `<code>` path, a link, a button — rendered without
 * cutting the sentence into fragments that a translator could never reorder.
 *
 * The message names each piece of markup as a placeholder, and the caller hands it over as a
 * slot of the same name:
 *
 *   "showDataDir": "Show the {dir} directory in the file tree"
 *
 *   <I18nT keypath="settings.explorer.showDataDir">
 *     <template #dir><code class="font-mono">voxarium_studio/</code></template>
 *   </I18nT>
 *
 * Text parameters go through `params` exactly as they do for `t()`, and are filled in first.
 * A placeholder with neither a parameter nor a slot is printed as written. It renders no
 * element of its own, so it replaces text inside existing markup without moving the layout.
 */
import { computed, type PropType } from 'vue'

import { t, type MessageKey, type MessageParams } from './index'

// A runtime declaration, not `defineProps<…>()`: the compiler cannot turn `MessageKey`, a
// mapped type over the English file, into a runtime prop type, and `PropType` keeps the check.
const props = defineProps({
  keypath: { type: String as PropType<MessageKey>, required: true },
  params: { type: Object as PropType<MessageParams>, default: undefined },
})

defineSlots<Record<string, () => unknown>>()

/** Alternating text and placeholder names: even indices are text, odd ones slot names. */
const parts = computed(() => t(props.keypath, props.params).split(/\{(\w+)\}/))

/** A placeholder nobody filled, as written. A function, since `}}` would end a mustache. */
const unfilled = (name: string) => `{${name}}`
</script>

<template>
  <template v-for="(part, index) in parts" :key="index">
    <slot v-if="index % 2 === 1" :name="part">{{ unfilled(part) }}</slot>
    <template v-else>{{ part }}</template>
  </template>
</template>
